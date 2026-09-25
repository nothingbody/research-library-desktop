import asyncio
from concurrent.futures import CancelledError
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from async_runtime import AsyncNetwork, acquire_rate
from robust_runtime import AdaptiveRate, Sessions, Transport
from robust_collector import Engine, Store
from test_robust_collector import until, listing, success


class Fixture:
    def __init__(self, respond):
        self.active = self.peak = 0
        self.lock = threading.Lock()
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args): pass
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                with fixture.lock:
                    fixture.active += 1
                    fixture.peak = max(fixture.peak, fixture.active)
                try:
                    status, obj, headers = respond(self.path.rsplit('/',1)[-1], data, self.headers)
                    body = gzip.compress(json.dumps(obj).encode())
                    self.send_response(status)
                    self.send_header('Content-Encoding','gzip')
                    self.send_header('Content-Length',str(len(body)))
                    for key,value in headers.items(): self.send_header(key,value)
                    self.end_headers()
                    self.wfile.write(body)
                finally:
                    with fixture.lock: fixture.active -= 1
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        return self
    def __exit__(self,*args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def transport(root):
    rate = AdaptiveRate(10)
    rate.rate = 1000  # Local fixture only; never used against the remote service.
    sessions = Sessions()
    sessions.set('Bearer fixture-not-a-real-token')
    return Transport(root,rate,sessions)


class AsyncTests(unittest.TestCase):
    def test_native_concurrency_and_archival_integrity(self):
        def respond(endpoint,data,headers):
            time.sleep(.12)
            return 200, {'code':0,'data':{'unified':{'id':data['id']},'sources':{}}}, {}
        with tempfile.TemporaryDirectory() as directory, Fixture(respond) as server:
            root = Path(directory)
            tx = transport(root)
            with AsyncNetwork(tx,base_url=server.url) as pool:
                futures = [pool.submit(None,'detail',{'id':i},f'detail:{i}',1) for i in range(6)]
                results = [future.result(5) for future in futures]
            self.assertGreaterEqual(server.peak,2)
            for i,result in enumerate(results):
                self.assertTrue(result['ok'])
                raw = gzip.decompress((root/result['raw']).read_bytes())
                self.assertEqual(hashlib.sha256(raw).hexdigest(),result['sha256'])
                self.assertEqual(json.loads(raw)['data'],result['data'])
                self.assertEqual(result['data']['unified']['id'],i)
            self.assertNotIn('fixture-not-a-real-token',(root/'requests.jsonl').read_text())

    def test_429_and_degraded_are_retryable(self):
        for status,data in [(429,{}),(200,{'code':0,'data':{'id':1,'degraded':True}})]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                with Fixture(lambda *args:(status,data,{'Retry-After':'120'} if status==429 else {})) as server:
                    tx = transport(Path(directory))
                    with AsyncNetwork(tx,base_url=server.url) as pool:
                        result = pool.submit(None,'detail',{'id':1},'detail:1',1).result(5)
                    self.assertEqual(result['category'],'transient')
                    self.assertNotIn('data',result)
                    if status==429:
                        self.assertGreaterEqual(result['delay'],120)
                        self.assertGreater(tx.rate.snapshot()['cooldown_seconds'],118)

    def test_bounded_queue_cancel_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root)
            store.apply(store.claim('list'),success(listing([1])))
            task = store.claim('detail')
            store.db.close()
            tx = transport(root)
            tx.rate.next_slot = time.monotonic()+30
            with AsyncNetwork(tx,max_workers=1) as pool:
                future = pool.submit(None,task['kind'],task['payload'],task['key'],task['attempt'])
                with self.assertRaises(RuntimeError):
                    pool.submit(None,'stats',{},'stats:test',1)
                future.cancel()
                with self.assertRaises(CancelledError): future.result()
            restored = Store(root)
            try:
                self.assertEqual(restored.claim('detail')['key'],task['key'])
                self.assertEqual(restored.db.execute("SELECT COUNT(*) FROM journals WHERE detail_status='ok'").fetchone()[0],0)
            finally: restored.db.close()
            self.assertFalse((root/'requests.jsonl').exists())

    def test_complete_pipeline_and_auth_renewal(self):
        def respond(endpoint,data,headers):
            if headers['Authorization']=='Bearer expired-fixture':
                return 401,{'code':401},{}
            if endpoint=='stats':
                result={'totalJournals':2,'matcherBuiltAt':'fixture'}
            elif endpoint=='search': result=listing([1,2])
            else: result={'unified':{'id':data['id']},'sources':{}}
            return 200,{'code':0,'data':result},{}
        with tempfile.TemporaryDirectory() as directory, Fixture(respond) as server:
            root=Path(directory)
            engine=Engine(root,10,'async')
            engine.rate.rate=1000
            def factory(tx,**kwargs): return AsyncNetwork(tx,base_url=server.url,**kwargs)
            with patch('async_runtime.AsyncNetwork',side_effect=factory):
                engine.set_session('Bearer expired-fixture')
                thread=threading.Thread(target=engine.run)
                thread.start()
                try:
                    until(lambda:engine.sessions.read()[0] is None)
                    engine.set_session('Bearer renewed-fixture')
                    until(lambda:engine.current().get('status')=='complete',10)
                    state=engine.current()
                    self.assertEqual(state['details'],2)
                    self.assertEqual(state['network_mode'],'async')
                    self.assertIsNotNone(state['pipeline']['commit_mean_ms'])
                finally:
                    engine.stop.set()
                    thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertTrue(json.loads((root/'manifest.json').read_text())['complete'])


class AsyncLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiters_recheck_new_cooldown_without_blocking_loop(self):
        tick=[0.0]
        rate=AdaptiveRate(10,clock=lambda:tick[0])
        rate.next_slot=1
        sleeps=[]
        async def advance(delay):
            sleeps.append(delay)
            tick[0]+=delay
            if len(sleeps)==1:
                rate.observe(False,.1,429,delay=3)
        with patch('async_runtime.asyncio.sleep',side_effect=advance):
            await acquire_rate(rate)
        self.assertGreaterEqual(tick[0],5.5)
        self.assertLessEqual(max(sleeps),.5)


if __name__=='__main__': unittest.main()
