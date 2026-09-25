import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from client_backend.library import Library
from client_backend.attachments import Attachments


class AttachmentRolesTest(unittest.TestCase):
    def test_existing_library_gains_role_and_supplement_is_distinct(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            path.mkdir(exist_ok=True)
            with closing(sqlite3.connect(path / 'library.sqlite3')) as db:
                db.execute('''CREATE TABLE attachments(id TEXT PRIMARY KEY,item_id TEXT,object_id TEXT,name TEXT,
                    version TEXT,text_status TEXT,page_count INTEGER,text_json TEXT,position_json TEXT NOT NULL DEFAULT '{}',created_at TEXT)''')
                db.commit()
            library = Library(path)
            item = library.create({'title': 'Paper with supporting materials'})
            attachments = Attachments(library)
            pdf = path / 'article.pdf'
            supplement = path / 'article-supplementary.txt'
            pdf.write_bytes(b'%PDF-1.4\n')
            supplement.write_text('Supporting figures and data', encoding='utf-8')
            first = attachments.add(item['id'], pdf)
            second = attachments.add(item['id'], supplement)
            roles = {row['id']: row['role'] for row in library.get(item['id'])['attachments']}
            self.assertEqual((roles[first['id']], roles[second['id']]), ('main', 'supplementary'))
            attachments.set_role(second['id'], 'other')
            self.assertEqual(attachments.record(second['id'])['role'], 'other')


if __name__ == '__main__':
    unittest.main()
