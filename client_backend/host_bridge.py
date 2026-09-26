"""Calls from the backend into the Electron main process ("host" calls).

Requests go out on stdout as {"host": {...}}; the stdin loop hands replies
({"hostReply": ...}) and progress updates ({"hostProgress": ...}) back here.
The calling job thread blocks until its reply arrives.
"""
from __future__ import annotations

import itertools
import threading
import time

from .common import AppError


class HostBridge:
    def __init__(self, send):
        self._send = send
        self._lock = threading.Lock()
        self._pending = {}
        self._ids = itertools.count(1)
        self._closed = False

    def call(self, method, params, timeout, progress=None):
        call_id = f'h{next(self._ids)}'
        slot = {'done': threading.Event(), 'reply': None, 'progress': None}
        with self._lock:
            if self._closed:
                raise AppError('HOST_UNAVAILABLE', '主进程连接已关闭，请重启应用')
            self._pending[call_id] = slot
        try:
            self._send({'host': {'id': call_id, 'method': method, 'params': params}})
            deadline, reported = time.monotonic() + timeout, None
            # Progress is reported from this (the job's) thread, not the stdin reader.
            while not slot['done'].wait(0.5):
                if progress and slot['progress'] is not None and slot['progress'] != reported:
                    reported = slot['progress']
                    progress(*reported)
                if time.monotonic() > deadline:
                    raise AppError('FULLTEXT_NETWORK', '全文下载超时，请稍后重试', retryable=True)
        finally:
            with self._lock:
                self._pending.pop(call_id, None)
        reply = slot['reply'] or {}
        if 'error' in reply:
            error = reply.get('error') or {}
            code = str(error.get('code') or 'HOST_ERROR')
            raise AppError(code, str(error.get('message') or '主进程调用失败')[:500], retryable=code == 'FULLTEXT_NETWORK')
        return reply.get('result') or {}

    def deliver(self, reply):
        with self._lock:
            slot = self._pending.get((reply or {}).get('id'))
        if slot:
            slot['reply'] = reply
            slot['done'].set()

    def progress(self, update):
        with self._lock:
            slot = self._pending.get((update or {}).get('id'))
        if slot:
            try:
                slot['progress'] = (int(update.get('bytes') or 0), int(update.get('total') or 0))
            except (TypeError, ValueError):
                pass

    def close(self):
        with self._lock:
            self._closed = True
            for slot in self._pending.values():
                slot['reply'] = {'error': {'code': 'HOST_UNAVAILABLE', 'message': '主进程连接已关闭，请重启应用'}}
                slot['done'].set()
