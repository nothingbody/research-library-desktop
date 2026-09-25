import tempfile
import time
import unittest
import json
from pathlib import Path

from client_backend.service import Application


SEED_ID = 'https://openalex.org/W100'
REF_ID = 'https://openalex.org/W200'


def work(work_id, title, doi='', references=None):
    return {'id': work_id, 'title': title, 'doi': doi, 'publication_year': 2024,
            'authorships': [{'author': {'display_name': 'A. Researcher'}}], 'primary_location': {'source': {'display_name': 'Test Journal'}},
            'open_access': {}, 'best_oa_location': {}, 'cited_by_count': 5, 'type': 'article',
            'referenced_works': references or [], 'related_works': []}


class RelationDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name) / 'library')
        self.seed = self.app.library.create({'title': 'Seed scheduling study', 'DOI': '10.1234/seed',
                                             'issued': {'date-parts': [[2024]]}})
        other = self.app.library.create({'title': 'Other scheduling study'})
        self.session = self.app.call('relations.create', {'itemIds': [self.seed['id'], other['id']]})
        self.seed_work = work(SEED_ID, 'Seed scheduling study', 'https://doi.org/10.1234/seed', [REF_ID, 'https://openalex.org/W201'])
        self.reference = work(REF_ID, 'Reference scheduling study', 'https://doi.org/10.1234/reference')

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def wait(self, job_id):
        for _ in range(100):
            job = next(row for row in self.app.jobs.list() if row['id'] == job_id)
            if job['state'] in ('completed', 'failed'):
                self.assertEqual(job['state'], 'completed', job)
                return job
            time.sleep(.025)
        self.fail('discovery timed out')

    def test_reference_provenance_dedup_and_review(self):
        queries = []
        def fake_json(url, source):
            queries.append(url)
            if 'doi%3A10.1234%2Fseed' in url:
                return {'results': [self.seed_work]}
            if 'openalex_id%3AW200' in url:
                return {'results': [self.reference]}
            if 'openalex_id%3AW201' in url:
                return {'results': [work('https://openalex.org/W201', 'Another reference')]}
            raise AssertionError(url)
        self.app.ai_search._json = fake_json
        job = self.app.call('relations.discover', {'sessionId': self.session['id'], 'itemId': self.seed['id'], 'direction': 'references', 'limit': 1})
        self.wait(job['jobId'])
        rows = self.app.call('relations.discoveries', {'sessionId': self.session['id']})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['sourceUrl'], 'https://openalex.org/W200')
        self.assertEqual(rows[0]['direction'], 'references')
        self.assertIsNone(rows[0]['importedItemId'])
        self.app.call('relations.discoveryDecide', {'sessionId': self.session['id'], 'itemId': self.seed['id'],
                                                    'workId': 'W200', 'direction': 'references', 'status': 'later',
                                                    'reason': 'First read the seed paper'})
        later = self.app.call('relations.discoveries', {'sessionId': self.session['id']})[0]
        self.assertEqual((later['status'], later['decisionReason']), ('later', 'First read the seed paper'))
        saved = self.app.call('relations.discoveryDecide', {'sessionId': self.session['id'], 'itemId': self.seed['id'],
                                                            'workId': 'W200', 'direction': 'references', 'status': 'saved'})
        self.assertTrue(saved['itemId'])
        self.assertEqual(len(self.app.call('relations.get', {'sessionId': self.session['id']})['items']), 3)
        self.wait(self.app.call('relations.discover', {'sessionId': self.session['id'], 'itemId': self.seed['id'],
                                                       'direction': 'references', 'limit': 1})['jobId'])
        rows = self.app.call('relations.discoveries', {'sessionId': self.session['id']})
        self.assertEqual(len(rows), 2)
        retained = next(row for row in rows if row['workId'] == 'W200')
        self.assertEqual(retained['status'], 'saved')
        self.assertEqual(retained['importedItemId'], saved['itemId'])
        self.assertEqual(len(queries), 4)

    def test_citing_requires_actual_reference(self):
        citing = work('https://openalex.org/W300', 'Citing study', references=[SEED_ID])
        false = work('https://openalex.org/W301', 'False match')
        def fake_json(url, source):
            if 'doi%3A10.1234%2Fseed' in url:
                return {'results': [self.seed_work]}
            return {'results': [citing, false]}
        self.app.ai_search._json = fake_json
        self.wait(self.app.call('relations.discover', {'sessionId': self.session['id'], 'itemId': self.seed['id'],
                                                       'direction': 'citing'})['jobId'])
        rows = self.app.call('relations.discoveries', {'sessionId': self.session['id']})
        self.assertEqual([row['workId'] for row in rows], ['W300'])

    def test_unrelated_local_profiles_do_not_create_an_edge(self):
        profiles = [
            {'profile': {'keywords': ['quantum', 'photon'], 'fields': {'methods': 'optical interference'}}},
            {'profile': {'keywords': ['agriculture', 'soil'], 'fields': {'methods': 'field survey'}}},
        ]
        self.assertEqual(self.app.relations._local_candidates(profiles), [])

    def test_same_title_and_year_with_different_author_is_not_merged(self):
        self.app.library.create({'title': 'A common title', 'author': [{'literal': 'Another Author'}],
                                 'issued': {'date-parts': [[2024]]}})
        with self.app.library.db() as db:
            matched = self.app.relation_discovery._match(db, {'title': 'A common title', 'authors': ['A. Researcher'],
                                                               'year': 2024, 'doi': ''})
        self.assertIsNone(matched)

    def test_manual_relation_is_separate_from_inferred_and_citation_edges(self):
        other_id = self.app.call('relations.get', {'sessionId': self.session['id']})['items'][1]['itemId']
        added = self.app.call('relations.manualAdd', {'sessionId': self.session['id'], 'leftItemId': self.seed['id'],
                                                     'rightItemId': other_id, 'label': '共用数据集', 'note': '作者核对后确认'})
        self.assertEqual(len(added), 1)
        self.assertEqual((added[0]['kind'], added[0]['note']), ('manual', '作者核对后确认'))
        export = json.loads(self.app.call('relations.graphExport', {'sessionId': self.session['id']}))
        self.assertEqual(export['format'], 'research-library-graph-v1')
        self.assertEqual(export['edges'][0]['kind'], 'manual')
        self.assertEqual(self.app.call('relations.results', {'sessionId': self.session['id']}), [])
        self.assertEqual(self.app.call('relations.discoveries', {'sessionId': self.session['id']}), [])
        self.assertEqual(self.app.call('relations.manualRemove', {'id': added[0]['id']}), [])


if __name__ == '__main__':
    unittest.main()
