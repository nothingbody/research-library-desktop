import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from client_backend.service import Application


class OrganizationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name) / 'library')
        self.item = self.app.library.create({'title': 'Surrogate scheduling study', 'abstract': 'Flexible manufacturing research.'})

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_project_references_existing_items_and_archiving_keeps_them(self):
        project = self.app.call('projects.create', {'title': 'Scheduling review', 'description': 'Compare methods'})
        linked = self.app.call('projects.link', {'projectId': project['id'], 'type': 'item', 'id': self.item['id']})
        self.assertEqual(linked['links'][0]['title'], self.item['title'])
        self.app.call('projects.archive', {'projectId': project['id']})
        self.assertFalse(self.app.call('projects.list', {}))
        self.assertEqual(self.app.library.get(self.item['id'])['title'], self.item['title'])

    def test_smart_collection_requeries_library_without_copying_items(self):
        saved = self.app.call('smartCollections.save', {'name': 'Unread scheduling', 'rule': {'q': 'surrogate', 'readingState': 'unread'}})
        self.assertEqual(self.app.call('smartCollections.results', {'id': saved['id']})['total'], 1)
        self.app.library.update(self.item['id'], {'readingState': 'read'}, self.item['revision'])
        self.assertEqual(self.app.call('smartCollections.results', {'id': saved['id']})['total'], 0)
        self.assertEqual(self.app.library.stats()['items'], 1)

    def test_annotations_search_returns_page_and_version_state(self):
        attachment_id, object_id = str(uuid4()), str(uuid4())
        with self.app.library.db(True) as db:
            db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
                       (object_id, str(uuid4()), 0, 'fixture.pdf', 'managed', 'application/pdf', '2026-01-01'))
            db.execute('''INSERT INTO attachments(id,item_id,object_id,name,version,text_status,created_at)
                VALUES(?,?,?,?,?,?,?)''', (attachment_id, self.item['id'], object_id, 'fixture.pdf', 'v1', 'pending', '2026-01-01'))
            db.execute('INSERT INTO annotations VALUES(?,?,?,?,?,?,?)',
                       (str(uuid4()), attachment_id, 'v1', json.dumps({'pageIndex': 3, 'quote': 'surrogate evaluation', 'comment': 'Important method'}),
                        1, '2026-01-01', '2026-01-01'))
        result = self.app.call('annotations.search', {'q': 'surrogate'})
        self.assertEqual((result['total'], result['items'][0]['page'], result['items'][0]['stale']), (1, 4, False))
        with self.app.library.db(True) as db:
            db.execute('UPDATE attachments SET version=? WHERE id=?', ('v2', attachment_id))
        self.assertTrue(self.app.call('annotations.search', {'q': 'surrogate'})['items'][0]['stale'])
        markdown = self.app.call('annotations.exportText', {'q': 'surrogate', 'format': 'markdown'})
        self.assertIn('surrogate evaluation', markdown)
        self.assertIn('附件版本已变化', markdown)
        self.assertIn('research://attachment/' + attachment_id + '?page=4', markdown)
        exported = json.loads(self.app.call('annotations.exportText', {'q': 'surrogate', 'format': 'json'}))
        self.assertEqual(exported['count'], 1)
        self.assertTrue(exported['annotations'][0]['stale'])


if __name__ == '__main__':
    unittest.main()
