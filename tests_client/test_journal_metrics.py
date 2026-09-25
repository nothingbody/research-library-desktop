import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from client_backend.service import Application


class JournalMetricsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / 'journals'
        self.source.mkdir()
        with closing(sqlite3.connect(self.source / 'journals.sqlite3')) as db:
            db.execute('CREATE TABLE journals(id INTEGER PRIMARY KEY,list_json TEXT,detail_json TEXT,detail_status TEXT,fetched_at TEXT)')
            for journal_id, name, issn, year, impact in (
                (1, 'Known Journal', '1234-567X', 2025, 8.4),
                (2, 'Shared ISSN A', '2222-2222', 2024, 3.1),
                (3, 'Shared ISSN B', '2222-2222', 2024, 2.9),
                (4, 'No JCR Year', '3333-3333', None, 6.0),
            ):
                item = {'canonical_name': name, 'print_issn': issn,
                        'jcr_quartile': 'Q1', 'jcr_impact_factor': impact, 'jcr_year': year}
                db.execute('INSERT INTO journals VALUES(?,?,?,?,?)',
                           (journal_id, json.dumps({'unified': item}), None, 'ok', '2026-09-23'))
            db.commit()
        self.app = Application(root / 'library', journal_root=self.source)
        self.app.journals.index({}, lambda *_: None)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_bulk_match_is_exact_unique_and_year_labeled(self):
        matches = self.app.journals.match_issns(['1234-567X', '2222-2222', '3333-3333', '9999-9999'])
        known = matches['1234567X']
        self.assertTrue(known['matched'])
        self.assertEqual((known['jcrYear'], known['jcrQuartile'], known['impactFactor']), (2025, 'Q1', 8.4))
        self.assertFalse(matches['22222222']['matched'])
        self.assertFalse(matches['99999999']['matched'])
        self.assertTrue(matches['33333333']['matched'])
        self.assertIsNone(matches['33333333']['impactFactor'])
        self.assertIsNone(matches['33333333']['jcrQuartile'])

    def test_results_get_page_metrics_without_changing_order(self):
        rows = [{'id': 'first', 'issn': '1234-567X'}, {'id': 'second', 'issn': ''},
                {'id': 'third', 'issn': '2222-2222'}]
        self.app.ai_search.results = lambda _: {'items': rows, 'total': 3}
        result = self.app.search_results({})
        self.assertEqual([row['id'] for row in result['items']], ['first', 'second', 'third'])
        self.assertEqual(result['items'][0]['journalMatch']['impactFactor'], 8.4)
        self.assertNotIn('journalMatch', result['items'][1])
        self.assertFalse(result['items'][2]['journalMatch']['matched'])


if __name__ == '__main__':
    unittest.main()
