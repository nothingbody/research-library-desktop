"""Network, rate control and crash-safe storage helpers for the journal worker."""
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import http.client
import json
import os
from pathlib import Path
import random
import sqlite3
import threading
import time
import uuid

HOST = 'www.scholay.com'
ORIGIN = 'https://' + HOST


def now():
    return datetime.now(timezone.utc).isoformat()


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def retry_after(value, epoch=None):
    """Honor both HTTP Retry-After forms without shortening the server's delay."""
    if value is None:
        return 0.0
    epoch = time.time() if epoch is None else epoch
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - epoch)
        except (TypeError, ValueError, OverflowError):
            return 0.0


def id_hash(ids):
    return hashlib.sha256(compact(ids).encode()).hexdigest()


class InstanceLock:
    def __init__(self, path):
        self.path, self.stream = Path(path), None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        if self.stream.seek(0, 2) == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise RuntimeError('Another collector already holds the instance lock') from None
        return self

    def close(self):
        if self.stream:
            self.stream.close()
            self.stream = None


class AdaptiveRate:
    def __init__(self, maximum=4.0, clock=time.monotonic, sleep=time.sleep):
        self.lock = threading.Lock()
        self.clock, self.sleep = clock, sleep
        self.maximum, self.rate = maximum, min(2.0, maximum)
        self.next_slot, self.cooldown_until = 0.0, 0.0
        self.stage_since = clock()
        self.last_adjust = clock()
        self.clean_successes = 0
        self.observations = deque(maxlen=2000)
        self.transitions = []
        self.on_transition = None

    def record_transition(self, old, reason, status=None):
        event={'at': now(), 'from': old, 'to': self.rate,
               'maximum_rps': self.maximum, 'reason': reason, 'http_status': status}
        self.transitions.append(event)
        if self.on_transition:
            self.on_transition(event)

    def set_maximum(self, maximum):
        # Raising the ceiling does not bypass ramp-up or an active cooldown.
        if type(maximum) not in (int, float) or not .5 <= maximum <= 12:
            raise ValueError('max_rps must be a finite number between 0.5 and 12')
        with self.lock:
            old=self.rate
            self.maximum=float(maximum)
            self.rate=min(self.rate,self.maximum)
            self.stage_since=self.clock()
            self.clean_successes=0
            if self.rate != old:
                self.last_adjust=self.clock()
                self.next_slot=max(self.next_slot,self.clock()+1/self.rate)
            self.record_transition(old,'configuration')

    def acquire(self):
        # Recheck after every short sleep so a new global cooldown applies to waiters.
        while True:
            with self.lock:
                current = self.clock()
                delay = max(self.next_slot, self.cooldown_until) - current
                if delay <= 0:
                    self.next_slot = current + 1.0 / self.rate
                    return
            self.sleep(min(delay, 0.5))

    def observe(self, ok, latency, status, delay=0.0, attempt=1):
        with self.lock:
            current = self.clock()
            self.observations.append((current, ok, latency, status, attempt))
            old = self.rate
            reason='healthy_ramp'
            if status in (429, 503):
                reason='http_backoff'
                self.rate = max(0.5, self.rate / 2)
                self.cooldown_until = max(self.cooldown_until, current + max(delay, 5))
            elif delay:
                self.cooldown_until = max(self.cooldown_until, current + delay)
            if not ok:
                self.clean_successes = 0
                self.stage_since = current
            else:
                self.clean_successes += 1
            recent = list(self.observations)[-60:]
            if len(recent) >= 30:
                failure_fraction = sum(not v[1] for v in recent) / len(recent)
                p95 = sorted(v[2] for v in recent)[int((len(recent)-1)*0.95)]
                if current-self.last_adjust>=30 and (failure_fraction > 0.05 or p95 > 3):
                    reason='error_fraction' if failure_fraction > 0.05 else 'high_latency'
                    self.rate = max(0.5, self.rate / 2)
                    self.stage_since, self.clean_successes = current, 0
                elif current-self.stage_since>=30 and self.clean_successes >= 60 and p95 < 1.5:
                    self.rate = min(self.maximum, self.rate + 1)
                    self.stage_since, self.clean_successes = current, 0
            if self.rate != old:
                self.last_adjust=current
                self.record_transition(old,reason,status)

    def snapshot(self):
        with self.lock:
            current = self.clock()
            recent = [x for x in self.observations if x[0] >= current-60]
            span = max(1, min(60, current-self.observations[0][0])) if self.observations else 1
            durations = sorted(x[2] for x in recent)
            return {'limit_rps': self.rate, 'maximum_rps': self.maximum,
                    'cooldown_seconds': max(0, round(self.cooldown_until-current, 1)),
                    'recent_success_rps': round(sum(x[1] for x in recent)/span, 3),
                    'recent_requests': len(recent), 'recent_errors': sum(not x[1] for x in recent),
                    'recent_error_fraction': round(sum(not x[1] for x in recent)/max(1,len(recent)),4),
                    'recent_retry_fraction': round(sum(x[4]>1 for x in recent)/max(1,len(recent)),4),
                    'latency_p95_ms': round(1000*durations[int((len(durations)-1)*.95)]) if durations else None,
                    'transitions': self.transitions[-20:]}


class Sessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.token, self.generation = None, 0

    def set(self, value):
        with self.lock:
            self.token = value
            self.generation += 1
            return self.generation

    def read(self):
        with self.lock:
            return self.token, self.generation

    def invalidate(self, generation):
        with self.lock:
            if self.generation == generation:
                self.token = None


class Transport:
    """Thread-local persistent HTTPS connections; credentials never reach logs."""
    def __init__(self, root, rate, sessions):
        self.root, self.rate, self.sessions = root, rate, sessions
        self.local = threading.local()
        self.log_lock = threading.Lock()

    def fetch(self, kind, payload, task_key, attempt):
        endpoint = 'search' if kind in ('list', 'verify', 'boundary', 'partition', 'partition_verify', 'partition_boundary') else ('get' if kind == 'detail' else 'related' if kind == 'related' else 'stats')
        self.rate.acquire()
        token, generation = self.sessions.read()
        if not token:
            return {'ok': False, 'category': 'auth', 'message': 'Session required', 'generation': generation}
        if not getattr(self.local, 'connection', None):
            self.local.connection = http.client.HTTPSConnection(HOST, timeout=45)
        started = time.monotonic()
        status, body, retry_value = None, None, None
        try:
            connection = self.local.connection
            request_path = '/api/v1/public/journal-seo/related?journal_id=' + str(int(payload['id'])) if kind == 'related' else '/api/v1/public/journals/' + endpoint
            connection.request('GET' if kind == 'related' else 'POST', request_path,
                               None if kind == 'related' else compact(payload).encode('utf-8'),
                               {'Content-Type': 'application/json', 'Accept-Encoding': 'gzip',
                                'Authorization': token, 'User-Agent': 'ScholayJournalArchive/2.0'})
            response = connection.getresponse()
            status, body = response.status, response.read()
            retry_value = response.getheader('Retry-After')
            if response.getheader('Content-Encoding') == 'gzip':
                body = gzip.decompress(body)
            if response.will_close:
                connection.close()
                self.local.connection = None
        except (OSError, http.client.HTTPException, EOFError) as exc:
            self.local.connection.close()
            self.local.connection = None
            network_error = type(exc).__name__
        else:
            network_error = None
        elapsed = time.monotonic()-started
        return self.finish(kind, payload, task_key, attempt, endpoint, token, generation,
                           status, body, retry_value, network_error, elapsed)

    def finish(self, kind, payload, task_key, attempt, endpoint, token, generation,
               status, body, retry_value, network_error, elapsed):
        """Archive and classify identically for threaded and asynchronous HTTP."""
        raw, sha = None, None
        if body is not None:
            raw = Path('raw') / endpoint / (task_key.replace(':', '-') + '-' + uuid.uuid4().hex + '.json.gz')
            destination = self.root / raw
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = destination.with_suffix('.tmp')
            with temp.open('wb') as file:
                with gzip.GzipFile(fileobj=file, mode='wb', compresslevel=1) as compressed:
                    compressed.write(body)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp, destination)
            sha = hashlib.sha256(body).hexdigest()
        try:
            obj = json.loads(body) if body is not None else {}
            if not isinstance(obj, dict):
                obj = {}
        except (ValueError, UnicodeError):
            obj = {}
        code = obj.get('code')
        degraded = (kind=='detail' and isinstance(obj.get('data'),dict) and obj['data'].get('degraded') is True)
        ok = status == 200 and code == 0 and isinstance(obj.get('data'), dict) and not degraded
        if ok:
            category = 'success'
        elif status in (401, 403) or code == 4036:
            category = 'auth'
        elif degraded or network_error or status == 429 or (status is not None and status >= 500) or code == 4030 or (status == 200 and not obj):
            category = 'transient'
        else:
            category = 'permanent'
        delay = retry_after(retry_value)
        if not ok and category == 'transient':
            delay = max(delay, min(300, 2 ** min(attempt, 8)) + random.uniform(0, 1))
        self.rate.observe(ok, elapsed, status, delay if status in (429, 503) or retry_value else 0, attempt=attempt)
        message = 'Source returned degraded detail; complete detail required' if degraded else (network_error or str(obj.get('message', 'Unexpected response')))
        # Response messages are untrusted; scrub the only credential used by this worker.
        if token:
            message = message.replace(token, '[redacted]').replace(token.removeprefix('Bearer '), '[redacted]')
        result = {'ok': ok, 'category': category, 'status': status, 'code': code,
                  'message': message[:1000], 'delay': delay, 'generation': generation,
                  'raw': str(raw) if raw else None, 'sha256': sha, 'latency_ms': round(elapsed*1000)}
        with self.log_lock, (self.root/'requests.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(compact({'at': now(), 'endpoint': endpoint, 'request': payload,
                                  'task': task_key, 'attempt': attempt, 'http_status': status,
                                  'code': code, 'raw': result['raw'], 'sha256': sha,
                                  'latency_ms': result['latency_ms'], 'category': category})+'\n')
            stream.flush()
        if ok:
            result['data'] = obj['data']
        return result


