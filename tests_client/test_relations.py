import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from uuid import uuid4
from pathlib import Path

from client_backend.service import Application


class RelationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name) / 'library')
        self.first = self.app.library.create({
            'title': 'Evidence-aware scheduling methods for flexible manufacturing',
            'author': [{'family': 'Lin'}], 'issued': {'date-parts': [[2024]]},
            'abstract': 'This study investigates flexible manufacturing scheduling. We compare a genetic algorithm with dispatching rules on energy use and completion time. The results show lower energy use, while the limited benchmark set requires further validation.'
        })
        self.second = self.app.library.create({
            'title': 'Multi-objective optimization for energy efficient flexible job shops',
            'author': [{'family': 'Chen'}], 'issued': {'date-parts': [[2025]]},
            'abstract': 'We study flexible job shop scheduling with multi-objective optimization. The method evaluates energy consumption and makespan using industrial benchmark data. Results suggest an efficient schedule, but sample coverage remains limited.'
        })

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def wait_job(self, job_id):
        for _ in range(150):
            job = next(value for value in self.app.jobs.list() if value['id'] == job_id)
            if job['state'] == 'completed':
                return job
            if job['state'] in ('failed', 'cancelled'):
                self.fail(str(job))
            time.sleep(.03)
        self.fail('relation job timed out')

    def attach_indexed_pages(self, item_id, pages):
        attachment_id, object_id = str(uuid4()), str(uuid4())
        with self.app.library.db(True) as db:
            db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
                       (object_id, str(uuid4()), 0, 'synthetic.pdf', 'managed', 'application/pdf', '2026-01-01'))
            db.execute('''INSERT INTO attachments(id,item_id,object_id,name,version,text_status,page_count,text_json,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (attachment_id, item_id, object_id, 'synthetic.pdf', 'synthetic-v1', 'indexed', len(pages), json.dumps(pages), '2026-01-01'))
        return attachment_id

    def test_local_profiles_relations_evidence_confirmation_export_and_stale_state(self):
        profile_job = self.app.call('relations.profile.run', {'itemIds': [self.first['id'], self.second['id']]})
        self.wait_job(profile_job['jobId'])
        profile = self.app.call('relations.profile.get', {'itemId': self.first['id']})
        self.assertEqual(profile['state'], 'ready')
        self.assertTrue(profile['profile']['keywords'])
        self.assertTrue(profile['evidence'])

        created = self.app.call('relations.create', {
            'itemIds': [self.first['id'], self.second['id']], 'mode': 'compare',
            'purpose': '比较节能调度方法和证据范围', 'remoteAI': False,
        })
        self.assertEqual(created['state'], 'planned')
        queued = self.app.call('relations.run', {'sessionId': created['id']})
        self.wait_job(queued['jobId'])
        session = self.app.call('relations.get', {'sessionId': created['id']})
        self.assertEqual(session['state'], 'completed')
        self.assertEqual(len(session['items']), 2)
        rows = self.app.call('relations.results', {'sessionId': created['id']})
        self.assertTrue(rows)
        self.assertEqual(rows[0]['engine'], 'local')
        detail = self.app.call('relations.evidence', {'relationId': rows[0]['id']})
        self.assertGreaterEqual(len({value['itemId'] for value in detail['evidence']}), 2)

        confirmed = self.app.call('relations.confirm', {'relationId': rows[0]['id'], 'status': 'confirmed', 'userNote': '保留为方法比较线索'})
        self.assertEqual(confirmed['status'], 'confirmed')
        note = self.app.call('relations.export', {'sessionId': created['id']})
        self.assertIn('关联综述', note['title'])
        self.assertIn('已确认关联', note['content'])

        self.app.library.update(self.second['id'], {'abstract': self.second['abstract'] + ' Updated local material.'}, self.second['revision'])
        stale = self.app.call('relations.get', {'sessionId': created['id']})
        self.assertEqual(stale['state'], 'stale')

    def test_explicit_remote_mode_requires_saved_evidence_ids(self):
        class EvidenceAssistant:
            def __init__(self):
                self.context = None

            def status(self):
                return {'ready': True}

            def relation_json(self, context):
                self.context = context
                first, second = context['papers']
                return {'relations': [{
                    'leftItemId': first['itemId'], 'rightItemId': second['itemId'], 'type': 'method_compare', 'confidence': .77,
                    'rationale': '仅根据两篇文献提供的方法与结果片段作比较。',
                    'evidence': [{'evidenceId': first['evidence'][0]['id']}, {'evidenceId': second['evidence'][0]['id']}],
                }], 'synthesis': '基于所给证据的受限总结。'}

        assistant = EvidenceAssistant()
        self.app.relations.assistant = assistant
        created = self.app.call('relations.create', {'itemIds': [self.first['id'], self.second['id']], 'mode': 'compare', 'remoteAI': True})
        queued = self.app.call('relations.run', {'sessionId': created['id']})
        self.wait_job(queued['jobId'])
        rows = self.app.call('relations.results', {'sessionId': created['id']})
        self.assertEqual(rows[0]['engine'], 'remote-evidence')
        self.assertEqual(len(assistant.context['papers']), 2)
        detail = self.app.call('relations.evidence', {'relationId': rows[0]['id']})
        self.assertEqual(len({value['itemId'] for value in detail['evidence']}), 2)

    def test_rerun_keeps_decision_and_note_then_requires_review_if_evidence_changes(self):
        session = self.app.call('relations.create', {'itemIds': [self.first['id'], self.second['id']]})
        self.wait_job(self.app.call('relations.run', {'sessionId': session['id']})['jobId'])
        original = self.app.call('relations.results', {'sessionId': session['id']})[0]
        self.app.call('relations.confirm', {'relationId': original['id'], 'status': 'confirmed', 'userNote': 'Keep my judgement'})
        self.wait_job(self.app.call('relations.run', {'sessionId': session['id']})['jobId'])
        same = self.app.call('relations.results', {'sessionId': session['id']})[0]
        self.assertEqual((same['id'], same['status'], same['userNote'], same['reviewState']),
                         (original['id'], 'confirmed', 'Keep my judgement', 'current'))
        changed_abstract = self.second['abstract'].replace('energy consumption and makespan', 'electricity emissions and makespan')
        self.app.library.update(self.second['id'], {'abstract': changed_abstract}, self.second['revision'])
        self.wait_job(self.app.call('relations.run', {'sessionId': session['id']})['jobId'])
        changed = self.app.call('relations.results', {'sessionId': session['id']})[0]
        self.assertEqual((changed['id'], changed['status'], changed['userNote'], changed['reviewState']),
                         (original['id'], 'confirmed', 'Keep my judgement', 'changed'))
        self.assertIn('待复核的原有判断', self.app.call('relations.export', {'sessionId': session['id']})['content'])
        self.assertEqual(self.app.call('relations.diff', {'sessionId': session['id']})['changes'][0]['state'], 'changed')
        history = self.app.call('relations.runs', {'sessionId': session['id']})
        self.assertEqual(len(history), 3)
        reviewed = self.app.call('relations.confirm', {'relationId': original['id'], 'status': 'confirmed', 'userNote': 'Reviewed new evidence'})
        self.assertEqual(reviewed['reviewState'], 'current')
        self.assertIn('Reviewed new evidence', self.app.call('relations.export', {'sessionId': session['id']})['content'])

    def test_remote_relation_rejects_evidence_from_other_two_papers(self):
        third = self.app.library.create({'title': 'Third flexible scheduling paper', 'abstract': self.first['abstract']})
        fourth = self.app.library.create({'title': 'Fourth flexible scheduling paper', 'abstract': self.second['abstract']})

        class WrongAssistant:
            def status(self):
                return {'ready': True}

            def relation_json(self, context):
                a, b, c, d = context['papers']
                return {'relations': [{'leftItemId': a['itemId'], 'rightItemId': b['itemId'], 'type': 'method_compare',
                                       'evidence': [{'evidenceId': c['evidence'][0]['id']}, {'evidenceId': d['evidence'][0]['id']}]}],
                        'synthesis': 'Unsupported remote conclusion'}

        self.app.relations.assistant = WrongAssistant()
        session = self.app.call('relations.create', {'itemIds': [self.first['id'], self.second['id'], third['id'], fourth['id']], 'remoteAI': True})
        self.wait_job(self.app.call('relations.run', {'sessionId': session['id']})['jobId'])
        rows = self.app.call('relations.results', {'sessionId': session['id']})
        self.assertTrue(rows)
        self.assertTrue(all(row['engine'] == 'local' for row in rows))
        self.assertNotIn('Unsupported remote conclusion', self.app.call('relations.get', {'sessionId': session['id']})['synthesis']['summary'])

    def test_pdf_page_enters_profile_and_export_links_to_source(self):
        pdf_paper = self.app.library.create({'title': 'Full-text-only surrogate scheduling paper', 'abstract': ''})
        attachment_id = self.attach_indexed_pages(pdf_paper['id'], [
            'Introduction to the flexible job shop scheduling problem.',
            'Methods: We use a surrogate evaluation method to reduce expensive evaluations in distributed flexible shops.'
        ])
        self.app.relations._profile(pdf_paper['id'])
        profile = self.app.call('relations.profile.get', {'itemId': pdf_paper['id']})
        pdf_evidence = next(row for row in profile['evidence'] if row['field'] == 'methods' and row['sourceType'] == 'pdf')
        self.assertEqual((pdf_evidence['attachmentId'], pdf_evidence['page']), (attachment_id, 2))
        self.assertIn('surrogate evaluation method', profile['profile']['fields']['methods'])

        class PdfAssistant:
            def status(self):
                return {'ready': True}

            def relation_json(self, context):
                first, second = context['papers']
                pdf_quote = next(row for row in first['evidence'] if row['page'] == 2)
                return {'relations': [{'leftItemId': first['itemId'], 'rightItemId': second['itemId'], 'type': 'method_compare',
                                       'evidence': [{'evidenceId': pdf_quote['id']}, {'evidenceId': second['evidence'][0]['id']}]}]}

        self.app.relations.assistant = PdfAssistant()
        session = self.app.call('relations.create', {'itemIds': [pdf_paper['id'], self.second['id']], 'remoteAI': True})
        self.wait_job(self.app.call('relations.run', {'sessionId': session['id']})['jobId'])
        relation = self.app.call('relations.results', {'sessionId': session['id']})[0]
        self.assertEqual(relation['engine'], 'remote-evidence')
        self.app.call('relations.confirm', {'relationId': relation['id'], 'status': 'confirmed'})
        exported = self.app.call('relations.export', {'sessionId': session['id']})['content']
        self.assertIn(f'research://attachment/{attachment_id}?page=2', exported)
        self.assertIn('surrogate evaluation method', exported)

    def test_existing_library_upgrades_with_recoverable_database_backup(self):
        root = Path(self.temp.name) / 'library'
        self.app.close()
        with closing(sqlite3.connect(root / 'library.sqlite3')) as db:
            db.execute('DROP TABLE relation_history')
            db.execute('DROP TABLE relation_reviews')
            db.execute('DROP TABLE relation_runs')
            db.execute('PRAGMA user_version=1')
            db.commit()
        self.app = Application(root)
        self.assertEqual(self.app.library.get(self.first['id'])['title'], self.first['title'])
        backup = root / 'migration-backups' / 'library-before-v2.sqlite3'
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM items').fetchone()[0], 2)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='relation_runs'").fetchone())
        with self.app.library.db() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertTrue(db.execute("SELECT 1 FROM sqlite_master WHERE name='relation_runs'").fetchone())


if __name__ == '__main__':
    unittest.main()
