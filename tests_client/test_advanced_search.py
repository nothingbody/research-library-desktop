import json
import tempfile
import unittest
from pathlib import Path

from client_backend.common import AppError, now, uid
from client_backend.library import Library
from client_backend.organization import Organization


class AdvancedSearchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.temp.name) / 'library')
        self.organization = Organization(self.library)
        self.a = self.library.collection_edit('create', {'name': '调度'})['id']
        self.b = self.library.collection_edit('create', {'name': '优化'})['id']
        self.one = self.library.create({'title': 'Flexible job shop scheduling', 'author': 'Wang, Mei',
            'year': '2025', 'abstract': 'Multi-objective scheduling', 'tags': ['FJSP']}, self.a)
        self.two = self.library.create({'title': 'Vehicle routing', 'author': 'Li, Jun',
            'year': '2024', 'abstract': 'Multi-objective transport'}, self.b)
        self.library.note_save({'itemId': self.one['id'], 'title': 'Method', 'content': 'Uses decomposition'})
        stamp = now()
        with self.library.db(True) as db:
            object_id, attachment_id = uid(), uid()
            db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)', (object_id, 'hash', 0, 'fixture.pdf', 'managed', 'application/pdf', stamp))
            db.execute('INSERT INTO attachments(id,item_id,object_id,name,version,text_status,created_at) VALUES(?,?,?,?,?,?,?)',
                       (attachment_id, self.two['id'], object_id, 'fixture.pdf', 'v1', 'indexed', stamp))
            db.execute('INSERT INTO annotations VALUES(?,?,?,?,?,?,?)',
                       (uid(), attachment_id, 'v1', json.dumps({'quote': 'AGV routing constraints', 'comment': 'Compare methods'}), 1, stamp, stamp))
            db.execute('INSERT INTO document_chunks(id,item_id,attachment_id,source_type,source_id,source_version,page,start_offset,text,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (uid(), self.two['id'], attachment_id, 'pdf', attachment_id, 'v1', 1, 0, 'Travel time and transport', stamp))

    def tearDown(self):
        self.temp.cleanup()

    def search(self, rule, **rest):
        return self.library.query({'advanced': rule, **rest})

    def test_nested_conditions_across_item_note_annotation_and_pdf(self):
        rule = {'op': 'any', 'conditions': [
            {'op': 'all', 'conditions': [
                {'field': 'title', 'operator': 'contains', 'value': 'shop'},
                {'field': 'note', 'operator': 'contains', 'value': 'decomposition'}]},
            {'op': 'all', 'conditions': [
                {'field': 'annotation', 'operator': 'contains', 'value': 'AGV'},
                {'field': 'pdf', 'operator': 'contains', 'value': 'Travel time'}]}]}
        result = self.search(rule, collectionIds=[self.a, self.b])
        self.assertEqual(result['total'], 2)
        self.assertEqual(len({item['id'] for item in result['items']}), 2)
        self.assertEqual(self.search(rule, collectionIds=[self.a])['total'], 1)

    def test_empty_and_literal_wildcards(self):
        empty = {'op': 'all', 'conditions': [{'field': 'note', 'operator': 'isEmpty', 'value': ''}]}
        self.assertEqual([item['id'] for item in self.search(empty)['items']], [self.two['id']])
        literal = {'op': 'all', 'conditions': [{'field': 'title', 'operator': 'contains', 'value': '%'}]}
        self.assertEqual(self.search(literal)['total'], 0)

    def test_saved_rule_updates_as_items_change_and_rejects_invalid_tree(self):
        rule = {'advanced': {'op': 'all', 'conditions': [{'field': 'tag', 'operator': 'equals', 'value': 'FJSP'}]},
                'collectionIds': [self.a]}
        saved = self.organization.smart_save({'name': 'FJSP', 'rule': rule})
        self.assertEqual(self.organization.smart_results({'id': saved['id']})['total'], 1)
        self.library.bulk([self.one['id']], 'tags', [])
        self.assertEqual(self.organization.smart_results({'id': saved['id']})['total'], 0)
        with self.assertRaises(AppError):
            self.organization.smart_save({'name': 'Bad', 'rule': {'advanced': {'op': 'all',
                'conditions': [{'field': 'title; DROP TABLE items', 'operator': 'contains', 'value': 'a'}]}}})


if __name__ == '__main__':
    unittest.main()
