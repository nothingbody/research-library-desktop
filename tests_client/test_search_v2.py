import json
import threading
import time
import unittest
from tests_client import test_ai_search as base
from client_backend.common import AppError
from client_backend.search_evidence import verified_model
from client_backend.ai_search import _year


class SearchV2Test(unittest.TestCase):
    setUp = base.AiSearchTest.setUp
    tearDown = base.AiSearchTest.tearDown
    wait_job = base.AiSearchTest.wait_job
    record = staticmethod(base.AiSearchTest.record)
    def session(self, brief='distributed flexible job shop scheduling multi objective', sources=None):
        return self.app.ai_search.create({'brief': brief, 'sources': sources or ['openalex'], 'mode': 'deep', 'useModel': False})

    def run_sync(self, session):
        return self.app.ai_search.run({'sessionId': session['id']}, lambda *a: None)

    def test_all_six_queries_execute_and_log(self):
        s = self.session()
        p = s['plan']
        p['queries'] = [{'id': str(i), 'query': f'scheduling term {i}'} for i in range(6)]
        self.app.ai_search.save_plan({'sessionId': s['id'], 'plan': p})
        called = []
        def records(source, queries, limit):
            called.extend(queries)
            return [self.record(source)]
        self.app.ai_search._source_records = records
        self.run_sync(s)
        result = self.app.ai_search.results({'sessionId': s['id']})
        self.assertEqual(len(called), 6)
        self.assertEqual(len(result['queryRuns']), 6)
        self.assertEqual(result['total'], 1)

    def test_required_concept_and_missing_abstract_do_not_pass(self):
        s = self.session()
        single = self.record('openalex', 'Multi objective flexible job shop scheduling')
        single['abstract'] = 'This model considers one factory only.'
        unknown = {**single, 'doi':'10.1000/unknown', 'title':'Distributed multi objective flexible job shop scheduling', 'abstract':''}
        self.app.ai_search._source_records = lambda *a: [single, unknown]
        self.run_sync(s)
        rows = self.app.ai_search.results({'sessionId':s['id']})['items']
        by_doi = {r['doi']:r for r in rows}
        self.assertEqual(by_doi[single['doi']]['screening'], 'excluded')
        self.assertEqual(by_doi[single['doi']]['tier'], 'candidate')
        self.assertEqual(by_doi[unknown['doi']]['screening'], 'pending')
        self.assertEqual(self.app.ai_search._score(s, single)[1], 'candidate')

    def test_default_results_put_supported_conditions_before_high_keyword_score(self):
        s = self.session()
        records = []
        for suffix, abstract in (
            ('supported', 'Distributed multi objective flexible job shop scheduling is investigated.'),
            ('missing', ''),
            ('contradicted', 'This study concerns a single factory only and multi objective flexible job shop scheduling.'),
        ):
            title = f'Multi objective flexible job shop scheduling {suffix}' if suffix == 'contradicted' else f'Distributed multi objective flexible job shop scheduling {suffix}'
            record = self.record('openalex', title, f'10.1000/{suffix}')
            record.update(sourceId=suffix, abstract=abstract)
            records.append(record)
        self.app.ai_search._source_records = lambda *a: records
        self.run_sync(s)
        with self.app.library.db(True) as db:
            for row in db.execute('SELECT candidate_id,data_json FROM search_candidate_details').fetchall():
                value = json.loads(row['data_json'])
                value['score'] = {'supported': 0.25, 'missing': 0.65, 'contradicted': 0.95}[value['doi'].split('/')[-1]]
                db.execute('UPDATE search_candidate_details SET data_json=? WHERE candidate_id=?', (json.dumps(value), row['candidate_id']))
        rows = self.app.ai_search.results({'sessionId': s['id']})['items']
        self.assertEqual([r['screening'] for r in rows], ['eligible', 'pending', 'excluded'])
        self.assertEqual([r['doi'].split('/')[-1] for r in rows], ['supported', 'missing', 'contradicted'])
        self.assertEqual([r['id'] for r in self.app.search_evaluation.report({'sessionId': s['id']})['items']],
                         [r['id'] for r in rows])
        lexical = self.app.ai_search.results({'sessionId': s['id'], 'sort': 'score'})['items']
        self.assertEqual(lexical[0]['doi'], '10.1000/contradicted')

    def test_source_merge_is_order_independent_and_retains_conflicts(self):
        a = self.record('openalex')
        b = self.record('crossref')
        b.update(abstract='', oaUrl='', year=2024, citationCount=2)
        s = self.session(sources=['openalex','crossref'])
        self.app.ai_search._source_records = lambda source,*_: [a if source=='openalex' else b]
        self.run_sync(s)
        r = self.app.ai_search.results({'sessionId':s['id']})['items'][0]
        self.assertEqual(r['abstract'], a['abstract'])
        self.assertEqual(r['oaUrl'], a['oaUrl'])
        self.assertEqual(r['year'], 2024)
        self.assertEqual(len(r['fieldSources']['year']['values']), 2)
        self.assertEqual(self.app.ai_search._merge([a,b]), self.app.ai_search._merge([b,a]))

    def test_rerun_isolates_old_results_and_old_metadata(self):
        s = self.session()
        self.app.ai_search._source_records = lambda *a: [self.record('openalex','Old result')]
        first = self.run_sync(s)
        old = self.app.ai_search.results({'sessionId':s['id']})['items'][0]
        self.app.ai_search.decision({'candidateId': old['id'], 'decision':'exclude','reason':'manual'})
        plan=s['plan'];plan['queries']=[{'query':'a different new query'}]
        self.app.ai_search.save_plan({'sessionId':s['id'],'plan':plan})
        self.app.ai_search._source_records = lambda *a: [self.record('openalex','New result','10.1000/new')]
        self.run_sync(s)
        current = self.app.ai_search.results({'sessionId':s['id']})
        historical = self.app.ai_search.results({'sessionId':s['id'],'runId':first['runId']})
        self.assertEqual([r['title'] for r in current['items']], ['New result'])
        self.assertEqual(historical['items'][0]['title'], 'Old result')
        self.assertEqual(historical['items'][0]['decision']['decision'], 'exclude')
        self.assertTrue(historical['historical'])
        self.assertNotEqual(historical['runPlan']['queries'][0]['query'], 'a different new query')
        with self.assertRaises(AppError): self.app.ai_search.evidence(old['id'])

    def test_same_work_across_sessions_keeps_candidate_and_import_snapshot(self):
        a = self.session()
        self.app.ai_search._source_records = lambda *x: [self.record('openalex','Original title')]
        self.run_sync(a)
        b = self.session()
        self.app.ai_search._source_records = lambda *x: [self.record('openalex','Changed title')]
        self.run_sync(b)
        old = self.app.ai_search.results({'sessionId':a['id']})['items'][0]
        self.assertEqual(old['title'], 'Original title')
        imported = self.app.ai_search.import_candidates({'candidateIds':[old['id']]})
        self.assertEqual(self.app.library.get(imported['imported'][0]['itemId'])['title'], 'Original title')

    def test_paging_sorting_filters_and_exact_nondoi_identity(self):
        s = self.session(sources=['openalex','crossref'])
        def records(source,*_):
            return [{**self.record(source,f'Paper {i:03}', ''), 'sourceId':f'{source}-{i}'} for i in range(231)]
        self.app.ai_search._source_records = records
        self.run_sync(s)
        first = self.app.ai_search.results({'sessionId':s['id'],'sort':'title','limit':200})
        last = self.app.ai_search.results({'sessionId':s['id'],'sort':'title','offset':200})
        self.assertEqual(first['total'], 231)
        self.assertEqual(first['items'][0]['title'], 'Paper 000')
        self.assertEqual(last['items'][0]['title'], 'Paper 200')
        self.assertEqual(len(last['items']), 31)
        filtered = self.app.ai_search.results({'sessionId':s['id'],'q':'Paper 230','yearFrom':2025,'oaOnly':True})
        self.assertEqual(filtered['total'],1)
        self.assertEqual(len(filtered['items'][0]['sources']),2)

    def test_partial_query_failure_preserves_success(self):
        s = self.session()
        p = s['plan']; p['queries']=[{'query':'good scheduling'},{'query':'bad scheduling'}]
        self.app.ai_search.save_plan({'sessionId':s['id'],'plan':p})
        def records(source,queries,*_):
            if queries[0].startswith('bad'): raise AppError('SOURCE_TIMEOUT','timeout')
            return [self.record(source)]
        self.app.ai_search._source_records=records
        result=self.run_sync(s)
        self.assertEqual(result['resultCount'],1)
        self.assertEqual(self.app.ai_search.get(s['id'])['state'],'partial')
        self.assertEqual(len(result['errors']),1)

    def test_cancel_stops_before_more_queries_and_can_rerun(self):
        s=self.session()
        started, release=threading.Event(),threading.Event()
        def records(*_):
            started.set(); release.wait(3)
            return [self.record('openalex')]
        self.app.ai_search._source_records=records
        job=self.app.ai_search.submit({'sessionId':s['id']})
        self.assertTrue(started.wait(3))
        self.app.ai_search.cancel({'sessionId':s['id']})
        release.set()
        finished=self.wait_job(job['jobId'])
        self.assertEqual(finished['state'],'cancelled')
        self.assertEqual(self.app.ai_search.get(s['id'])['state'],'cancelled')
        self.app.ai_search._source_records=lambda *a:[self.record('openalex')]
        self.assertEqual(self.run_sync(s)['resultCount'],1)

    def test_quotes_are_verified_and_invented_extracts_dropped(self):
        criterion={'id':'x','label':'分布式','required':True,'kind':'include','terms':[],'negativeTerms':[]}
        ev=[{'id':'e1','kind':'abstract','text':'We study distributed scheduling.'}]
        raw={'checks':[{'id':'x','status':'pass','references':[{'evidenceId':'e1','quote':'invented evidence'}]}],
             'summaryZh':{'text':'虚构结论','references':[{'evidenceId':'bad','quote':'We study distributed scheduling.'}]}}
        checks,fields=verified_model(raw,[criterion],ev)
        self.assertEqual(checks[0]['status'],'unknown'); self.assertEqual(fields,{})
        raw['checks'][0]['references']=[{'evidenceId':'e1','quote':ev[0]['text']}]
        checks,_=verified_model(raw,[criterion],ev)
        self.assertEqual(checks[0]['status'],'pass')

    def test_explicit_model_screening_and_structured_fields(self):
        s=self.session()
        self.app.ai_search._source_records=lambda *a:[self.record('openalex')]
        self.run_sync(s)
        candidate=self.app.ai_search.results({'sessionId':s['id']})['items'][0]
        self.app.assistant.status=lambda:{'ready':True}
        def verify(context):
            ref={'evidenceId':context['evidence'][0]['id'],'quote':context['evidence'][0]['text']}
            return {'checks':[{'id':c['id'],'status':'unknown','references':[]} for c in context['criteria']],
                    'summaryZh':{'text':'调度研究，具体约束需确认','references':[ref]}}
        self.app.assistant.search_verify=verify
        with self.assertRaises(AppError): self.app.ai_search.submit_verify({'candidateIds':[candidate['id']]})
        job=self.app.ai_search.submit_verify({'candidateIds':[candidate['id']],'allowRemote':True})
        self.assertEqual(self.wait_job(job['jobId'])['state'],'completed')
        result=self.app.ai_search.evidence(candidate['id'])
        self.assertEqual(result['screening'],'pending')
        self.assertIn('summaryZh',result['fields'])
        self.assertEqual(result['verification'],'model-with-validated-quotes')

    def test_batch_verification_processes_twenty_and_reports_incremental_progress(self):
        s = self.session()
        records = []
        for i in range(20):
            row = self.record('openalex', f'Distributed flexible job shop scheduling paper {i}', f'10.1000/batch-{i}')
            row['sourceId'] = f'batch-{i}'
            records.append(row)
        self.app.ai_search._source_records = lambda *a: records
        self.run_sync(s)
        ids = [row['id'] for row in self.app.ai_search.results({'sessionId': s['id'], 'limit': 20})['items']]
        self.app.assistant.status = lambda: {'ready': True}
        with self.assertRaises(AppError):
            self.app.ai_search.submit_verify({'candidateIds': ids + ['extra'], 'allowRemote': True})
        def answer(context):
            source = context['evidence'][0]
            ref = {'evidenceId': source['id'], 'quote': source['text']}
            return {'checks': [], 'summaryZh': {'text': '题名描述柔性作业车间调度', 'references': [ref]}}
        self.app.assistant.search_verify = answer
        updates = []
        result = self.app.ai_search.verify({'sessionId': s['id'], 'candidateIds': ids, 'includeFulltext': False},
                                           lambda value, message: updates.append((value, message)))
        self.assertEqual(result['verified'], 20)
        self.assertEqual(len(result['failed']), 0)
        self.assertTrue(any('10/20' in message for _, message in updates))
        self.assertEqual(updates[-1][0], 1)
        self.assertEqual(sum(bool(self.app.ai_search.evidence(cid)['fields'].get('summaryZh')) for cid in ids), 20)

    def test_year_adapter_and_surrogate_mapping(self):
        self.assertEqual(_year({'date-parts':[[2024,1]]}),2024)
        s=self.session('多车间、多目标柔性作业车间调度，重点关注代理模型与计算效率')
        query=s['plan']['queries'][0]['query']
        self.assertIn('surrogate model',query)
        self.assertNotIn('agent model',query)

    def test_chinese_agv_review_plan_uses_short_english_queries_and_transport_condition(self):
        s = self.session('请检索柔性作业车间与自动导引车（AGV）联合调度的综述论文，关注运输资源和计算效率。')
        queries = [item['query'] for item in s['plan']['queries']]
        self.assertEqual(len(queries), 4)
        self.assertTrue(all(len(query) < 100 and query.isascii() for query in queries))
        self.assertTrue(any('AGV' in query or 'guided vehicle' in query for query in queries))
        self.assertTrue(any('review' in query or 'survey' in query for query in queries))
        self.assertIn('transport', {item['id'] for item in s['plan']['criteria']})

    def test_cancelled_verification_waits_for_worker_and_discards_response(self):
        s=self.session()
        self.app.ai_search._source_records=lambda *a:[self.record('openalex')]
        self.run_sync(s)
        cid=self.app.ai_search.results({'sessionId':s['id']})['items'][0]['id']
        self.app.assistant.status=lambda:{'ready':True}
        started,release=threading.Event(),threading.Event()
        def model(context):
            started.set(); release.wait(3)
            return {}
        self.app.assistant.search_verify=model
        job=self.app.ai_search.submit_verify({'candidateIds':[cid],'allowRemote':True})
        self.assertTrue(started.wait(3))
        self.app.ai_search.cancel({'sessionId':s['id']})
        self.assertEqual(self.app.ai_search.get(s['id'])['state'],'verifying')
        with self.assertRaises(AppError): self.app.ai_search.submit({'sessionId':s['id']})
        release.set()
        for _ in range(100):
            if self.app.ai_search.get(s['id'])['state']!='verifying': break
            time.sleep(.02)
        self.assertEqual(self.app.ai_search.get(s['id'])['state'],'completed')
        self.assertFalse(self.app.ai_search.evidence(cid)['verification'].startswith('model'))

    def test_pdf_evidence_is_bounded_page_anchored_and_opt_in_each_time(self):
        s=self.session()
        self.app.ai_search._source_records=lambda *a:[self.record('openalex')]
        self.run_sync(s)
        cid=self.app.ai_search.results({'sessionId':s['id']})['items'][0]['id']
        item=self.app.ai_search.import_candidates({'candidateIds':[cid]})['imported'][0]['itemId']
        with self.app.library.db(True) as db:
            db.execute("INSERT INTO objects VALUES('obj','hash',0,'fixture.pdf','managed','application/pdf','2026-01-01')")
            db.execute("INSERT INTO attachments(id,item_id,object_id,name,version,text_status,page_count,text_json,created_at) VALUES('pdf',?,'obj','fixture.pdf','v1','indexed',50,?,'2026-01-01')",(item,json.dumps(['Private PDF page '+str(i)+' '+('x'*6000) for i in range(50)])))
        ev,note=self.app.ai_search._pdf_evidence(self.app.ai_search.evidence(cid))
        self.assertEqual(sum(len(e['text']) for e in ev),30000)
        self.assertEqual(ev[0]['page'],1)
        self.assertEqual(ev[-1]['page'],6)
        self.assertEqual(ev[0]['attachmentId'],'pdf')
        contexts=[]
        self.app.assistant.search_verify=lambda context: contexts.append(context) or {}
        for include in (True,False):
            self.app.ai_search.verify({'sessionId':s['id'],'candidateIds':[cid],'includeFulltext':include},lambda *a:None)
        self.assertTrue(any(e['kind']=='fulltext' for e in contexts[0]['evidence']))
        self.assertFalse(any(e['kind']=='fulltext' for e in contexts[1]['evidence']))


if __name__=='__main__': unittest.main()
