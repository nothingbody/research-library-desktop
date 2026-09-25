import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from client_backend.common import AppError
from client_backend.service import Application


class AiSearchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name) / 'library')

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def wait_job(self, job_id):
        for _ in range(150):
            job = next(value for value in self.app.jobs.list() if value['id'] == job_id)
            if job['state'] in ('completed', 'failed', 'cancelled'):
                return job
            time.sleep(.02)
        self.fail('AI search job timed out')

    @staticmethod
    def record(source, title='Evidence-first scheduling', doi_value='10.1000/fixture'):
        return {
            'source': source, 'sourceId': source + '-1', 'title': title,
            'authors': [{'family': 'Wang', 'given': 'Mei'}], 'year': 2025,
            'venue': 'Journal of Test Research',
            'abstract': 'Flexible job shop scheduling uses machine learning for efficient manufacturing decisions.',
            'doi': doi_value, 'url': 'https://example.test/' + source,
            'citationCount': 28, 'oaUrl': 'https://example.test/fulltext', 'type': 'article-journal', 'raw': {'source': source},
        }

    def test_plan_async_dedup_evidence_filters_and_local_import(self):
        session = self.app.call('aiSearch.create', {
            'brief': 'Find machine learning methods for flexible job shop scheduling in manufacturing systems.',
            'sources': ['openalex', 'crossref'], 'mode': 'deep', 'useModel': False,
        })
        self.assertEqual(session['state'], 'planned')
        self.assertGreaterEqual(len(session['plan']['queries']), 1)
        self.app.ai_search._source_records = lambda source, queries, limit: [self.record(source)]
        job = self.app.call('aiSearch.run', {'sessionId': session['id']})
        completed = self.wait_job(job['jobId'])
        self.assertEqual(completed['state'], 'completed')
        detail = self.app.call('aiSearch.get', {'sessionId': session['id']})
        self.assertEqual(detail['state'], 'completed')
        self.assertEqual(detail['resultCount'], 1)
        self.assertEqual({row['source'] for row in detail['sourceRuns']}, {'openalex', 'crossref'})
        values = self.app.call('aiSearch.results', {'sessionId': session['id'], 'source': 'openalex'})
        self.assertEqual(values['total'], 1)
        candidate = values['items'][0]
        self.assertEqual(set(candidate['sources']), {'openalex', 'crossref'})
        self.assertEqual(candidate['tier'], 'core')
        evidence = self.app.call('aiSearch.evidence', {'candidateId': candidate['id']})
        self.assertIn('Flexible job shop', evidence['evidence'][1]['text'])
        first = self.app.call('aiSearch.import', {'candidateIds': [candidate['id']]})
        self.assertEqual(len(first['imported']), 1)
        self.assertEqual(self.app.call('library.stats', {})['items'], 1)
        again = self.app.call('aiSearch.import', {'candidateIds': [candidate['id']]})
        self.assertEqual(len(again['existing']), 1)
        self.assertEqual(self.app.call('library.stats', {})['items'], 1)

    def test_opening_paper_translates_and_caches_public_intro(self):
        session = self.app.call('aiSearch.create', {
            'brief': 'large language models for smart logistics review',
            'sources': ['openalex'], 'useModel': False,
        })
        record = self.record('openalex', title='Smart logistics: a review')
        record['keywords'] = ['smart logistics', 'language models']
        self.app.ai_search._source_records = lambda source, queries, limit: [record]
        job = self.app.call('aiSearch.run', {'sessionId': session['id']})
        self.assertEqual(self.wait_job(job['jobId'])['state'], 'completed')
        candidate = self.app.call('aiSearch.results', {'sessionId': session['id']})['items'][0]
        translated = {'titleZh': '智慧物流：一项综述', 'abstractZh': '柔性作业车间调度使用机器学习。',
                      'keywordsZh': ['智慧物流', '语言模型']}
        with patch.object(self.app.assistant, 'status', return_value={'ready': True}), \
                patch.object(self.app.assistant, 'search_intro', return_value=translated) as translate:
            first = self.app.call('aiSearch.intro', {'candidateId': candidate['id']})
            second = self.app.call('aiSearch.intro', {'candidateId': candidate['id']})
        self.assertEqual(first['introZh']['title'], translated['titleZh'])
        self.assertEqual(second['introZh']['keywords'], translated['keywordsZh'])
        translate.assert_called_once()

    def test_short_chinese_logistics_question_runs_without_plan_edit(self):
        session = self.app.call('aiSearch.create', {
            'brief': '大模型下的智慧物流发展综述', 'sources': ['openalex'], 'useModel': False,
        })
        queries = [row['query'] for row in session['plan']['queries']]
        self.assertTrue(any('large language models' in query for query in queries))
        self.assertEqual({'logistics-domain', 'large-language-model'},
                         {c['id'] for c in session['plan']['criteria'] if c['required']} & {'logistics-domain', 'large-language-model'})
        self.assertEqual(session['state'], 'planned')
        self.app.ai_search._source_records = lambda source, queries, limit: [self.record(source)]
        self.assertEqual(self.wait_job(self.app.call('aiSearch.run', {'sessionId': session['id']})['jobId'])['state'], 'completed')

    def test_logistics_topic_ranks_before_llm_paper_in_another_field(self):
        session = self.app.call('aiSearch.create', {
            'brief': '大模型下的智慧物流发展综述', 'sources': ['openalex'], 'useModel': False,
        })
        education = self.record('openalex', 'Large language models in education: a review', '10.1000/education')
        education.update(sourceId='education', abstract='Generative AI and LLM tools in education are reviewed.')
        logistics = self.record('openalex', 'Artificial intelligence in logistics: a review', '10.1000/logistics')
        logistics.update(sourceId='logistics', abstract='Logistics and supply chain practices are reviewed.')
        self.app.ai_search._source_records = lambda source, queries, limit: [education, logistics]
        self.assertEqual(self.wait_job(self.app.call('aiSearch.run', {'sessionId': session['id']})['jobId'])['state'], 'completed')
        rows = self.app.call('aiSearch.results', {'sessionId': session['id']})['items']
        self.assertEqual([row['doi'] for row in rows], ['10.1000/logistics', '10.1000/education'])

    def test_import_candidate_without_publication_year(self):
        session = self.app.call('aiSearch.create', {
            'brief': 'Find scheduling approaches with clear machine shop evidence.',
            'sources': ['openalex'], 'useModel': False,
        })
        record = self.record('openalex')
        record['year'] = None
        self.app.ai_search._source_records = lambda source, queries, limit: [record]
        self.assertEqual(self.wait_job(self.app.call('aiSearch.run', {'sessionId': session['id']})['jobId'])['state'], 'completed')
        candidate = self.app.call('aiSearch.results', {'sessionId': session['id']})['items'][0]
        imported = self.app.call('aiSearch.import', {'candidateIds': [candidate['id']]})
        self.assertEqual(len(imported['imported']), 1)
        item = self.app.call('items.get', {'id': imported['imported'][0]['itemId']})
        self.assertFalse(item['issued'])

    def test_import_existing_candidate_into_selected_collection(self):
        session = self.app.call('aiSearch.create', {
            'brief': 'Find scheduling papers and collect them for a review project.',
            'sources': ['openalex'], 'useModel': False,
        })
        self.app.ai_search._source_records = lambda source, queries, limit: [self.record(source)]
        self.assertEqual(self.wait_job(self.app.call('aiSearch.run', {'sessionId': session['id']})['jobId'])['state'], 'completed')
        candidate_id = self.app.call('aiSearch.results', {'sessionId': session['id']})['items'][0]['id']
        first = self.app.call('aiSearch.import', {'candidateIds': [candidate_id]})
        item_id = first['imported'][0]['itemId']
        collection = self.app.call('collections.edit', {'action': 'create', 'name': 'Review shortlist'})
        again = self.app.call('aiSearch.import', {'candidateIds': [candidate_id], 'collectionId': collection['id']})
        self.assertEqual(again['existing'][0]['itemId'], item_id)
        self.assertEqual(self.app.call('items.get', {'id': item_id})['collections'], [collection['id']])
        self.app.call('aiSearch.import', {'candidateIds': [candidate_id], 'collectionId': collection['id']})
        self.assertEqual(self.app.call('items.get', {'id': item_id})['collections'], [collection['id']])

    def test_plan_is_editable_and_online_switch_is_enforced(self):
        session = self.app.call('aiSearch.create', {
            'brief': 'Survey transparent evidence practices in academic discovery systems.',
            'sources': ['openalex'], 'useModel': False,
        })
        plan = session['plan']
        plan['queries'][0]['query'] = 'evidence provenance academic discovery'
        plan['sources'] = ['openalex', 'arxiv']
        saved = self.app.call('aiSearch.savePlan', {'sessionId': session['id'], 'plan': plan})
        self.assertEqual(saved['plan']['queries'][0]['query'], 'evidence provenance academic discovery')
        self.assertEqual(saved['sources'], ['openalex', 'arxiv'])
        self.app.call('settings.save', {'online': False})
        with self.assertRaises(AppError):
            self.app.call('aiSearch.run', {'sessionId': session['id']})

    def test_source_pagination_resumes_without_losing_existing_results(self):
        session = self.app.call('aiSearch.create', {
            'brief': 'Find scheduling approaches with clear machine shop evidence.',
            'sources': ['openalex'], 'useModel': False,
        })
        plan = session['plan']
        plan['queries'] = plan['queries'][:1]
        self.app.call('aiSearch.savePlan', {'sessionId': session['id'], 'plan': plan})
        calls = []

        def records(source, queries, limit, offset=0):
            calls.append((source, offset, limit))
            values = []
            for number in range(offset, offset + (limit if offset == 0 else 1)):
                value = self.record(source, f'Scheduling paper {number}', f'10.1000/page{number}')
                value['sourceId'] = f'paper-{number}'
                values.append(value)
            return values

        self.app.ai_search._source_records = records
        first = self.app.call('aiSearch.run', {'sessionId': session['id']})
        self.assertEqual(self.wait_job(first['jobId'])['state'], 'completed')
        before = self.app.call('aiSearch.results', {'sessionId': session['id']})
        self.assertEqual(before['total'], 16)
        self.assertTrue(before['sourceCursors'][0]['hasMore'])
        next_page = self.app.call('aiSearch.expand', {'sessionId': session['id']})
        self.assertEqual(self.wait_job(next_page['jobId'])['state'], 'completed')
        after = self.app.call('aiSearch.results', {'sessionId': session['id']})
        self.assertEqual(after['total'], 17)
        self.assertFalse(after['sourceCursors'][0]['hasMore'])
        self.assertEqual([value[1] for value in calls], [0, 16])
        self.assertEqual(self.app.call('aiSearch.get', {'sessionId': session['id']})['activeRunId'],
                         self.app.call('aiSearch.get', {'sessionId': session['id']})['runs'][0]['id'])

    def test_quality_report_counts_only_manual_labels(self):
        session = self.app.call('aiSearch.create', {
            'brief': 'Find journal papers about flexible job shop scheduling.',
            'sources': ['crossref'], 'useModel': False,
        })
        self.app.ai_search._source_records = lambda source, queries, limit: [self.record(source)]
        job = self.app.call('aiSearch.run', {'sessionId': session['id']})
        self.assertEqual(self.wait_job(job['jobId'])['state'], 'completed')
        report = self.app.call('searchEvaluation.report', {'sessionId': session['id']})
        self.assertEqual(report['labelled'], 0)
        self.assertIsNone(report['relevantAmongLabelled'])
        candidate_id = report['items'][0]['id']
        saved = self.app.call('searchEvaluation.save', {'sessionId': session['id'],
            'candidateId': candidate_id, 'grade': 2, 'hardMatch': 'pass', 'note': '主题吻合'})
        self.assertEqual(saved['labelled'], 1)
        self.assertEqual(saved['relevantAmongLabelled'], 1)
        self.assertEqual(saved['hardPassAmongLabelled'], 1)
        with self.assertRaises(AppError):
            self.app.call('searchEvaluation.save', {'sessionId': session['id'],
                'candidateId': 'wrong', 'grade': 3})

    def test_openalex_citation_chain_records_direction_and_candidates(self):
        session = self.app.call('aiSearch.create', {
            'brief': 'Find evidence on flexible job shop scheduling methods.',
            'sources': ['openalex'], 'useModel': False,
        })
        anchor = self.record('openalex', 'Anchor scheduling study', '10.1000/anchor')
        anchor['sourceId'] = 'https://openalex.org/W123456'
        self.app.ai_search._source_records = lambda source, queries, limit: [anchor]
        job = self.app.call('aiSearch.run', {'sessionId': session['id']})
        self.assertEqual(self.wait_job(job['jobId'])['state'], 'completed')
        first = self.app.call('aiSearch.results', {'sessionId': session['id']})['items'][0]
        urls = []

        def fake_json(url, source):
            urls.append(url)
            return {'results': [{'id': 'https://openalex.org/W789012',
                                 'title': 'Cited scheduling method', 'doi': 'https://doi.org/10.1000/cited',
                                 'publication_year': 2024, 'authorships': [], 'primary_location': {},
                                 'cited_by_count': 3, 'open_access': {}, 'type': 'article'}]}

        self.app.ai_search._json = fake_json
        expansion = self.app.call('aiSearch.citationExpand', {'sessionId': session['id'],
            'candidateId': first['id'], 'direction': 'references'})
        self.assertEqual(self.wait_job(expansion['jobId'])['state'], 'completed')
        self.assertIn('cited_by%3AW123456', urls[0])
        links = self.app.call('aiSearch.citationLinks', {'sessionId': session['id'], 'candidateId': first['id']})
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]['direction'], 'references')
        self.assertEqual(links[0]['source'], 'openalex')
        self.assertEqual(links[0]['doi'], '10.1000/cited')
        self.assertEqual(links[0]['year'], 2024)
        self.assertTrue(links[0]['available'])
        self.assertEqual(self.app.call('aiSearch.results', {'sessionId': session['id']})['total'], 2)

    def test_semantic_rerank_requires_quote_from_candidate(self):
        session = self.app.call('aiSearch.create', {
            'brief': 'Find relevant flexible job shop scheduling studies.',
            'sources': ['openalex'], 'useModel': False,
        })
        self.app.ai_search._source_records = lambda source, queries, limit: [
            self.record(source, 'Verified scheduling evidence', '10.1000/semantic')]
        job = self.app.call('aiSearch.run', {'sessionId': session['id']})
        self.assertEqual(self.wait_job(job['jobId'])['state'], 'completed')
        candidate = self.app.call('aiSearch.results', {'sessionId': session['id']})['items'][0]
        self.app.assistant.search_rerank_json = lambda context: {'scores': [
            {'candidateId': candidate['id'], 'score': .92, 'quote': 'Verified scheduling evidence', 'reason': '方法匹配'},
            {'candidateId': 'wrong', 'score': 1, 'quote': 'fabricated quote', 'reason': 'invalid'},
        ]}
        rerank_job = self.app.call('aiSearch.rerank', {'sessionId': session['id']})
        self.assertEqual(self.wait_job(rerank_job['jobId'])['state'], 'completed')
        result = self.app.call('aiSearch.results', {'sessionId': session['id'], 'sort': 'semantic'})
        self.assertEqual(result['semanticCount'], 1)
        self.assertEqual(result['items'][0]['semantic']['quote'], 'Verified scheduling evidence')


if __name__ == '__main__':
    unittest.main()
