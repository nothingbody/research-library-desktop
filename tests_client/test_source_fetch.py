import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from client_backend.common import AppError
from client_backend.service import Application


class FakeHeaders:
    def __init__(self, retry_after=None):
        self._retry_after = retry_after

    def get(self, key, default=None):
        return self._retry_after if key == 'Retry-After' else default


class FakeResponse:
    def __init__(self, data):
        self.data, self.headers = data, {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, _):
        return self.data


def http_error(code, retry_after=None):
    return HTTPError('https://api.test/', code, 'error', FakeHeaders(retry_after), None)


class SourceFetchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name) / 'library')

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def test_retry_succeeds_after_transient_429(self):
        calls = []
        def fake_urlopen(req, timeout):
            calls.append(1)
            if len(calls) < 3:
                raise http_error(429, retry_after='0')
            return FakeResponse(b'{"results": []}')
        with patch('client_backend.ai_search.request.urlopen', fake_urlopen):
            self.assertEqual(self.app.ai_search._json('https://api.test/', 'openalex'), {'results': []})
        self.assertEqual(len(calls), 3)

    def test_retry_gives_up_after_budget_and_stays_retryable(self):
        with patch('client_backend.ai_search.request.urlopen', side_effect=http_error(503)):
            with self.assertRaises(AppError) as ctx:
                self.app.ai_search._json('https://api.test/', 'crossref')
        self.assertEqual(ctx.exception.code, 'SOURCE_HTTP_ERROR')
        self.assertTrue(ctx.exception.retryable)

    def test_client_error_is_not_retried(self):
        calls = []
        def fake_urlopen(req, timeout):
            calls.append(1)
            raise http_error(404)
        with patch('client_backend.ai_search.request.urlopen', fake_urlopen):
            with self.assertRaises(AppError) as ctx:
                self.app.ai_search._json('https://api.test/', 'crossref')
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(len(calls), 1)

    def test_network_error_maps_without_retry_loop(self):
        with patch('client_backend.ai_search.request.urlopen', side_effect=URLError('down')):
            with self.assertRaises(AppError) as ctx:
                self.app.ai_search._json('https://api.test/', 'pubmed')
        self.assertEqual(ctx.exception.code, 'SOURCE_NETWORK_ERROR')

    def test_retry_delay_honors_numeric_retry_after_with_cap(self):
        self.assertEqual(self.app.ai_search._retry_delay(http_error(429, '2'), 1.0), 2.0)
        self.assertEqual(self.app.ai_search._retry_delay(http_error(429, '600'), 1.0), 10.0)
        self.assertEqual(self.app.ai_search._retry_delay(http_error(429, 'Wed, 21 Oct 2026 07:28:00 GMT'), 1.0), 1.0)
        self.assertEqual(self.app.ai_search._retry_delay(http_error(429), 1.5), 1.5)

    def test_openalex_adds_mailto_only_with_valid_contact(self):
        seen = []
        def fake_json(url, source):
            seen.append(url)
            return {'results': []}
        self.app.ai_search._json = fake_json
        self.app.ai_search._openalex('scheduling', 5)
        self.assertNotIn('mailto', seen[0])
        self.app.library.set_settings({'contactEmail': 'researcher@example.org'})
        self.app.ai_search._openalex('scheduling', 5)
        self.assertIn('mailto=researcher%40example.org', seen[1])

    def test_contact_email_setting_validates_format(self):
        with self.assertRaises(AppError):
            self.app.library.set_settings({'contactEmail': 'not-an-email'})
        self.assertEqual(self.app.library.set_settings({'contactEmail': ''})['contactEmail'], '')


PUBMED_ESEARCH = {'esearchresult': {'idlist': ['111', '222']}}
PUBMED_ESUMMARY = {'result': {
    '111': {'title': 'First study', 'authors': [{'name': 'Wang M'}], 'pubdate': '2024',
            'fulljournalname': 'Journal One', 'articleids': [{'idtype': 'doi', 'value': '10.1000/a'}]},
    '222': {'title': 'Second study', 'authors': [], 'pubdate': '2023',
            'fulljournalname': 'Journal Two', 'articleids': []},
}}
PUBMED_EFETCH = b'''<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle><MedlineCitation><PMID>111</PMID><Article><Abstract>
    <AbstractText Label="BACKGROUND">Flexible  scheduling matters.</AbstractText>
    <AbstractText Label="METHODS">We use <i>surrogate</i> models.</AbstractText>
  </Abstract></Article></MedlineCitation></PubmedArticle>
  <PubmedArticle><MedlineCitation><PMID>222</PMID><Article/></MedlineCitation></PubmedArticle>
</PubmedArticleSet>'''


class PubmedAbstractTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name) / 'library')

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def run_pubmed(self, fail_efetch=False):
        def fake_json(url, source):
            return PUBMED_ESEARCH if 'esearch' in url else PUBMED_ESUMMARY
        def fake_xml(url, source):
            if fail_efetch:
                raise AppError('SOURCE_NETWORK_ERROR', '无法连接 PubMed', retryable=True)
            from xml.etree import ElementTree
            return ElementTree.fromstring(PUBMED_EFETCH)
        self.app.ai_search._json, self.app.ai_search._xml = fake_json, fake_xml
        return self.app.ai_search._pubmed('scheduling', 10)

    def test_efetch_fills_abstract_and_keeps_summary_fields(self):
        records = self.run_pubmed()
        first, second = {r['sourceId']: r for r in records}['111'], {r['sourceId']: r for r in records}['222']
        self.assertIn('surrogate models', first['abstract'])
        self.assertEqual(first['doi'], '10.1000/a')
        self.assertEqual(first['venue'], 'Journal One')
        self.assertEqual(second['abstract'], '')

    def test_efetch_failure_degrades_to_empty_abstract(self):
        records = self.run_pubmed(fail_efetch=True)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(r['abstract'] == '' for r in records))


if __name__ == '__main__':
    unittest.main()
