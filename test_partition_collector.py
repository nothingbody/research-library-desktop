import json
from pathlib import Path
import tempfile
import time
import threading
import gzip
import hashlib
import unittest
from unittest.mock import patch

from robust_collector import Store, Engine
from test_robust_collector import success, until


def page(ids, more=False, source='crossref', direction='asc', oa=False):
    return {'rows':[{'unified':{'id':i,'canonical_name':str(i),'sources':source,
                               'openalex_is_oa':oa,'doaj_is_in_doaj':False}} for i in ids],
            'hasMore':more,'pageMeta':{'applied_sort':{'by':'id','dir':direction}}}


class PartitionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.store=Store(self.root)
        self.baseline={'totalJournals':3,'matcherBuiltAt':'fixed','matcherSchemaVersion':'fixture',
                       'sourceCoverage':{'crossref':3}}
        self.store.apply(self.store.claim('stats'),success(self.baseline))

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def start(self, scope='all', data=None):
        self.store.request_partitions(scope)
        task=self.store.claim('stats')
        self.store.apply(task,success(data or self.baseline))

    def test_early_reverse_returns_to_forward_and_closes_gap(self):
        with patch('partition_collector.PAGE_LIMIT',3),patch('partition_collector.PAGE_SIZE',2):
            self.start()
            self.store.apply(self.store.claim('partition'),success(page([1,2],True)))
            with self.store.db:
                self.store.put('scheduler_mode','discovery_first')
            self.store.prioritize_unseen()
            for ids in ([8,7],[6,5],[4,3]):
                task=self.store.claim('partition')
                self.assertEqual(task['payload']['sort_dir'],'desc')
                self.store.apply(task,success(page(ids,True,direction='desc')))
            task=self.store.claim('partition')
            self.assertEqual((task['payload']['sort_dir'],task['payload']['page']),('asc',2))
            self.store.apply(task,success(page([3,4],True)))
            self.assertEqual(self.store.db.execute('SELECT reason FROM partitions').fetchone()[0],'bidirectional_overlap')
            self.assertEqual([x[0] for x in self.store.db.execute('SELECT id FROM journals ORDER BY id')],list(range(1,9)))
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tasks WHERE kind='partition_verify'").fetchone()[0],5)

    def test_early_meet_retires_queued_opposite_page_without_skipping_verification(self):
        with patch('partition_collector.PAGE_SIZE',2):
            self.start()
            self.store.apply(self.store.claim('partition'),success(page([1,2],True)))
            with self.store.db:
                self.store.put('scheduler_mode','discovery_first')
            self.store.prioritize_unseen()
            self.store.apply(self.store.claim('partition'),success(page([3,2],True,direction='desc')))
            self.assertEqual(self.store.db.execute("SELECT state FROM tasks WHERE task_key='partition:crossref:asc:2'").fetchone()[0],'superseded')
            self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tasks WHERE kind='partition_verify'").fetchone()[0],2)
            self.assertFalse(self.store.partitions_verified())

    def test_priority_never_switches_a_partition_with_running_request(self):
        with patch('partition_collector.PAGE_SIZE',2):
            self.start()
            self.store.apply(self.store.claim('partition'),success(page([1,2],True)))
            with self.store.db:
                self.store.put('scheduler_mode','discovery_first')
            task=self.store.claim('partition')
            self.assertIsNone(self.store.claim('partition'))
            self.store.prioritize_unseen()
            self.assertEqual(self.store.db.execute('SELECT direction FROM partitions').fetchone()[0],'asc')
            self.store.apply(task,success(page([3,4],True)))
            self.store.prioritize_unseen()
            self.assertEqual(self.store.db.execute('SELECT direction FROM partitions').fetchone()[0],'desc')

    def test_dedup_preserves_detail_and_requires_verification(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO journals(id,list_json,detail_json,detail_status) VALUES(1,'{}','{\"saved\":true}','ok')")
        self.start()
        self.store.apply(self.store.claim('partition'),success(page([1,2,3])))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM journals').fetchone()[0],3)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tasks WHERE kind='detail'").fetchone()[0],2)
        self.assertEqual(self.store.db.execute('SELECT detail_json FROM journals WHERE id=1').fetchone()[0],'{"saved":true}')
        self.assertFalse(self.store.partitions_verified())
        self.store.apply(self.store.claim('partition_verify'),success(page([1,2,3])))
        self.assertTrue(self.store.partitions_verified())
        self.assertTrue(self.store.coverage_audit()['source_counts_match'])
        self.assertFalse(self.store.summary()['complete'])

    def test_bidirectional_meet_and_all_page_verification(self):
        with patch('partition_collector.PAGE_LIMIT',1),patch('partition_collector.PAGE_SIZE',2):
            self.start()
            self.store.apply(self.store.claim('partition'),success(page([1,2],True)))
            reverse=self.store.claim('partition')
            self.assertEqual(reverse['payload']['sort_dir'],'desc')
            self.store.apply(reverse,success(page([3,2],True,direction='desc')))
            self.assertEqual(self.store.db.execute('SELECT reason FROM partitions').fetchone()[0],'bidirectional_overlap')
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM journals').fetchone()[0],3)
            while task:=self.store.claim('partition_verify'):
                desc=task['payload']['sort_dir']=='desc'
                self.store.apply(task,success(page([3,2] if desc else [1,2],True,direction='desc' if desc else 'asc')))
            self.assertTrue(self.store.partitions_verified())

    def test_uncovered_gap_is_blocked_not_complete(self):
        with patch('partition_collector.PAGE_LIMIT',1),patch('partition_collector.PAGE_SIZE',2):
            self.start()
            self.store.apply(self.store.claim('partition'),success(page([1,2],True)))
            self.store.apply(self.store.claim('partition'),success(page([10,9],True,direction='desc')))
            self.assertEqual(self.store.db.execute('SELECT state FROM partitions').fetchone()[0],'blocked')
            self.assertFalse(self.store.partitions_verified())
            self.assertFalse(self.store.summary()['complete'])

    def test_source_mismatch_rolls_back_page_and_checkpoint(self):
        self.start()
        task=self.store.claim('partition')
        with self.assertRaisesRegex(ValueError,'Source membership'):
            self.store.apply(task,success(page([1],source='openalex')))
        self.store.validation_failure(task,ValueError('Source membership'))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM journals').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT next_asc FROM partitions').fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT state FROM partitions').fetchone()[0],'blocked')

    def test_changed_overlap_cannot_hide_missing_ids(self):
        with patch('partition_collector.PAGE_LIMIT',1),patch('partition_collector.PAGE_SIZE',2):
            self.start()
            self.store.apply(self.store.claim('partition'),success(page([1,4],True)))
            task=self.store.claim('partition')
            with self.assertRaisesRegex(ValueError,'overlap changed'):
                self.store.apply(task,success(page([5,3],True,direction='desc')))
            self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM journals').fetchone()[0],2)
            self.assertEqual(self.store.db.execute('SELECT next_desc FROM partitions').fetchone()[0],1)

    def test_verification_detects_source_change(self):
        self.start()
        self.store.apply(self.store.claim('partition'),success(page([1,2])))
        task=self.store.claim('partition_verify')
        with self.assertRaisesRegex(ValueError,'IDs changed'):
            self.store.apply(task,success(page([1,3])))
        self.assertFalse(self.store.partitions_verified())

    def test_restart_gates_next_page_on_boundary(self):
        with patch('partition_collector.PAGE_SIZE',2):
            self.start()
            self.store.apply(self.store.claim('partition'),success(page([1,2],True)))
            claimed=self.store.claim('partition')
            self.assertEqual(claimed['payload']['page'],2)
            self.store.db.close()
            self.store=Store(self.root)
            self.assertIsNone(self.store.claim('partition'))
            self.store.apply(self.store.claim('partition_boundary'),success(page([1,2],True)))
            self.assertEqual(self.store.claim('partition')['payload']['page'],2)

    def test_due_retry_is_not_starved_by_pending_work(self):
        with self.store.db:
            for i in range(1,50):
                self.store.enqueue('detail:'+str(i),'detail',{'id':i})
            self.store.db.execute("UPDATE tasks SET state='retry',next_retry=? WHERE task_key='detail:49'",(time.time()-1,))
        tasks=[self.store.claim('detail'),self.store.claim('detail')]
        self.assertIn('detail:49',[t['key'] for t in tasks])
        self.assertIn('detail:1',[t['key'] for t in tasks])

    def test_oa_split_missing_members_fails_global_coverage(self):
        self.baseline['sourceCoverage']={'openalex':3}
        with self.store.db:
            self.store.put('stats_start',self.baseline)
        self.start()
        for _ in range(2):
            task=self.store.claim('partition')
            oa=task['payload']['is_oa']
            self.store.apply(task,success(page([1] if oa else [2],source='openalex',oa=oa)))
        while task:=self.store.claim('partition_verify'):
            oa=task['payload']['is_oa']
            self.store.apply(task,success(page([1] if oa else [2],source='openalex',oa=oa)))
        self.assertTrue(self.store.partitions_verified())
        self.assertFalse(self.store.coverage_audit()['source_counts_match'])
        self.assertEqual(self.store.coverage_audit()['missing_journals'],1)
        self.assertFalse(self.store.summary()['complete'])

    def test_pilot_can_expand_idempotently_without_losing_progress(self):
        self.start('pilot')
        self.store.apply(self.store.claim('partition'),success(page([1,2,3])))
        self.store.apply(self.store.claim('partition_verify'),success(page([1,2,3])))
        self.assertFalse(self.store.partitions_verified())
        self.start('all')
        self.assertTrue(self.store.partitions_verified())
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM partition_batches').fetchone()[0],1)

    def test_changed_baseline_cannot_start_partitions(self):
        self.store.request_partitions('all')
        with self.assertRaisesRegex(ValueError,'baseline changed'):
            self.store.apply(self.store.claim('stats'),success({**self.baseline,'totalJournals':4}))
        self.assertFalse(self.store.get('partitions_enabled',False))

    def test_partition_pause_preserves_detail_progress(self):
        self.start()
        with self.store.db:
            self.store.put('partitions_paused',True)
            self.store.enqueue('detail:99','detail',{'id':99})
        self.assertIsNone(self.store.claim('partition'))
        self.assertEqual(self.store.claim('detail')['key'],'detail:99')
        with self.store.db:
            self.store.put('partitions_paused',False)
        self.assertIsNotNone(self.store.claim('partition'))

    def test_only_proven_degraded_identity_errors_are_requeued(self):
        with self.store.db:
            for jid in (1,2):
                self.store.enqueue('detail:'+str(jid),'detail',{'id':jid})
            self.store.db.execute("UPDATE tasks SET state='blocked',error='Detail identity mismatch' WHERE kind='detail'")
        events=[]
        for jid,returned in ((1,1),(2,999)):
            raw=json.dumps({'code':0,'data':{'degraded':True,'id':returned}}).encode()
            path='fixture-'+str(jid)+'.gz'
            (self.root/path).write_bytes(gzip.compress(raw))
            events.append({'task':'detail:'+str(jid),'raw':path,'sha256':hashlib.sha256(raw).hexdigest()})
        (self.root/'requests.jsonl').write_text('\n'.join(json.dumps(x) for x in events)+'\n',encoding='utf-8')
        self.store.recover_degraded_details()
        states=dict(self.store.db.execute("SELECT task_key,state FROM tasks WHERE kind='detail'"))
        self.assertEqual(states,{'detail:1':'retry','detail:2':'blocked'})

    def test_engine_completes_new_workflow_and_keeps_legacy_cap_evidence(self):
        self.store.apply(self.store.claim('list'),{'ok':False,'category':'permanent','status':400,'message':'old pagination cap'})
        self.store.db.close()
        engine=Engine(self.root)
        calls=[]
        def fake(kind,payload,key,attempt):
            self.assertFalse(any(k.startswith('_') for k in payload))
            calls.append((kind,key))
            if kind in ('partition','partition_verify'):
                return success(page([1,2,3]))
            if kind=='detail':
                return success({'unified':{'id':payload['id']}})
            return success(self.baseline)
        engine.transport.fetch=fake
        engine.commands.put({'action':'start_partitions','scope':'all'})
        engine.set_session('Bearer fixture')
        thread=threading.Thread(target=engine.run)
        thread.start()
        try:
            until(lambda:engine.current().get('status')=='complete',seconds=10)
            self.assertEqual(engine.current()['records'],3)
            self.assertEqual(engine.current()['details'],3)
            self.assertIn(('stats','stats:partition-discovered'),calls)
            self.assertIn(('stats','stats:end'),calls)
        finally:
            engine.stop.set()
            thread.join(10)
            self.store=Store(self.root)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.store.get('list_blocker')['message'],'old pagination cap')
        self.assertTrue(self.store.summary()['complete'])


if __name__=='__main__':
    unittest.main()
