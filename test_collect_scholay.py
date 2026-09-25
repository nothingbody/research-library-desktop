import json
from pathlib import Path
import tempfile
import unittest

from collect_scholay import APIError, Collector


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.collector = Collector(Path(self.temp.name), 1)

    def tearDown(self):
        self.collector.db.close()
        self.temp.cleanup()

    def test_resume_after_login_requirement(self):
        c = self.collector
        calls = []

        def request(endpoint, payload, label):
            calls.append(payload['page'])
            if payload['page'] == 2:
                raise APIError(400, 4036, 'login required')
            return {'rows': [{'unified': {'id': 1, 'canonical_name': 'fixture'}}], 'hasMore': True}

        c.request = request
        c.lists()
        self.assertEqual(c.get('next_page'), 2)
        self.assertEqual(c.report()['list_blocker']['code'], 4036)
        c.lists()
        self.assertEqual(calls, [1, 2, 2])
        self.assertEqual(c.report()['unique_journals'], 1)
        self.assertFalse(c.report()['complete'])

    def test_pagination_must_not_silently_repeat(self):
        c = self.collector
        c.request = lambda *args: {'rows': [{'unified': {'id': 1}}], 'hasMore': True}
        with self.assertRaisesRegex(ValueError, 'Unstable ID order'):
            c.lists()
        self.assertEqual(c.get('next_page'), 2)
        self.assertFalse(c.report()['complete'])

    def test_completion_requires_details_and_end_baseline(self):
        c = self.collector
        c.put('stats_start', {'totalJournals': 1})
        c.request = lambda *args: {'rows': [{'unified': {'id': 1}}], 'hasMore': False}
        c.lists()
        self.assertFalse(c.report()['complete'])
        c.request = lambda *args: {'unified': {'id': 1, 'print_issn': '1234-5678'},
                                   'sources': {'fixture': {'metric': 2}}}
        c.details()
        self.assertFalse(c.report()['complete'])
        c.put('stats_end', {'totalJournals': 1})
        c.db.commit()
        self.assertTrue(c.report()['complete'])
        c.export()
        record = json.loads((c.root / 'journals.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(record['detail']['sources']['fixture']['metric'], 2)
        self.assertEqual(c.db.execute('SELECT COUNT(*) FROM journal_issns').fetchone()[0], 1)
        c.put('stats_end', {'totalJournals': 2})
        self.assertFalse(c.report()['complete'])


if __name__ == '__main__':
    unittest.main()
