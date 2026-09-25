import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from client_backend.common import AppError, dumps, now, uid
from client_backend.assistant import ReadingAssistant
from client_backend.service import Application


class ReadingAssistantTest(unittest.TestCase):
    def test_deepseek_requests_disable_thinking_and_request_json_when_needed(self):
        messages = [{'role': 'user', 'content': 'test'}]
        plain = json.loads(ReadingAssistant._request_body('https://api.deepseek.com', 'deepseek-flash', messages,
                                                           temperature=0, max_tokens=32))
        self.assertEqual(plain['thinking'], {'type': 'disabled'})
        self.assertNotIn('response_format', plain)
        structured = json.loads(ReadingAssistant._request_body('https://api.deepseek.com', 'deepseek-flash', messages,
                                                                temperature=0, max_tokens=100, json_output=True))
        self.assertEqual(structured['response_format'], {'type': 'json_object'})
        local = json.loads(ReadingAssistant._request_body('http://127.0.0.1:1234/v1', 'local', messages,
                                                          temperature=0, max_tokens=32, json_output=True))
        self.assertNotIn('thinking', local)
        self.assertNotIn('response_format', local)

    def setUp(self):
        self.seen = []
        seen = self.seen
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                body = json.loads(raw)
                seen.append({'path': self.path, 'authorization': self.headers.get('Authorization'), 'body': body})
                layout = any('PDF 段落忠实翻译' in str(message.get('content', '')) for message in body.get('messages', []))
                align = any('双语文本对齐器' in str(message.get('content', '')) for message in body.get('messages', []))
                intro = any('你只翻译给出的题名' in str(message.get('content', '')) for message in body.get('messages', []))
                content = json.dumps({'translations': {'1': '第一行译文', '2': '第二行译文'}}) if layout else json.dumps({'target': '调度'}) if align else json.dumps({'titleZh': '智慧物流', 'abstractZh': '物流研究摘要。', 'keywordsZh': ['物流']}) if intro else '本地测试结果：仅依据所选原文。'
                response = json.dumps({'choices': [{'message': {'content': content}}]}).encode('utf-8')
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(response))); self.end_headers(); self.wfile.write(response)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.temp = tempfile.TemporaryDirectory()
        self.app = Application(Path(self.temp.name) / 'library')
        self.item = self.app.library.create({'title': 'Reading assistant fixture', 'abstract': 'Original abstract'})
        self.app.call('assistant.settings', {'assistantBaseUrl': f'http://127.0.0.1:{self.server.server_port}/v1', 'assistantModel': 'local-fixture', 'assistantTimeout': 10})

    def tearDown(self):
        self.app.close(); self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def wait_job(self, job_id):
        for _ in range(100):
            row = next(job for job in self.app.jobs.list() if job['id'] == job_id)
            if row['state'] == 'completed':
                return row
            if row['state'] in ('failed', 'cancelled'):
                self.fail(str(row))
            time.sleep(.03)
        self.fail('assistant job timed out')

    def indexed_attachment(self, pages):
        object_id, attachment_id = uid(), uid()
        with self.app.library.db(True) as db:
            db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)',
                       (object_id, 'fixture-hash-' + object_id, 1, str(Path(self.temp.name) / 'fixture.pdf'), 'managed', 'application/pdf', now()))
            db.execute('''INSERT INTO attachments(id,item_id,object_id,name,version,text_status,page_count,text_json,role,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                       (attachment_id, self.item['id'], object_id, 'fixture.pdf', 'fixture-version', 'indexed', len(pages), dumps(pages), 'main', now()))
        return attachment_id

    def test_bibliographic_intro_translation_uses_configured_model(self):
        self.app.call('assistant.configure', {'apiKey': 'local-test-key'})
        result = self.app.assistant.search_intro({'title': 'Smart logistics', 'abstract': 'Logistics research abstract.',
                                                  'keywords': ['logistics']})
        self.assertEqual(result['titleZh'], '智慧物流')
        self.assertEqual(result['keywordsZh'], ['物流'])
        self.assertEqual(self.seen[-1]['authorization'], 'Bearer local-test-key')

    def test_reading_workspace_terms_and_assistant_are_local_and_searchable(self):
        reading = self.app.call('reading.get', {'itemId': self.item['id']})
        self.assertEqual(reading['session']['status'], 'planned')
        updated = self.app.call('reading.save', {'itemId': self.item['id'], 'session': {'status': 'reading', 'goal': 'Check method', 'targetMinutes': 20, 'revision': reading['session']['revision']}, 'card': {'methods': 'Local method note', 'revision': reading['card']['revision']}})
        self.assertEqual(updated['card']['methods'], 'Local method note')
        activity = self.app.call('reading.activity', {'itemId': self.item['id'], 'page': 3, 'pageCount': 8, 'elapsedSeconds': 73})
        self.assertEqual(activity['secondsRead'], 73)
        term = self.app.call('terms.save', {'itemId': self.item['id'], 'term': 'attention', 'translation': '注意力机制', 'explanation': 'Local term explanation', 'source': {'page': 3, 'quote': 'attention connects'}})
        self.assertEqual(self.app.call('terms.list', {'itemId': self.item['id']})[0]['term'], 'attention')
        self.assertEqual(self.app.library.query({'q': '注意力机制'})['total'], 1)
        self.app.call('assistant.configure', {'apiKey': 'local-test-key'})
        submitted = self.app.call('assistant.run', {'itemId': self.item['id'], 'task': 'translate', 'text': 'Selected source text only.', 'page': 3})
        self.wait_job(submitted['jobId'])
        self.assertEqual(self.seen[0]['path'], '/v1/chat/completions')
        self.assertEqual(self.seen[0]['authorization'], 'Bearer local-test-key')
        self.assertIn('Selected source text only.', self.seen[0]['body']['messages'][1]['content'])
        self.assertEqual(self.seen[0]['body']['max_tokens'], 4800)
        runs = self.app.call('assistant.runs', {'itemId': self.item['id']})
        self.assertEqual(runs[0]['state'], 'completed')
        self.assertIn('本地测试结果', runs[0]['result']['content'])
        note = self.app.call('assistant.apply', {'runId': submitted['runId'], 'target': 'note'})
        self.assertEqual(note['title'], '对照翻译')
        self.assertIn('## 原文', note['content'])
        self.assertIn('## 中文翻译', note['content'])
        self.assertIn('Selected source text only.', note['content'])
        self.assertIn('本地测试结果', note['content'])
        self.assertNotIn('apiKey', self.app.library.get_settings())
        self.app.call('terms.delete', {'id': term['id'], 'revision': term['revision']})
        self.assertEqual(self.app.call('terms.list', {'itemId': self.item['id']}), [])

    def test_requires_memory_only_credential_and_tests_connection(self):
        submitted = self.app.call('assistant.run', {'itemId': self.item['id'], 'task': 'explain', 'text': 'source'})
        for _ in range(100):
            row = next(job for job in self.app.jobs.list() if job['id'] == submitted['jobId'])
            if row['state'] in ('failed', 'cancelled'):
                break
            time.sleep(.03)
        self.assertEqual(row['state'], 'failed')
        self.assertEqual(row['error']['code'], 'INVALID_ARGUMENT')
        self.app.call('assistant.configure', {'apiKey': 'local-test-key'})
        self.assertTrue(self.app.call('assistant.test', {})['ok'])
        self.app.call('assistant.clear', {})
        self.assertFalse(self.app.call('assistant.status', {})['runtimeConfigured'])
        with self.assertRaises(AppError):
            self.app.call('assistant.test', {})

    def test_reading_assistant_uses_indexed_page_or_paper_text_not_metadata(self):
        attachment_id = self.indexed_attachment(['First page source sentence.', 'Second page method and result.'])
        self.app.call('assistant.configure', {'apiKey': 'local-test-key'})
        page_run = self.app.call('assistant.run', {'itemId': self.item['id'], 'attachmentId': attachment_id,
                                                   'task': 'explain', 'scope': 'page', 'page': 2})
        self.wait_job(page_run['jobId'])
        page_request = self.seen[-1]['body']['messages'][1]['content']
        self.assertIn('当前 PDF 页面', page_request)
        self.assertIn('Second page method and result.', page_request)
        self.assertNotIn('First page source sentence.', page_request)
        translation_run = self.app.call('assistant.run', {'itemId': self.item['id'], 'attachmentId': attachment_id,
                                                          'task': 'translate', 'scope': 'page', 'page': 2})
        self.wait_job(translation_run['jobId'])
        translation_request = self.seen[-1]['body']
        self.assertEqual(translation_request['max_tokens'], 4800)
        self.assertIn('Second page method and result.', translation_request['messages'][1]['content'])
        paper_run = self.app.call('assistant.run', {'itemId': self.item['id'], 'attachmentId': attachment_id,
                                                    'task': 'summarize', 'scope': 'paper', 'page': 1})
        self.wait_job(paper_run['jobId'])
        paper_request = self.seen[-1]['body']['messages'][1]['content']
        self.assertIn('整篇 PDF 正文', paper_request)
        self.assertIn('First page source sentence.', paper_request)
        self.assertIn('Second page method and result.', paper_request)
        with self.assertRaises(AppError):
            self.app.call('assistant.run', {'itemId': self.item['id'], 'task': 'summarize', 'scope': 'selection', 'text': ''})

    def test_layout_translation_returns_id_mapped_text_for_pdf_overlay(self):
        self.app.call('assistant.configure', {'apiKey': 'local-test-key'})
        run = self.app.call('assistant.run', {'itemId': self.item['id'], 'task': 'translate_layout', 'scope': 'selection', 'page': 1, 'layoutVersion': 2,
                                              'text': json.dumps({'segments': [{'id': '1', 'text': 'First heading'}, {'id': '2', 'text': 'Second paragraph'}]})})
        self.wait_job(run['jobId'])
        saved = self.app.call('assistant.runs', {'itemId': self.item['id']})[0]['result']
        self.assertEqual(saved['translations']['1'], '第一行译文')
        self.assertEqual(saved['translations']['2'], '第二行译文')
        self.assertEqual(self.app.call('assistant.runs', {'itemId': self.item['id']})[0]['input']['layoutVersion'], 2)
        request_body = self.seen[-1]['body']
        self.assertEqual(request_body['max_tokens'], 7600)
        self.assertEqual(request_body['temperature'], 0)
        self.assertIn('PDF 段落忠实翻译', request_body['messages'][0]['content'])
        self.assertIn('学科通行的专业术语', request_body['messages'][0]['content'])
        self.assertIn('不得为适应页面宽度而删减', request_body['messages'][0]['content'])
        self.assertIn('Reading assistant fixture', request_body['messages'][1]['content'])

    def test_translated_page_is_saved_and_reused_after_reopening(self):
        attachment_id = self.indexed_attachment(['First heading. Second paragraph.'])
        self.app.call('assistant.configure', {'apiKey': 'local-test-key'})
        payload = {'itemId': self.item['id'], 'attachmentId': attachment_id, 'page': 1,
                   'layoutVersion': 3, 'segments': [{'id': '1', 'text': 'First heading'},
                                                     {'id': '2', 'text': 'Second paragraph'}]}
        first = self.app.call('assistant.translation.page', payload)
        self.assertEqual(first['state'], 'pending')
        for _ in range(100):
            saved = self.app.call('assistant.translation.page', payload)
            if saved['state'] == 'ready':
                break
            time.sleep(.03)
        self.assertEqual(saved['state'], 'ready')
        self.assertEqual(saved['translations']['1'], '第一行译文')
        self.assertEqual(len(self.seen), 1)
        self.app.close()
        self.app = Application(Path(self.temp.name) / 'library')
        reopened = self.app.call('assistant.translation.page', payload)
        self.assertEqual(reopened['state'], 'ready')
        self.assertTrue(reopened['saved'])
        self.assertEqual(len(self.seen), 1)
        with self.app.library.db(True) as db:
            db.execute('DELETE FROM translation_pages WHERE attachment_id=?', (attachment_id,))
        migrated = self.app.call('assistant.translation.page', payload)
        self.assertEqual(migrated['state'], 'ready')
        self.assertEqual(len(self.seen), 1)

    def test_translation_cache_changes_with_academic_glossary(self):
        attachment_id = self.indexed_attachment(['Scheduling under resource constraints.'])
        self.app.call('assistant.configure', {'apiKey': 'local-test-key'})
        payload = {'itemId': self.item['id'], 'attachmentId': attachment_id, 'page': 1,
                   'segments': [{'id': '1', 'text': 'Scheduling under resource constraints.'},
                                {'id': '2', 'text': 'A second paragraph.'}]}
        first = self.app.call('assistant.translation.page', payload)
        for _ in range(100):
            first = self.app.call('assistant.translation.page', payload)
            if first['state'] == 'ready':
                break
            time.sleep(.03)
        self.assertEqual(first['state'], 'ready')
        self.app.call('terms.save', {'itemId': self.item['id'], 'term': 'scheduling',
                                      'translation': '调度'})
        updated = self.app.call('assistant.translation.page', payload)
        self.assertNotEqual(updated['sourceHash'], first['sourceHash'])
        for _ in range(100):
            updated = self.app.call('assistant.translation.page', payload)
            if updated['state'] == 'ready':
                break
            time.sleep(.03)
        self.assertEqual(updated['state'], 'ready')
        self.assertEqual(len(self.seen), 2)
        self.assertIn('调度', self.seen[-1]['body']['messages'][1]['content'])

    def test_alignment_returns_only_an_existing_translation_fragment(self):
        self.app.call('assistant.configure', {'apiKey': 'local-test-key'})
        text = json.dumps({'sourceParagraph': 'Scheduling under dual resource constraints.',
                           'translatedParagraph': '双资源约束下的调度问题。', 'selectedText': 'Scheduling'})
        run = self.app.call('assistant.run', {'itemId': self.item['id'], 'task': 'align_translation',
                                             'scope': 'selection', 'page': 1, 'text': text})
        self.wait_job(run['jobId'])
        saved = self.app.call('assistant.runs', {'itemId': self.item['id']})[0]
        self.assertEqual(saved['result']['alignedTarget'], '调度')
        self.assertEqual(self.app.call('assistant.runs', {'itemId': self.item['id'], 'task': 'align_translation'})[0]['id'], saved['id'])
        self.assertEqual(self.app.call('assistant.runs', {'itemId': self.item['id'], 'publicOnly': True}), [])
        self.assertEqual(self.seen[-1]['body']['max_tokens'], 300)
        self.assertEqual(self.seen[-1]['body']['temperature'], 0)
        self.assertIn('双语文本对齐器', self.seen[-1]['body']['messages'][0]['content'])


if __name__ == '__main__':
    unittest.main()
