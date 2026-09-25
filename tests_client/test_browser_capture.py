import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from client_backend.attachments import Attachments
from client_backend.browser_capture import capture, import_downloaded
from client_backend.common import AppError
from client_backend.fulltext import Fulltext
from client_backend.library import Library


class FakeJobs:
    def create(self, kind, payload):
        return {'jobId': 'queued-pdf', 'kind': kind, 'payload': payload}


class BrowserCaptureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.library = Library(Path(self.temp.name) / 'library')
        self.fulltext = Fulltext(self.library, None, FakeJobs())
        self.collection = self.library.collection_edit('create', {'name': '调度'})['id']

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, **overrides):
        return {'pageUrl': 'https://example.org/paper/one', 'collectionId': self.collection,
                'data': {'title': 'Flexible Job Shop Scheduling', 'author': ['Zhang, Wei'],
                         'year': '2025', 'DOI': '10.1234/fjss'}, **overrides}

    def test_capture_reuses_doi_and_adds_collection_and_pdf_source(self):
        first = capture(self.library, self.fulltext, self.payload(pdfUrl='https://example.org/paper/one.pdf'))
        second = capture(self.library, self.fulltext, self.payload(pdfUrl='https://example.org/paper/one.pdf', downloadPdf=True))
        self.assertTrue(first['created'])
        self.assertFalse(second['created'])
        self.assertEqual(first['itemId'], second['itemId'])
        self.assertEqual(second['downloadJobId'], 'queued-pdf')
        self.assertEqual(self.library.query({'collectionId': self.collection})['total'], 1)
        self.assertEqual(len(self.fulltext.sources(first['itemId'])), 1)

    def test_same_page_url_reuses_record_even_without_author_and_year(self):
        payload = self.payload(data={'title': 'Same title'})
        first = capture(self.library, self.fulltext, payload)
        second = capture(self.library, self.fulltext, payload)
        self.assertEqual(first['itemId'], second['itemId'])
        self.assertEqual(second['duplicateBasis'], '来源网址')

    def test_same_title_without_other_identity_on_different_pages_stays_distinct(self):
        payload = self.payload(data={'title': 'Same title'})
        first = capture(self.library, self.fulltext, payload)
        second = capture(self.library, self.fulltext, self.payload(pageUrl='https://example.org/paper/two', data={'title': 'Same title'}))
        self.assertNotEqual(first['itemId'], second['itemId'])

    def test_arxiv_abstract_and_pdf_versions_match_before_and_during_capture(self):
        from client_backend.browser_capture import check_duplicate
        first = capture(self.library, self.fulltext, self.payload(pageUrl='https://arxiv.org/abs/1706.03762',
            data={'title': 'Attention Is All You Need', 'author': ['Vaswani, Ashish'], 'year': '2017'}))
        candidate = self.payload(pageUrl='https://www.arxiv.org/pdf/1706.03762v7',
            data={'title': 'Attention is all you need!', 'author': ['Vaswani, A.'], 'year': '2023'})
        self.assertEqual(check_duplicate(self.library, candidate)['duplicate']['basis'], 'arXiv ID')
        second = capture(self.library, self.fulltext, candidate)
        self.assertEqual(first['itemId'], second['itemId'])
        self.assertEqual(self.library.query({})['total'], 1)

    def test_title_year_first_author_matches_variants_without_false_doi_merge(self):
        from client_backend.browser_capture import check_duplicate
        first = capture(self.library, self.fulltext, self.payload())
        candidate = self.payload(pageUrl='https://another.example.org/study', data={
            'title': 'Flexible job-shop scheduling!', 'author': ['Zhang, Wen'], 'year': '2025'})
        self.assertEqual(check_duplicate(self.library, candidate)['duplicate']['basis'], '题名、年份与首位作者')
        self.assertEqual(capture(self.library, self.fulltext, candidate)['itemId'], first['itemId'])

    def test_rejects_bad_pdf_before_creating_record(self):
        with self.assertRaises(AppError):
            capture(self.library, self.fulltext, self.payload(pdfUrl='http://localhost/private.pdf'))
        self.assertEqual(self.library.query({})['total'], 0)

    def test_different_doi_with_same_title_author_year_is_distinct(self):
        first = capture(self.library, self.fulltext, self.payload())
        second = capture(self.library, self.fulltext, self.payload(data={
            'title': 'Flexible Job Shop Scheduling', 'author': ['Zhang, Wei'],
            'year': '2025', 'DOI': '10.1234/revised'}))
        self.assertNotEqual(first['itemId'], second['itemId'])

    def test_browser_capture_keeps_auto_classification_tags_when_deduplicating(self):
        first = capture(self.library, self.fulltext, self.payload(data={'title': 'Flexible Job Shop Scheduling', 'tags': ['来源：知网', '类型：期刊论文']}))
        capture(self.library, self.fulltext, self.payload(data={'title': 'Flexible Job Shop Scheduling', 'tags': ['来源：知网', '期刊：经济研究']}))
        item = self.library.get(first['itemId'])
        self.assertEqual(item['tags'], ['来源：知网', '类型：期刊论文', '期刊：经济研究'])

    def test_imports_completed_authorized_cnki_pdf_and_queues_index(self):
        item = capture(self.library, self.fulltext, self.payload())
        download_root = Path(self.temp.name) / 'profile'
        target = download_root / 'Downloads' / 'fixture.pdf'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'%PDF-1.4\n% browser download fixture\n')
        fulltext = Fulltext(self.library, SimpleNamespace(attachments=Attachments(self.library)), FakeJobs())
        with mock.patch('client_backend.browser_capture.Path.home', return_value=download_root):
            result = import_downloaded(self.library, fulltext, {'itemId': item['itemId'], 'sourceUrl': 'https://kns.cnki.net/kcms2/article/abstract?v=fixture', 'format': 'pdf', 'path': str(target)})
        self.assertEqual(result['indexJobId'], 'queued-pdf')
        self.assertEqual(self.library.get(item['itemId'])['attachments'][0]['textStatus'], 'pending')

    def test_import_rejects_non_download_path(self):
        item = capture(self.library, self.fulltext, self.payload())
        target = Path(self.temp.name) / 'fixture.caj'
        target.write_bytes(b'CAJ fixture')
        fulltext = Fulltext(self.library, SimpleNamespace(attachments=Attachments(self.library)), FakeJobs())
        with self.assertRaises(AppError):
            import_downloaded(self.library, fulltext, {'itemId': item['itemId'], 'sourceUrl': 'https://kns.cnki.net/kcms2/article/abstract?v=fixture', 'format': 'caj', 'path': str(target)})

    def test_offline_download_request_saves_metadata_without_network_job(self):
        self.library.set_settings({'online': False})
        result = capture(self.library, self.fulltext, self.payload(pdfUrl='https://example.org/paper.pdf', downloadPdf=True))
        self.assertTrue(result['downloadSkippedOffline'])
        self.assertIsNone(result['downloadJobId'])
        self.assertEqual(self.library.query({})['total'], 1)


if __name__ == '__main__':
    unittest.main()
