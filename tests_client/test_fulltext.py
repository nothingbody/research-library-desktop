import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from client_backend.service import Application
from client_backend.common import AppError
from client_backend.fulltext import public_url


class Response:
    def __init__(self, data, url, content_type):
        self.data, self.url, self.headers = data, url, {'Content-Type': content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _):
        return self.data

    def geturl(self):
        return self.url


class FulltextTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name) / 'library')

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_import_retains_open_fulltext_and_existing_item_is_idempotent(self):
        search = self.app.ai_search.create({'brief': 'distributed flexible job shop scheduling', 'sources': ['openalex'], 'useModel': False})
        record = {'source': 'openalex', 'sourceId': 'fixture-1', 'title': 'Scheduling fulltext fixture',
                  'authors': [{'family': 'Wang'}], 'year': 2025, 'venue': 'Test Journal',
                  'abstract': 'Flexible job shop scheduling uses a surrogate method for faster evaluation.',
                  'doi': '10.1000/fixture', 'url': 'https://doi.org/10.1000/fixture',
                  'citationCount': 10, 'oaUrl': 'https://example.org/paper.pdf',
                  'type': 'article-journal', 'raw': {'source': 'fixture'}}
        self.app.ai_search._source_records = lambda *args: [record]
        self.app.ai_search.run({'sessionId': search['id']}, lambda *_: None)
        candidate = self.app.ai_search.results({'sessionId': search['id']})['items'][0]
        first = self.app.ai_search.import_candidates({'candidateIds': [candidate['id']]})['imported'][0]
        self.app.ai_search.import_candidates({'candidateIds': [candidate['id']]})
        sources = self.app.fulltext.sources(first['itemId'])
        self.assertEqual(len(sources), 2)
        self.assertEqual((sources[0]['origin'], sources[0]['url']), ('oa', record['oaUrl']))
        self.assertEqual(sources[1]['origin'], 'record')

    def test_resolver_distinguishes_pdf_landing_page_and_private_address(self):
        item = self.app.library.create({'title': 'Synthetic scheduling paper'})
        source = self.app.fulltext.add({'itemId': item['id'], 'url': 'https://example.org/article'})
        with patch('client_backend.fulltext.request.urlopen', return_value=Response(
                b'<html><meta name="citation_pdf_url" content="/paper.pdf"></html>',
                'https://example.org/article', 'text/html')):
            resolved, kind = self.app.fulltext._resolve({'url': source['url']}, lambda *_: None)
        self.assertEqual((resolved, kind), ('https://example.org/paper.pdf', 'pdf'))
        with patch('client_backend.fulltext.request.urlopen', return_value=Response(
                b'<html><title>Article</title></html>', 'https://example.org/article', 'text/html')):
            self.assertEqual(self.app.fulltext._resolve({'url': source['url']}, lambda *_: None)[1], 'landing')
        for address in ('http://localhost/file.pdf', 'http://127.0.0.1/file.pdf', 'file:///test.pdf'):
            with self.assertRaises(AppError):
                public_url(address)

    def test_download_rejects_private_and_credential_addresses_before_network(self):
        item = self.app.library.create({'title': 'Synthetic download paper'})
        for address in ('http://127.0.0.1/secret.pdf', 'http://192.168.1.1/secret.pdf',
                        'http://user:pass@example.org/paper.pdf', 'ftp://example.org/paper.pdf'):
            with self.assertRaises(AppError):
                self.app.downloads.pdf({'itemId': item['id'], 'url': address}, lambda *_: None)

    def test_obtain_attaches_verified_pdf_and_repeat_reuses_attachment(self):
        item = self.app.library.create({'title': 'Synthetic scheduling paper'})
        source = self.app.fulltext.add({'itemId': item['id'], 'url': 'https://example.org/paper.pdf'})
        attachment_id, object_id = str(uuid4()), str(uuid4())
        with self.app.library.db(True) as db:
            db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
                       (object_id, str(uuid4()), 0, 'synthetic.pdf', 'managed', 'application/pdf', '2026-01-01'))
            db.execute('''INSERT INTO attachments(id,item_id,object_id,name,version,text_status,created_at)
                VALUES(?,?,?,?,?,?,?)''', (attachment_id, item['id'], object_id, 'synthetic.pdf', 'v1', 'pending', '2026-01-01'))
        with (patch('client_backend.fulltext.request.urlopen', return_value=Response(
                b'%PDF-1.7 synthetic fixture', source['url'], 'application/pdf')),
              patch.object(self.app.downloads, 'pdf', return_value={'attachmentId': attachment_id}) as download):
            first = self.app.fulltext.obtain({'itemId': item['id'], 'sourceId': source['id']}, lambda *_: None)
            second = self.app.fulltext.obtain({'itemId': item['id'], 'sourceId': source['id']}, lambda *_: None)
        self.assertEqual(first['attachmentId'], attachment_id)
        self.assertTrue(second['duplicate'])
        self.assertEqual(download.call_count, 1)
        self.assertEqual(self.app.fulltext.sources(item['id'])[0]['state'], 'attached')


if __name__ == '__main__':
    unittest.main()