def export_snapshot(root):
    """Publish a complete generation atomically, retaining the previous generation on failure."""
    generation = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex[:8]
    temporary = root/'exports'/('.'+generation+'.tmp')
    target = root/'exports'/generation
    temporary.mkdir(parents=True)
    db = sqlite3.connect((root/'journals.sqlite3').as_uri()+'?mode=ro', uri=True)
    fields, count, details = {}, 0, 0
    def walk(value, prefix=''):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, prefix+'.'+key if prefix else key)
        elif isinstance(value, list):
            fields.setdefault(prefix, set()).add('array')
            for child in value:
                walk(child, prefix+'[]')
        else:
            fields.setdefault(prefix, set()).add(type(value).__name__)
    try:
        db.execute('BEGIN')
        with (temporary/'journals.jsonl.gz').open('wb') as file:
            with gzip.GzipFile(fileobj=file, mode='wb', compresslevel=1) as output:
                for journal_id, listing, detail in db.execute('SELECT id,list_json,detail_json FROM journals ORDER BY id'):
                    record = {'id': journal_id, 'listing': json.loads(listing), 'detail': json.loads(detail) if detail else None}
                    walk(record)
                    output.write((compact(record)+'\n').encode())
                    count += 1
                    details += detail is not None
            file.flush()
            os.fsync(file.fileno())
        atomic_json(temporary/'field_dictionary.json', {k: sorted(v) for k,v in sorted(fields.items())})
        manifest = {'generation': generation, 'created_at': now(), 'records': count, 'details': details,
                    'note': 'Consistent local database snapshot; source-wide completion is in the live manifest.'}
        atomic_json(temporary/'snapshot.json', manifest)
        db.rollback()
        os.replace(temporary, target)
        atomic_json(root/'exports'/'CURRENT.json', manifest)
        return manifest
    finally:
        db.close()
