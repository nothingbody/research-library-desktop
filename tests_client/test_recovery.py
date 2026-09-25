from contextlib import closing
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from client_backend.common import AppError
from client_backend.downloads import Downloads
from client_backend.service import Application
from robust_collector import Store
from robust_runtime import AdaptiveRate, Sessions, Transport
from async_runtime import AsyncNetwork
from tests_client.test_backend import fixture_pdf
from test_robust_collector import success, listing


class RecoveryTest(unittest.TestCase):
    def test_collector_interactive_priority_and_related_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder)
            try:
                store.apply(store.claim('list'), success(listing([1, 2])))
                self.assertTrue(store.request_client(2, 'detail'))
                task = store.claim('detail')
                self.assertEqual(task['payload']['id'], 2)
                self.assertFalse(store.request_client(999, 'detail'))
                self.assertFalse(store.request_client(True, 'related'))
                self.assertTrue(store.request_client(1, 'related'))
                task = store.claim('related')
                data = {'same_category': [{'id': 2}], 'recent_papers': []}
                store.apply(task, success(data))
                self.assertEqual(json.loads(store.db.execute('SELECT data_json FROM journal_related WHERE journal_id=1').fetchone()[0]), data)
            finally:
                store.db.close()

    def test_async_related_get_transport_uses_existing_archive(self):
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                observed.append(self.path)
                raw = json.dumps({'code':0,'data':{'same_category':[],'recent_papers':[]}}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever);thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                sessions=Sessions();sessions.set('Bearer local-fixture-only')
                tx=Transport(Path(folder),AdaptiveRate(4),sessions)
                with AsyncNetwork(tx,base_url=f'http://127.0.0.1:{server.server_port}') as pool:
                    result=pool.submit(None,'related',{'id':3078},'related:3078',1).result(10)
                self.assertTrue(result['ok'])
                self.assertTrue((Path(folder)/result['raw']).is_file())
                self.assertEqual(observed,['/api/v1/public/journal-seo/related?journal_id=3078'])
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_download_rejects_html_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);fixture_pdf(root/'fixture.pdf');raw=(root/'fixture.pdf').read_bytes()
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args):pass
                def do_GET(self):
                    data=b'<html>Sign in</html>' if self.path=='/login' else raw
                    self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever);thread.start()
            app=Application(root/'library')
            try:
                item=app.library.create({'title':'download'})
                url=f'http://127.0.0.1:{server.server_port}'
                # The production SSRF guard blocks 127.0.0.1; bypass it only for this local fixture server.
                with unittest.mock.patch('client_backend.downloads.public_url', side_effect=lambda value: str(value).strip()):
                    with self.assertRaises(AppError):app.downloads.pdf({'url':url+'/login','itemId':item['id']},lambda *a:None)
                    first=app.downloads.pdf({'url':url+'/paper.pdf','itemId':item['id']},lambda *a:None)
                    again=app.downloads.pdf({'url':url+'/paper.pdf','itemId':item['id']},lambda *a:None)
                self.assertEqual(first['attachmentId'],again['attachmentId'])
                self.assertEqual(app.library.stats()['attachments'],1)
            finally:
                app.close();server.shutdown();server.server_close();thread.join()

    def test_interrupted_import_job_recovers_on_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            fixture_pdf(root/'fixture.pdf')
            app=Application(root/'library')
            preview=app.imports.preview([str(root/'fixture.pdf')])
            from client_backend.common import dumps, now
            with app.library.db(True) as db:
                db.execute("INSERT INTO jobs(id,kind,payload,state,created_at,updated_at) VALUES('interrupted','import',?,'running',?,?)",(dumps({'batchId':preview['batchId']}),now(),now()))
            app.close()
            resumed=Application(root/'library')
            try:
                for _ in range(80):
                    job=next(j for j in resumed.jobs.list() if j['id']=='interrupted')
                    if job['state'] in ('completed','failed'):break
                    time.sleep(.05)
                self.assertEqual(job['state'],'completed')
                self.assertEqual(resumed.library.stats()['items'],1)
                self.assertEqual(resumed.library.stats()['attachments'],1)
            finally:
                resumed.close()


if __name__ == '__main__':unittest.main()
