from __future__ import annotations

import itertools
import json
import queue
import threading

from .common import AppError, dumps, now, require, uid

# Lower runs first. Interactive kinds a user is actively waiting on jump ahead of
# batch work such as imports, indexing and backups.
PRIORITIES = {
    'caj.convert': 10,
    'metadata.lookup': 10, 'fulltext.obtain': 10, 'pdf.download': 10,
    'assistant.run': 10, 'research-ask.run': 10, 'ai-search.verify': 10, 'ai-search.rerank': 10,
    'ai-search.run': 20, 'ai-search.expand': 20, 'ai-search.citations': 20,
    'relations.profile': 20, 'relations.run': 20, 'relations.discover': 20,
}
DEFAULT_PRIORITY = 30
WORKERS = 3
STOP = (99, 0, None)


class Jobs:
    def __init__(self, library, emit=lambda *args: None, workers=WORKERS):
        self.library, self.emit = library, emit
        self.handlers = {}
        self.closing = threading.Event()
        self.active, self.lock = set(), threading.Lock()
        self.queue = queue.PriorityQueue()
        self.sequence = itertools.count()
        self.pool = [threading.Thread(target=self._worker, name=f'library-job-{index}', daemon=True)
                     for index in range(workers)]
        for thread in self.pool:
            thread.start()

    def recover(self, kinds=None, submit=True):
        with self.library.db(True) as db:
            where, args = '', []
            if kinds is not None:
                kinds = list(kinds)
                if not kinds:
                    return
                where = ' AND kind IN (' + ','.join('?' for _ in kinds) + ')'
                args = kinds
            db.execute("UPDATE jobs SET state='pending',message='上次运行中断，继续处理',updated_at=? WHERE state='running'" + where, [now(), *args])
            rows = db.execute("SELECT id,kind FROM jobs WHERE state='pending'" + where, args).fetchall()
        if submit:
            for job_id, kind in rows:
                self._submit(job_id, kind)

    def create(self, kind, payload):
        require(kind in self.handlers, '不支持的后台任务')
        with self.library.db(True) as db:
            old = db.execute("SELECT id FROM jobs WHERE kind=? AND payload=? AND state IN ('pending','running')", (kind, dumps(payload))).fetchone()
            if old:
                return {'jobId': old[0], 'existing': True}
            job_id = uid()
            db.execute('INSERT INTO jobs(id,kind,payload,state,created_at,updated_at) VALUES(?,?,?,\'pending\',?,?)', (job_id, kind, dumps(payload), now(), now()))
        self._submit(job_id, kind)
        return {'jobId': job_id}

    def _submit(self, job_id, kind=None):
        if kind is None:
            with self.library.db() as db:
                row = db.execute('SELECT kind FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row:
                return
            kind = row[0]
        with self.lock:
            if job_id in self.active or self.closing.is_set():
                return
            self.active.add(job_id)
        self.queue.put((PRIORITIES.get(kind, DEFAULT_PRIORITY), next(self.sequence), job_id))

    def _worker(self):
        while True:
            _, _, job_id = self.queue.get()
            if job_id is None:
                self.queue.task_done()
                return
            try:
                self._run(job_id)
            finally:
                with self.lock:
                    self.active.discard(job_id)
                self.queue.task_done()

    def _run(self, job_id):
        if self.closing.is_set():
            # Shutdown leaves queued jobs pending; recover() picks them up next start.
            return
        try:
            with self.library.db(True) as db:
                job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if not job or job['state'] != 'pending':
                    return
                db.execute("UPDATE jobs SET state='running',error=NULL,updated_at=? WHERE id=?", (now(), job_id))
            def progress(value=0, message=''):
                with self.library.db(True) as db:
                    state = db.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
                    if state == 'cancelled' or self.closing.is_set():
                        raise AppError('CANCELLED', '任务已暂停，可重新运行')
                    db.execute('UPDATE jobs SET progress=?,message=?,updated_at=? WHERE id=?', (max(0, min(1, value)), message, now(), job_id))
                self.emit('job.progress', {'jobId': job_id, 'progress': value, 'message': message})
            result = self.handlers[job['kind']](json.loads(job['payload']), progress)
            with self.library.db(True) as db:
                db.execute("UPDATE jobs SET state='completed',progress=1,result=?,message='已完成',updated_at=? WHERE id=? AND state<>'cancelled'", (dumps(result), now(), job_id))
            self.emit('data.changed', {'jobId': job_id, 'kind': job['kind']})
        except Exception as exc:
            interrupted = isinstance(exc, AppError) and exc.code == 'CANCELLED'
            state = ('pending' if self.closing.is_set() else 'cancelled') if interrupted else 'failed'
            error = {'code': getattr(exc, 'code', 'TASK_FAILED'), 'message': str(exc)[:1000]}
            with self.library.db(True) as db:
                db.execute('UPDATE jobs SET state=?,error=?,message=?,updated_at=? WHERE id=?', (state, dumps(error), str(exc)[:300], now(), job_id))
            self.emit('job.changed', {'jobId': job_id, 'state': state})

    def list(self):
        with self.library.db() as db:
            values = [dict(row) for row in db.execute('SELECT id,kind,state,progress,message,result,error,created_at AS createdAt,updated_at AS updatedAt FROM jobs ORDER BY created_at DESC LIMIT 200')]
            for value in values:
                for key in ('result', 'error'):
                    value[key] = json.loads(value[key]) if value[key] else None
            return values

    def action(self, job_id, action):
        with self.library.db(True) as db:
            row = db.execute('SELECT state,kind FROM jobs WHERE id=?', (job_id,)).fetchone()
            require(row, '任务不存在')
            if action == 'cancel':
                require(row[0] in ('pending', 'running'), '任务不在运行中')
                db.execute("UPDATE jobs SET state='cancelled',updated_at=? WHERE id=?", (now(), job_id))
            elif action == 'retry':
                require(row[0] in ('failed', 'cancelled'), '只能重试失败或已取消任务')
                with self.lock:
                    require(job_id not in self.active, '任务正在停止，请稍后重试')
                db.execute("UPDATE jobs SET state='pending',error=NULL,updated_at=? WHERE id=?", (now(), job_id))
            else:
                raise AppError('INVALID_ARGUMENT', '不支持的任务操作')
        if action == 'retry':
            self._submit(job_id, row[1])
        return {'ok': True}

    def close(self):
        self.closing.set()
        for _ in self.pool:
            self.queue.put(STOP)
        for thread in self.pool:
            thread.join(timeout=10)
