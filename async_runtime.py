"""Native asynchronous HTTP with a bounded bridge to the durable writer thread."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import aiohttp

from robust_runtime import ORIGIN, compact


async def acquire_rate(rate):
    # One shared limiter, including Retry-After cooldowns set by other requests.
    while True:
        with rate.lock:
            current = rate.clock()
            delay = max(rate.next_slot, rate.cooldown_until) - current
            if delay <= 0:
                rate.next_slot = current + 1 / rate.rate
                return
        await asyncio.sleep(min(delay, .5))


class AsyncNetwork:
    """Futures API lets the existing single SQLite owner retain transaction semantics.

    Only HTTP and rate waiting run on the event loop. Archival/compression use a
    separate bounded executor; the caller remains the sole SQLite writer.
    """
    def __init__(self, transport, max_workers=13, base_url=ORIGIN):
        self.transport, self.capacity, self.base_url = transport, max_workers, base_url
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.counts = dict(submitted=0, rate_waiting=0, network_active=0, archiving=0)
        self.start_error = None
        self.closed = False
        self.futures = set()

    def change(self, **changes):
        with self.lock:
            for key, delta in changes.items():
                self.counts[key] += delta

    def snapshot(self):
        with self.lock:
            return dict(self.counts)

    def __enter__(self):
        self.thread = threading.Thread(target=self.run_loop, name='journal-async-network')
        self.thread.start()
        self.ready.wait()
        if self.start_error:
            self.thread.join()
            raise self.start_error
        return self

    def run_loop(self):
        async def serve():
            try:
                self.loop = asyncio.get_running_loop()
                self.shutdown_event = asyncio.Event()
                self.archive = ThreadPoolExecutor(max_workers=4, thread_name_prefix='journal-archive')
                timeout = aiohttp.ClientTimeout(total=45, connect=15, sock_read=30)
                connector = aiohttp.TCPConnector(limit=self.capacity, limit_per_host=self.capacity)
                async with aiohttp.ClientSession(timeout=timeout, connector=connector,
                        headers={'User-Agent':'ScholayJournalArchive/6.0', 'Accept-Encoding':'gzip'}) as self.client:
                    self.ready.set()
                    await self.shutdown_event.wait()
                    # Cancelled bridge futures may still be unwinding archival awaits.
                    active = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                    for task in active:
                        task.cancel()
                    if active:
                        await asyncio.gather(*active, return_exceptions=True)
            except BaseException as exc:
                self.start_error = exc
                self.ready.set()
                raise
            finally:
                if hasattr(self, 'archive'):
                    self.archive.shutdown(wait=True)
        asyncio.run(serve())

    def submit(self, ignored_fetch, kind, payload, task_key, attempt):
        with self.lock:
            if self.closed:
                raise RuntimeError('Async network is closed')
            if self.counts['submitted'] >= self.capacity:
                raise RuntimeError('Async network queue capacity exceeded')
            self.counts['submitted'] += 1
        future = asyncio.run_coroutine_threadsafe(self.fetch(kind, payload, task_key, attempt), self.loop)
        with self.lock:
            self.futures.add(future)
        def completed(done):
            self.change(submitted=-1)
            with self.lock:
                self.futures.discard(done)
        future.add_done_callback(completed)
        return future

    async def fetch(self, kind, payload, task_key, attempt):
        endpoint = ('search' if kind in ('list','verify','boundary','partition','partition_verify','partition_boundary')
                    else 'get' if kind == 'detail' else 'related' if kind == 'related' else 'stats')
        self.change(rate_waiting=1)
        try:
            await acquire_rate(self.transport.rate)
        finally:
            self.change(rate_waiting=-1)
        token, generation = self.transport.sessions.read()
        if not token:
            return {'ok':False,'category':'auth','message':'Session required','generation':generation}
        status, body, retry_value, network_error = None, None, None, None
        started = time.monotonic()
        self.change(network_active=1)
        try:
            request_path = '/api/v1/public/journal-seo/related?journal_id=' + str(int(payload['id'])) if kind == 'related' else '/api/v1/public/journals/' + endpoint
            async with self.client.request('GET' if kind == 'related' else 'POST', self.base_url+request_path,
                    data=None if kind == 'related' else compact(payload).encode('utf-8'), allow_redirects=False,
                    headers={'Authorization':token,'Content-Type':'application/json'}) as response:
                status = response.status
                retry_value = response.headers.get('Retry-After')
                body = await response.read()
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            network_error = type(exc).__name__
        finally:
            self.change(network_active=-1)
        elapsed = time.monotonic()-started
        self.change(archiving=1)
        try:
            return await self.loop.run_in_executor(self.archive, self.transport.finish,
                kind, payload, task_key, attempt, endpoint, token, generation,
                status, body, retry_value, network_error, elapsed)
        finally:
            self.change(archiving=-1)

    def __exit__(self, exc_type, exc, traceback):
        with self.lock:
            self.closed = True
            outstanding = list(self.futures)
        # Normal shutdown drains; exceptional shutdown cancels without committing.
        for future in outstanding:
            if exc_type:
                future.cancel()
            else:
                try:
                    future.result()
                except Exception:
                    pass
        self.loop.call_soon_threadsafe(self.shutdown_event.set)
        self.thread.join()
