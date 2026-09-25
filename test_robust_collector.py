import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from robust_collector import Store, Engine
from robust_runtime import AdaptiveRate, InstanceLock, Sessions, Transport, export_snapshot, retry_after
from robust_service import make_handler
from speed_trial import acceptable


def success(data):
    return {'ok':True,'category':'success','data':data,'generation':1}


def listing(ids,more=False):
    return {'rows':[{'unified':{'id':i,'canonical_name':'fixture '+str(i)}} for i in ids], 'hasMore':more}


def until(predicate,seconds=6):
    limit=time.monotonic()+seconds
    while time.monotonic()<limit:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('Condition did not become true')


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.store=Store(self.root)

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def test_retry_does_not_advance_page(self):
        task=self.store.claim('list')
        self.store.apply(task,{'ok':False,'category':'transient','status':400,'code':4030,'message':'temporary','delay':120})
        self.assertEqual(self.store.get('next_page'),1)
        state,deadline=self.store.db.execute('SELECT state,next_retry FROM tasks WHERE task_key=?',(task['key'],)).fetchone()
        self.assertEqual(state,'retry')
        self.assertGreater(deadline,time.time()+119)
        self.assertIsNone(self.store.claim('list'))

    def test_atomic_page_rollback_and_dedup(self):
        first=self.store.claim('list')
        self.store.apply(first,success(listing([1,2],True)))
        second=self.store.claim('list')
        with self.assertRaises(ValueError):
            self.store.apply(second,success(listing([2,3])))
        self.assertEqual(self.store.get('next_page'),2)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM journals').fetchone()[0],2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM tasks WHERE kind='detail'").fetchone()[0],2)

    def test_schema_failure_does_not_advance_page(self):
        task=self.store.claim('list')
        with self.assertRaises(ValueError):
            self.store.apply(task,success({'rows':[],'hasMore':True}))
        self.assertEqual(self.store.get('next_page'),1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0],0)

    def test_pagination_blocker_survives_restart(self):
        task=self.store.claim('list')
        self.store.apply(task,{'ok':False,'category':'permanent','status':400,'code':400,'message':'pagination cap'})
        restarted=Store(self.root)
        try:
            self.assertEqual(restarted.summary()['list_blocker']['message'],'pagination cap')
            self.assertEqual(restarted.db.execute("SELECT state FROM tasks WHERE task_key='list:1'").fetchone()[0],'blocked')
        finally:
            restarted.db.close()

    def test_completion_needs_second_pass_and_matching_stats(self):
        stats={'totalJournals':1,'matcherBuiltAt':'fixture','matcherSchemaVersion':'v1'}
        self.store.apply(self.store.claim('stats'),success(stats))
        self.store.apply(self.store.claim('list'),success(listing([1])))
        self.store.apply(self.store.claim('detail'),success({'unified':{'id':1,'all_issns':'1234-5678'},'sources':{}}))
        self.assertFalse(self.store.summary()['complete'])
        verify=self.store.claim('verify')
        with self.assertRaises(ValueError):
            self.store.apply(verify,success(listing([2])))
        self.store.apply(verify,success(listing([1])))
        with self.store.db:
            self.store.put('stats_end',stats)
        self.assertFalse(self.store.summary()['complete'])
        with self.store.db:
            self.store.enqueue('stats:end','stats',{})
        self.store.apply(self.store.claim('stats'),success(stats))
        self.assertTrue(self.store.summary()['complete'])
        with self.store.db:
            self.store.put('stats_end',{**stats,'matcherBuiltAt':'changed'})
        self.assertFalse(self.store.summary()['complete'])

    def test_abrupt_process_exit_reclaims_running_work(self):
        code='''from pathlib import Path
import os,sys
from robust_collector import Store
s=Store(Path(sys.argv[1]))
s.claim('list')
s.db.execute('BEGIN')
s.db.execute("INSERT INTO journals(id,list_json) VALUES(999,'{}')")
os._exit(19)
'''
        result=subprocess.run([sys.executable,'-c',code,str(self.root)],cwd=Path(__file__).parent,capture_output=True)
        self.assertEqual(result.returncode,19,result.stderr)
        restarted=Store(self.root)
        try:
            self.assertEqual(restarted.db.execute("SELECT state FROM tasks WHERE task_key='list:1'").fetchone()[0],'pending')
            self.assertEqual(restarted.db.execute('SELECT COUNT(*) FROM journals').fetchone()[0],0)
            self.assertEqual(restarted.db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        finally:
            restarted.db.close()

    def test_failed_export_preserves_published_generation(self):
        self.store.apply(self.store.claim('list'),success(listing([1])))
        previous=export_snapshot(self.root)
        pointer=(self.root/'exports'/'CURRENT.json').read_bytes()
        with self.store.db:
            self.store.db.execute("UPDATE journals SET list_json='invalid-json'")
        with self.assertRaises(ValueError):
            export_snapshot(self.root)
        self.assertEqual((self.root/'exports'/'CURRENT.json').read_bytes(),pointer)
        with gzip.open(self.root/'exports'/previous['generation']/'journals.jsonl.gz','rt',encoding='utf-8') as stream:
            self.assertEqual(json.loads(stream.readline())['id'],1)


class RuntimeTests(unittest.TestCase):
    def test_twelve_rps_still_honors_global_backoff(self):
        clock=[0.0]
        rate=AdaptiveRate(8,clock=lambda:clock[0])
        rate.set_maximum(12)
        self.assertEqual(rate.rate,2)
        for _ in range(1500):
            clock[0]+=.25
            rate.observe(True,.2,200)
        self.assertEqual(rate.rate,12)
        rate.observe(False,.2,429,180)
        self.assertEqual(rate.rate,6)
        self.assertEqual(rate.cooldown_until,clock[0]+180)

    def test_mixed_trial_requires_useful_work_and_detail_floor(self):
        result={'duration_seconds':300,'committed_rps':8.5,'committed_work_rps':11.8,
                'error_fraction':0,'http_429_503':0,'latency_p95_ms':220,
                'workload':'mixed','minimum_detail_rps':6.5,'new_bad_tasks':0,'new_bad_partitions':0}
        self.assertTrue(acceptable(result,12,300))
        for key,value in [('committed_rps',6),('committed_work_rps',9),('new_bad_tasks',1),('new_bad_partitions',1)]:
            self.assertFalse(acceptable({**result,key:value},12,300))
    def test_degraded_detail_is_retried_not_accepted_as_complete(self):
        class Response:
            status=200
            will_close=False
            def read(self): return b'{"code":0,"data":{"id":1,"degraded":true}}'
            def getheader(self,key): return None
        class Connection:
            def request(self,*args): pass
            def getresponse(self): return Response()
        with tempfile.TemporaryDirectory() as directory:
            sessions=Sessions()
            sessions.set('Bearer fixture')
            rate=AdaptiveRate()
            with patch.object(rate,'acquire'),patch('robust_runtime.http.client.HTTPSConnection',return_value=Connection()):
                result=Transport(Path(directory),rate,sessions).fetch('detail',{'id':1},'detail:1',1)
            self.assertFalse(result['ok'])
            self.assertEqual(result['category'],'transient')
            self.assertIn('degraded',result['message'])

    def test_live_trial_requires_full_window_and_committed_throughput(self):
        result={'duration_seconds':300,'committed_rps':7.8,'error_fraction':0,
                'http_429_503':0,'latency_p95_ms':220}
        self.assertTrue(acceptable(result,8,300))
        for key,value in [('duration_seconds',299),('committed_rps',6),('error_fraction',.02),
                          ('http_429_503',1),('latency_p95_ms',1600)]:
            self.assertFalse(acceptable({**result,key:value},8,300))

    def test_live_ceiling_preserves_cooldown_and_ramp(self):
        clock=[100.0]
        rate=AdaptiveRate(4,clock=lambda:clock[0])
        rate.rate=4
        rate.observe(False,.2,429,180)
        rate.set_maximum(8)
        self.assertEqual(rate.rate,2)
        self.assertEqual(rate.cooldown_until,280)
        self.assertEqual(rate.clean_successes,0)
        rate.set_maximum(1)
        self.assertEqual(rate.rate,1)
        self.assertGreaterEqual(rate.next_slot,101)
        self.assertEqual(rate.transitions[-1]['reason'],'configuration')
        for invalid in (None,True,'8',0,13,float('nan'),float('inf')):
            with self.assertRaises(ValueError):
                rate.set_maximum(invalid)
        self.assertEqual(rate.maximum,1)

    def test_eight_rps_ceiling_and_retry_metrics(self):
        clock=[0.0]
        rate=AdaptiveRate(8,clock=lambda:clock[0])
        for _ in range(500):
            clock[0]+=.5
            rate.observe(True,.2,200,attempt=2)
        self.assertEqual(rate.rate,8)
        self.assertEqual(rate.snapshot()['recent_retry_fraction'],1)
        rate.observe(False,.2,503,90)
        self.assertEqual(rate.rate,4)
        self.assertEqual(rate.cooldown_until,clock[0]+90)
        self.assertEqual(rate.transitions[-1]['reason'],'http_backoff')

    def test_full_retry_after_and_http_date(self):
        self.assertEqual(retry_after('180'),180)
        self.assertEqual(retry_after('Thu, 01 Jan 1970 00:03:00 GMT',epoch=0),180)
        self.assertEqual(retry_after('invalid'),0)

    def test_adaptive_rate_ramps_and_globally_backs_off(self):
        clock=[0.0]
        rate=AdaptiveRate(4,clock=lambda:clock[0])
        for _ in range(120):
            clock[0]+=1
            rate.observe(True,.2,200)
        self.assertEqual(rate.rate,4)
        rate.observe(False,.2,429,180)
        self.assertEqual(rate.rate,2)
        self.assertEqual(rate.cooldown_until,clock[0]+180)
        self.assertEqual(rate.snapshot()['recent_errors'],1)

    def test_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            first=InstanceLock(Path(directory)/'lock').acquire()
            try:
                with self.assertRaises(RuntimeError):
                    InstanceLock(Path(directory)/'lock').acquire()
            finally:
                first.close()
            second=InstanceLock(Path(directory)/'lock').acquire()
            second.close()

    def test_frequent_errors_reduce_rate_without_http_429(self):
        clock=[0.0]
        rate=AdaptiveRate(4,clock=lambda:clock[0])
        rate.rate=4
        for i in range(30):
            clock[0]+=1
            rate.observe(i%2==0,.2,200 if i%2==0 else 400)
        self.assertEqual(rate.rate,2)

    def test_keep_alive_and_credential_not_logged(self):
        class Response:
            status=200
            will_close=False
            def read(self): return b'{"code":0,"data":{}}'
            def getheader(self,key): return None
        class Connection:
            def request(self,*args): pass
            def getresponse(self): return Response()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            sessions=Sessions()
            sessions.set('Bearer fake-test-credential')
            rate=AdaptiveRate()
            with patch.object(rate,'acquire'),patch('robust_runtime.http.client.HTTPSConnection',return_value=Connection()) as constructor:
                transport=Transport(root,rate,sessions)
                self.assertTrue(transport.fetch('stats',{},'test:1',1)['ok'])
                self.assertTrue(transport.fetch('stats',{},'test:2',1)['ok'])
                self.assertEqual(constructor.call_count,1)
            logs=(root/'requests.jsonl').read_text(encoding='utf-8')
            self.assertNotIn('fake-test-credential',logs)
            for line in logs.splitlines():
                row=json.loads(line)
                with gzip.open(root/row['raw'],'rb') as stream:
                    self.assertEqual(hashlib.sha256(stream.read()).hexdigest(),row['sha256'])

    def test_old_failed_session_cannot_invalidate_new_one(self):
        sessions=Sessions()
        old=sessions.set('old')
        sessions.set('new')
        sessions.invalidate(old)
        self.assertEqual(sessions.read()[0],'new')

    def test_broken_connection_is_logged_and_replaced(self):
        class Broken:
            closed=False
            def request(self,*args): raise ConnectionResetError('fixture disconnect')
            def close(self): self.closed=True
        class Response:
            status=200
            will_close=False
            def read(self): return b'{"code":0,"data":{}}'
            def getheader(self,key): return None
        class Working:
            def request(self,*args): pass
            def getresponse(self): return Response()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            sessions=Sessions()
            sessions.set('Bearer fake-fixture-session')
            rate=AdaptiveRate()
            broken=Broken()
            with patch.object(rate,'acquire'),patch('robust_runtime.http.client.HTTPSConnection',side_effect=[broken,Working()]):
                transport=Transport(root,rate,sessions)
                failed=transport.fetch('stats',{},'test:network',1)
                self.assertEqual(failed['category'],'transient')
                self.assertIsNone(failed['raw'])
                self.assertTrue(broken.closed)
                self.assertTrue(transport.fetch('stats',{},'test:network',2)['ok'])


class EngineTests(unittest.TestCase):
    def test_list_failure_does_not_stop_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            store=Store(root)
            with store.db:
                store.db.execute('INSERT INTO journals(id,list_json) VALUES(?,?)',(1,json.dumps({'unified':{'id':1}})))
            store.db.close()
            engine=Engine(root)
            def fake(kind,payload,key,attempt):
                if kind=='list':
                    return {'ok':False,'category':'permanent','status':400,'message':'pagination cap','generation':1}
                if kind=='detail':
                    return success({'unified':{'id':payload['id']}})
                return success({'totalJournals':100})
            engine.transport.fetch=fake
            engine.set_session('Bearer fixture-session')
            thread=threading.Thread(target=engine.run)
            thread.start()
            try:
                until(lambda:engine.current().get('status')=='blocked')
                self.assertEqual(engine.current()['details'],1)
                self.assertEqual(engine.current()['queue']['list']['blocked'],1)
            finally:
                engine.stop.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())

    def test_auth_expiry_resumes_without_process_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            engine=Engine(root)
            auth_failed=threading.Event()
            def fake(kind,payload,key,attempt):
                token,generation=engine.sessions.read()
                if generation==1:
                    auth_failed.set()
                    return {'ok':False,'category':'auth','status':401,'message':'expired','generation':generation}
                if kind in ('list','verify'):
                    return success(listing([1]))
                if kind=='detail':
                    return success({'unified':{'id':1}})
                return success({'totalJournals':1,'matcherBuiltAt':'fixture'})
            engine.transport.fetch=fake
            engine.set_session('Bearer expired-fixture')
            thread=threading.Thread(target=engine.run)
            thread.start()
            try:
                self.assertTrue(auth_failed.wait(5))
                until(lambda:engine.sessions.read()[0] is None)
                engine.set_session('Bearer renewed-fixture')
                until(lambda:engine.current().get('status')=='complete')
                self.assertEqual(engine.current()['details'],1)
            finally:
                engine.stop.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())

    def test_control_origin_nonce_and_session_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            engine=Engine(Path(directory))
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(engine,'fixture-nonce'))
            thread=threading.Thread(target=server.serve_forever)
            thread.start()
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
            try:
                for origin,expected in [('https://unrelated.example',403),('https://www.scholay.com',202)]:
                    connection.request('POST','/session/fixture-nonce',json.dumps({'authorization':'Bearer fake-fixture-session'}),
                                       {'Origin':origin,'Content-Type':'application/json'})
                    response=connection.getresponse()
                    response.read()
                    self.assertEqual(response.status,expected)
                self.assertEqual(engine.sessions.read()[0],'Bearer fake-fixture-session')
                for value,expected in [(10,202),(12,202),(13,400),(None,400),(True,400)]:
                    connection.request('POST','/control/fixture-nonce',json.dumps({'action':'configure_rate','max_rps':value}),
                                       {'Origin':'https://www.scholay.com','Content-Type':'application/json'})
                    response=connection.getresponse()
                    response.read()
                    self.assertEqual(response.status,expected)
                self.assertEqual(engine.rate.maximum,12)
                self.assertEqual(engine.rate.rate,2)
                self.assertEqual(len((Path(directory)/'rate_events.jsonl').read_text().splitlines()),2)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join()


if __name__=='__main__':
    unittest.main()
