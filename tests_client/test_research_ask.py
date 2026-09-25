import json
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

from client_backend.common import AppError
from client_backend.service import Application


class ResearchAskTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name) / 'library')
        self.first = self.app.library.create({'title': 'Distributed scheduling paper', 'abstract': ''})
        self.second = self.app.library.create({'title': 'Surrogate scheduling comparison',
                                               'abstract': 'Surrogate evaluation scheduling reduces simulation effort in flexible manufacturing.'})
        self.attachment_id, object_id = str(uuid4()), str(uuid4())
        with self.app.library.db(True) as db:
            db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
                       (object_id, str(uuid4()), 0, 'fixture.pdf', 'managed', 'application/pdf', '2026-01-01'))
            db.execute('''INSERT INTO attachments(id,item_id,object_id,name,version,text_status,page_count,text_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (self.attachment_id, self.first['id'], object_id, 'fixture.pdf', 'v1', 'indexed', 2,
                 json.dumps(['Background to distributed scheduling.',
                             'Methods: Our surrogate evaluation scheduling approach reduces expensive simulations.']), '2026-01-01'))

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def wait(self, job_id):
        for _ in range(150):
            job = next(row for row in self.app.jobs.list() if row['id'] == job_id)
            if job['state'] in ('completed', 'failed', 'cancelled'):
                return job
            time.sleep(.03)
        self.fail('research answer timed out')

    def test_pdf_only_page_is_found_and_reindex_removes_stale_text(self):
        result = self.app.call('documents.search', {'itemIds': [self.first['id']], 'question': 'surrogate evaluation scheduling'})
        self.assertGreater(result['totalMatches'], 0)
        evidence = result['chunks'][0]
        self.assertEqual((evidence['sourceType'], evidence['attachmentId'], evidence['page']), ('pdf', self.attachment_id, 2))
        old_id = evidence['id']
        with self.app.library.db(True) as db:
            db.execute('UPDATE attachments SET version=?,text_json=? WHERE id=?',
                       ('v2', json.dumps(['Background.', 'A revised dispatch rule has replaced the earlier method.']), self.attachment_id))
        again = self.app.call('documents.search', {'itemIds': [self.first['id']], 'question': 'surrogate evaluation scheduling'})
        self.assertFalse(again['chunks'])
        with self.app.library.db() as db:
            self.assertFalse(db.execute('SELECT 1 FROM document_chunks WHERE id=?', (old_id,)).fetchone())

    def test_answer_keeps_only_quotes_from_selected_papers_and_reopens(self):
        class Assistant:
            def research_json(self, context):
                pdf = next(row for row in context['sources'] if row['sourceType'] == 'pdf')
                abstract = next(row for row in context['sources'] if row['sourceType'] == 'abstract')
                return {'claims': [
                    {'text': 'Two papers discuss surrogate evaluation.', 'citations': [
                        {'chunkId': pdf['id'], 'quote': 'surrogate evaluation scheduling approach reduces expensive simulations'},
                        {'chunkId': abstract['id'], 'quote': 'Surrogate evaluation scheduling reduces simulation effort'}]},
                    {'text': 'Invented unsupported result.', 'citations': [{'chunkId': pdf['id'], 'quote': 'invented numerical improvement'}]},
                ], 'insufficient': 'Neither excerpt establishes benchmark superiority.'}

        self.app.research_ask.assistant = Assistant()
        conversation = self.app.call('researchAsk.create', {'itemIds': [self.first['id'], self.second['id']]})
        sent = self.app.call('researchAsk.send', {'conversationId': conversation['id'], 'question': 'surrogate evaluation scheduling'})
        self.assertEqual(self.wait(sent['jobId'])['state'], 'completed')
        reopened = self.app.call('researchAsk.get', {'conversationId': conversation['id']})
        answer = reopened['messages'][-1]
        self.assertEqual(answer['status'], 'completed')
        self.assertEqual((len(answer['result']['claims']), answer['result']['droppedClaims']), (1, 1))
        citations = answer['result']['claims'][0]['citations']
        self.assertEqual({value['itemId'] for value in citations}, {self.first['id'], self.second['id']})
        self.assertTrue(any(value['attachmentId'] == self.attachment_id and value['page'] == 2 for value in citations))
        self.assertIn('Neither excerpt', answer['result']['insufficient'])
        note = self.app.call('researchAsk.saveClaim', {'messageId': answer['id'], 'claimIndex': 0})
        self.assertIn('research://attachment/' + self.attachment_id + '?page=2', note['content'])
        self.assertIn('待核对', note['tags'])
        with self.app.library.db(True) as db:
            db.execute('UPDATE attachments SET version=? WHERE id=?', ('v2', self.attachment_id))
        with self.assertRaises(AppError):
            self.app.call('researchAsk.saveClaim', {'messageId': answer['id'], 'claimIndex': 0})

    def test_no_matching_evidence_does_not_call_model(self):
        class Assistant:
            def research_json(self, _):
                raise AssertionError('model must not run without evidence')

        self.app.research_ask.assistant = Assistant()
        conversation = self.app.call('researchAsk.create', {'itemIds': [self.first['id']]})
        sent = self.app.call('researchAsk.send', {'conversationId': conversation['id'], 'question': 'quantum neutrino oscillation'})
        self.assertEqual(self.wait(sent['jobId'])['state'], 'completed')
        answer = self.app.call('researchAsk.get', {'conversationId': conversation['id']})['messages'][-1]
        self.assertFalse(answer['result']['claims'])
        self.assertIn('没有找到', answer['result']['insufficient'])

    def test_chinese_question_covers_selected_english_papers(self):
        result = self.app.call('documents.search', {'itemIds': [self.first['id'], self.second['id']],
                                                    'question': '这两篇文献分别研究了什么调度问题？请比较。',
                                                    'sourceTypes': ['abstract', 'pdf']})
        self.assertEqual({chunk['itemId'] for chunk in result['chunks']}, {self.first['id'], self.second['id']})
        self.assertTrue(all(chunk['quote'] for chunk in result['chunks']))


if __name__ == '__main__':
    unittest.main()
