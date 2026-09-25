import tempfile
import time
import unittest
from pathlib import Path

from client_backend.service import Application
from tests_client.test_backend import fixture_pdf


class WorkflowReleaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name) / 'library')

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def done(self, job_id):
        for _ in range(150):
            job = next(value for value in self.app.jobs.list() if value['id'] == job_id)
            if job['state'] in ('completed', 'failed', 'cancelled'):
                self.assertEqual(job['state'], 'completed', str(job))
                return job
            time.sleep(.03)
        self.fail('background job did not finish')

    def test_search_collect_pdf_annotate_ask_relate_and_export(self):
        session = self.app.call('aiSearch.create', {'brief': 'Compare scheduling methods across flexible manufacturing papers.',
                                                    'sources': ['openalex'], 'useModel': False})

        def records(source, queries, limit):
            return [dict(source=source, sourceId=f'https://openalex.org/W{n}',
                         title=f'Flexible manufacturing scheduling study {n}', authors=[], year=2025,
                         venue='Test Journal', abstract=f'Flexible manufacturing scheduling study {n} compares machine assignments and completion time.',
                         doi=f'10.1000/workflow{n}', url=f'https://example.test/{n}', citationCount=0,
                         oaUrl=f'https://example.test/{n}.pdf', type='article', raw={'id': n}) for n in (123, 456)]

        self.app.ai_search._source_records = records
        self.done(self.app.call('aiSearch.run', {'sessionId': session['id']})['jobId'])
        candidates = self.app.call('aiSearch.results', {'sessionId': session['id']})['items']
        imported = self.app.call('aiSearch.import', {'candidateIds': [value['id'] for value in candidates]})
        item_ids = [row['itemId'] for row in imported['imported']]
        self.assertEqual(len(item_ids), 2)
        fixture = Path(self.temp.name) / 'verification.pdf'
        fixture_pdf(fixture)

        def attach(payload, progress):
            record = self.app.attachments.add(payload['itemId'], fixture)
            self.app.jobs.create('pdf.index', {'attachmentId': record['id']})
            return {'attachmentId': record['id']}

        self.app.downloads.pdf = attach
        self.app.fulltext._resolve = lambda source, progress: (source['url'], 'pdf')
        source = self.app.call('fulltext.sources', {'itemId': item_ids[0]})[0]
        obtained = self.done(self.app.call('fulltext.obtain', {'itemId': item_ids[0], 'sourceId': source['id']})['jobId'])
        first_attachment = obtained['result']['attachmentId']
        second_attachment = self.app.attachments.add(item_ids[1], fixture)['id']
        self.done(self.app.jobs.create('pdf.index', {'attachmentId': second_attachment})['jobId'])
        info = self.app.call('attachments.get', {'id': first_attachment})
        self.app.call('annotations.save', {'attachmentId': first_attachment, 'data': {
            'type': 'highlight', 'version': info['version'], 'pageIndex': 0, 'pageLabel': '1',
            'rects': [[10, 10, 90, 25]], 'quote': 'Attention connects queries, keys, and values.',
            'comment': 'Compare with the second paper', 'color': '#f6d766'}})
        self.assertEqual(self.app.call('annotations.search', {'q': 'Attention'})['total'], 1)

        class Assistant:
            def research_json(self, context):
                sources = {}
                for row in context['sources']:
                    if row['sourceType'] == 'abstract':
                        sources[row['itemId']] = row
                return {'claims': [{'text': 'Both articles discuss flexible manufacturing scheduling.',
                                    'citations': [{'chunkId': row['id'], 'quote': row['quote'][:80]}
                                                  for row in sources.values()]}]}

        self.app.research_ask.assistant = Assistant()
        conversation = self.app.call('researchAsk.create', {'itemIds': item_ids})
        self.done(self.app.call('researchAsk.send', {'conversationId': conversation['id'],
                                                     'question': 'flexible manufacturing scheduling',
                                                     'sourceTypes': ['abstract']})['jobId'])
        messages = self.app.call('researchAsk.get', {'conversationId': conversation['id']})['messages']
        self.assertEqual(len(messages[-1]['result']['claims'][0]['citations']), 2)
        note = self.app.call('researchAsk.saveClaim', {'messageId': messages[-1]['id'], 'claimIndex': 0})
        self.assertIn('待核对', note['title'])
        relation = self.app.call('relations.create', {'itemIds': item_ids})
        self.done(self.app.call('relations.run', {'sessionId': relation['id']})['jobId'])
        rows = self.app.call('relations.results', {'sessionId': relation['id']})
        self.assertTrue(rows)
        self.app.call('relations.confirm', {'relationId': rows[0]['id'], 'status': 'confirmed'})
        exported = self.app.call('relations.export', {'sessionId': relation['id']})
        self.assertIn('已确认关联', exported['content'])


if __name__ == '__main__':
    unittest.main()
