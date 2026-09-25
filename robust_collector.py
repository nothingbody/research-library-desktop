"""Durable, independently scheduled journal discovery, detail and verification tasks."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED, CancelledError
from collections import deque
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import queue
import gzip
import hashlib

from robust_runtime import AdaptiveRate, Sessions, Transport, atomic_json, compact, export_snapshot, id_hash, now
from partition_collector import PartitionStore, PART_KINDS


class Store(PartitionStore):
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root/'journals.sqlite3', timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS journals (
          id INTEGER PRIMARY KEY, canonical_name TEXT, list_json TEXT NOT NULL,
          detail_json TEXT, detail_status TEXT NOT NULL DEFAULT 'pending', fetched_at TEXT, error TEXT);
        CREATE TABLE IF NOT EXISTS journal_issns (journal_id INTEGER NOT NULL, issn TEXT NOT NULL,
          PRIMARY KEY(journal_id,issn));
        CREATE TABLE IF NOT EXISTS journal_sources (journal_id INTEGER NOT NULL, source TEXT NOT NULL,
          data_json TEXT, PRIMARY KEY(journal_id,source));
        CREATE TABLE IF NOT EXISTS checkpoint (key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS tasks (
          task_key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          next_retry REAL NOT NULL DEFAULT 0, error TEXT, updated_at TEXT NOT NULL,
          raw_path TEXT, response_sha256 TEXT);
        CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(kind,state,next_retry);
        CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state);
        CREATE INDEX IF NOT EXISTS journals_detail_state ON journals(detail_status,id);
        CREATE TABLE IF NOT EXISTS journal_related(journal_id INTEGER PRIMARY KEY,data_json TEXT NOT NULL,fetched_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batches (
          page INTEGER PRIMARY KEY, page_size INTEGER NOT NULL, row_count INTEGER NOT NULL,
          first_id INTEGER,last_id INTEGER, ids_sha256 TEXT NOT NULL,
          verified INTEGER NOT NULL DEFAULT 0, legacy INTEGER NOT NULL DEFAULT 0);
        ''')
        self.init_partition_schema()
        self.claim_turns={}
        with self.db:
            self.db.execute("UPDATE tasks SET state='pending',next_retry=0,updated_at=? WHERE state='running'", (now(),))
        self.migrate()
        self.recover_degraded_details()
        self.resume_partition_boundaries()

    def recover_degraded_details(self):
        candidates={key:json.loads(payload)['id'] for key,payload in self.db.execute(
            "SELECT task_key,payload FROM tasks WHERE kind='detail' AND state='blocked' AND error='Detail identity mismatch'")}
        log=self.root/'requests.jsonl'
        if not candidates or not log.exists():
            return
        latest={}
        with log.open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    event=json.loads(line)
                except ValueError:
                    continue
                if event.get('task') in candidates and event.get('raw'):
                    latest[event['task']]=event
        recovered=[]
        with self.db:
            for key,event in latest.items():
                try:
                    raw=gzip.decompress((self.root/event['raw']).read_bytes())
                    if hashlib.sha256(raw).hexdigest()!=event.get('sha256'):
                        continue
                    obj=json.loads(raw)
                    data=obj.get('data',{})
                    if obj.get('code')!=0 or data.get('degraded') is not True or data.get('id')!=candidates[key]:
                        continue
                except (OSError,ValueError,TypeError,AttributeError):
                    continue
                self.db.execute("UPDATE tasks SET state='retry',attempts=0,next_retry=0,error=?,raw_path=?,response_sha256=?,updated_at=? WHERE task_key=?",
                    ('Verified legacy degraded response; retry full detail',event['raw'],event['sha256'],now(),key))
                recovered.append({'task':key,'raw':event['raw'],'sha256':event['sha256']})
        if recovered:
            atomic_json(self.root/'degraded_recovery.json',{'at':now(),'requeued':len(recovered),'evidence':recovered})

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM checkpoint WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO checkpoint VALUES (?,?)', (key, compact(value)))

    def enqueue(self, key, kind, payload):
        self.db.execute('INSERT OR IGNORE INTO tasks(task_key,kind,payload,updated_at,partition_id) VALUES(?,?,?,?,?)',
                        (key, kind, compact(payload), now(),payload.get('_partition')))

    def request_client(self, journal_id, kind):
        """Interactive requests share this writer, durable queue and network limiter."""
        if kind not in ('detail', 'related') or not isinstance(journal_id, int) or isinstance(journal_id, bool) or journal_id <= 0:
            return False
        if not self.db.execute('SELECT 1 FROM journals WHERE id=?', (journal_id,)).fetchone():
            return False
        key = kind + ':' + str(journal_id)
        with self.db:
            self.enqueue(key, kind, {'id': journal_id})
            self.db.execute("UPDATE tasks SET state='pending',attempts=0,next_retry=-1,error=NULL,updated_at=? WHERE task_key=? AND state<>'running'", (now(), key))
        return True

    def list_payload(self, page):
        return {'type': 'journal', 'page': page, 'page_size': self.get('page_size',50),
                'sort_by': 'id', 'sort_dir': 'asc'}

    def migrate(self):
        with self.db:
            if not self.get('robust_migrated'):
                old_size = self.get('page_size',20)
                offset = (self.get('next_page',1)-1)*old_size
                if offset % 50:
                    raise ValueError('Legacy checkpoint is not aligned to 50 rows')
                self.put('page_size',50)
                self.put('next_page',offset//50+1)
                ids = [r[0] for r in self.db.execute('SELECT id FROM journals ORDER BY id')]
                if len(ids) != offset and not self.get('list_exhausted',False):
                    raise ValueError('Legacy checkpoint and record count disagree')
                for start in range(0,len(ids),50):
                    segment=ids[start:start+50]
                    self.db.execute('INSERT OR IGNORE INTO batches VALUES(?,?,?,?,?,?,0,1)',
                                    (start//50+1,50,len(segment),segment[0],segment[-1],id_hash(segment)))
                self.put('robust_migrated',now())
            # SQL-generated payloads contain only local numeric identifiers.
            self.db.execute('''INSERT OR IGNORE INTO tasks(task_key,kind,payload,updated_at)
              SELECT 'detail:'||id,'detail','{"id":'||id||'}',? FROM journals WHERE detail_status!='ok' ''',(now(),))
            if not self.get('list_exhausted',False):
                page=self.get('next_page',1)
                self.enqueue(f'list:{page}','list',self.list_payload(page))
            if self.get('stats_start') is None:
                self.enqueue('stats:start','stats',{})
            previous=self.get('next_page',1)-1
            if previous>0:
                key='boundary:'+str(time.time_ns())
                self.enqueue(key,'boundary',self.list_payload(previous))
                self.put('boundary_task',key)
            # Preserve recorded access/coverage failures across restarts. A successful
            # new list commit is the event that clears a previous list blocker.
            if self.get('list_exhausted'):
                self.seed_verification()

    def seed_verification(self):
        for page, in self.db.execute('SELECT page FROM batches WHERE verified=0').fetchall():
            self.enqueue(f'verify:{page}','verify',self.list_payload(page))

    def claim(self, kind):
        if kind in PART_KINDS and self.get('partitions_paused',False):
            return None
        with self.db:
            turn=self.claim_turns.get(kind,0)
            self.claim_turns[kind]=turn+1
            preferred='retry' if turn%2==0 else 'pending'
            query='''SELECT task_key,payload,attempts FROM tasks t
              WHERE kind=? AND state=? AND next_retry<=?
              AND (partition_id IS NULL OR EXISTS (SELECT 1 FROM partitions p WHERE p.id=t.partition_id AND p.state IN ('active','verifying')))
              AND (kind!='partition' OR NOT EXISTS (SELECT 1 FROM tasks b
                WHERE b.partition_id=t.partition_id AND b.kind='partition_boundary' AND b.state!='complete'))
              AND (kind!='partition' OR EXISTS (SELECT 1 FROM partitions p WHERE p.id=t.partition_id
                AND p.direction=json_extract(t.payload,'$.sort_dir')))
              AND (kind!='partition' OR NOT EXISTS (SELECT 1 FROM tasks b
                WHERE b.partition_id=t.partition_id AND b.kind='partition' AND b.state='running'))
              ORDER BY next_retry,rowid LIMIT 1'''
            row=self.db.execute(query,(kind,preferred,time.time())).fetchone()
            if row is None:
                row=self.db.execute(query,(kind,'pending' if preferred=='retry' else 'retry',time.time())).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE tasks SET state='running',attempts=attempts+1,updated_at=? WHERE task_key=?",(now(),row[0]))
        return {'key':row[0],'kind':kind,'payload':json.loads(row[1]),'attempt':row[2]+1}

    def resume_auth(self):
        with self.db:
            self.db.execute("UPDATE tasks SET state='pending',next_retry=0,error=NULL WHERE state='auth_wait'")

    def retry_failed(self):
        with self.db:
            self.db.execute("UPDATE tasks SET state='retry',attempts=0,next_retry=0,error=NULL WHERE state='failed'")

    def apply(self, task, result):
        key,kind=task['key'],task['kind']
        with self.db:
            if not result['ok']:
                category=result['category']
                state='auth_wait' if category=='auth' else ('retry' if category=='transient' and task['attempt']<6 else ('failed' if category=='transient' else 'blocked'))
                self.db.execute('UPDATE tasks SET state=?,next_retry=?,error=?,updated_at=?,raw_path=?,response_sha256=? WHERE task_key=?',
                    (state,time.time()+result.get('delay',0),compact({k:v for k,v in result.items() if k!='data'}),now(),result.get('raw'),result.get('sha256'),key))
                if kind=='detail':
                    self.db.execute("UPDATE journals SET detail_status='error',error=? WHERE id=?",(result.get('message'),task['payload']['id']))
                if state=='blocked' and kind in ('list','verify','boundary'):
                    self.put('list_blocker',{'task':key,'status':result.get('status'),'code':result.get('code'),'message':result.get('message')})
                if state in ('blocked','failed') and kind in PART_KINDS:
                    self.partition_failure(task,result.get('message','Partition request failed'))
                return
            data=result['data']
            if kind in PART_KINDS:
                self.apply_partition(task,result)
            elif key.startswith('stats:partition-'):
                self.apply_partition_stats(task,data)
            elif kind in ('list','verify','boundary'):
                self.apply_list(task,data)
            elif kind=='detail':
                self.apply_detail(task['payload']['id'],data)
            elif kind=='related':
                if not isinstance(data, dict) or not any(k in data for k in ('same_category', 'recent_papers', 'category_hub', 'country_hub')):
                    raise ValueError('Related content response has unexpected structure')
                self.db.execute('INSERT OR REPLACE INTO journal_related VALUES(?,?,?)', (task['payload']['id'], compact(data), now()))
            elif key=='stats:start':
                self.put('stats_start',data)
                self.put('stats_latest',data)
            else:
                if key=='stats:end' and self.get('partitions_enabled'):
                    self.apply_partition_stats(task,data)
                self.put('stats_end',data)
                self.put('stats_latest',data)
            self.db.execute("UPDATE tasks SET state='complete',error=NULL,updated_at=?,raw_path=?,response_sha256=? WHERE task_key=?",
                            (now(),result.get('raw'),result.get('sha256'),key))

    def apply_list(self, task, data):
        rows=data.get('rows')
        if not isinstance(rows,list):
            raise ValueError('Missing rows array')
        ids=[row.get('unified',{}).get('id') for row in rows]
        if any(type(i) is not int or i<=0 for i in ids) or ids!=sorted(set(ids)):
            raise ValueError('Invalid, unordered or duplicate IDs')
        page=task['payload']['page']
        meta=data.get('pageMeta',{})
        more=data.get('hasMore',meta.get('has_more'))
        if data.get('truncated') or meta.get('truncated') or type(more) is not bool or (more and not rows):
            raise ValueError('Truncated or ambiguous pagination')
        if task['kind'] in ('verify','boundary'):
            expected=self.db.execute('SELECT ids_sha256 FROM batches WHERE page=?',(page,)).fetchone()
            if not expected or id_hash(ids)!=expected[0]:
                raise ValueError('Source page IDs changed; source snapshot needs reconciliation')
            if task['kind']=='verify':
                self.db.execute('UPDATE batches SET verified=1 WHERE page=?',(page,))
            return
        if page!=self.get('next_page',1):
            raise ValueError('Out-of-order page commit')
        if ids and ids[0]<=self.get('last_id',0):
            raise ValueError('Page overlaps committed IDs')
        for row in rows:
            unified=row['unified']
            self.db.execute('INSERT INTO journals(id,canonical_name,list_json) VALUES(?,?,?)',
                            (unified['id'],unified.get('canonical_name'),compact(row)))
            self.enqueue(f"detail:{unified['id']}",'detail',{'id':unified['id']})
        self.db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,0,0)',
                        (page,self.get('page_size'),len(ids),ids[0] if ids else None,ids[-1] if ids else None,id_hash(ids)))
        self.put('next_page',page+1)
        if ids:
            self.put('last_id',ids[-1])
        self.put('list_blocker',None)
        if more:
            self.enqueue(f'list:{page+1}','list',self.list_payload(page+1))
        else:
            self.put('list_exhausted',True)
            self.seed_verification()

    def apply_detail(self, journal_id, data):
        unified=data.get('unified',{})
        if unified.get('id')!=journal_id:
            raise ValueError('Detail identity mismatch')
        self.db.execute("UPDATE journals SET detail_json=?,detail_status='ok',fetched_at=?,error=NULL WHERE id=?",(compact(data),now(),journal_id))
        issns=[]
        for key in ('issn_l','print_issn','electronic_issn'):
            issns.extend(str(unified.get(key) or '').split(','))
        extra=unified.get('all_issns') or []
        if isinstance(extra,str):
            extra=extra.split(',')
        issns.extend(v for v in extra if isinstance(v,str))
        self.db.execute('DELETE FROM journal_issns WHERE journal_id=?',(journal_id,))
        for value in sorted(set(v.strip() for v in issns if v.strip())):
            self.db.execute('INSERT INTO journal_issns VALUES(?,?)',(journal_id,value))
        sources=data.get('sources',{})
        self.db.execute('DELETE FROM journal_sources WHERE journal_id=?',(journal_id,))
        if isinstance(sources,dict):
            self.db.executemany('INSERT INTO journal_sources VALUES(?,?,?)',((journal_id,k,compact(v)) for k,v in sources.items()))

    def validation_failure(self, task, exc):
        # The failed apply transaction rolled back. Never advance its checkpoint.
        with self.db:
            self.db.execute("UPDATE tasks SET state='blocked',error=?,updated_at=? WHERE task_key=?",(str(exc),now(),task['key']))
            if task['kind'] in ('list','boundary','verify'):
                self.put('list_blocker',{'task':task['key'],'message':str(exc)})
            if task['kind'] in PART_KINDS:
                self.partition_failure(task,str(exc))

    def active_task_counts(self):
        clause="AND kind NOT IN ('list','verify','boundary')" if self.get('partitions_enabled') else ''
        ready=self.db.execute("""SELECT COUNT(*) FROM tasks t WHERE state IN ('pending','retry','running')
          AND (partition_id IS NULL OR EXISTS (SELECT 1 FROM partitions p WHERE p.id=t.partition_id AND p.state IN ('active','verifying'))) """+clause).fetchone()[0]
        bad=self.db.execute("SELECT COUNT(*) FROM tasks WHERE state IN ('blocked','failed','auth_wait') "+clause).fetchone()[0]
        if self.get('partitions_enabled'):
            bad+=self.db.execute("SELECT COUNT(*) FROM partitions WHERE state='blocked'").fetchone()[0]
        return ready,bad

    def discovery_finished(self):
        return self.partitions_verified() if self.get('partitions_enabled') else self.get('list_exhausted')

    def summary(self):
        count=self.db.execute('SELECT COUNT(*) FROM journals').fetchone()[0]
        details=self.db.execute("SELECT COUNT(*) FROM journals WHERE detail_status='ok'").fetchone()[0]
        task_counts={}
        for kind,state,n in self.db.execute('SELECT kind,state,COUNT(*) FROM tasks GROUP BY kind,state'):
            task_counts.setdefault(kind,{})[state]=n
        baseline=self.get('stats_start',{})
        ending=self.get('stats_end',{})
        keys=('totalJournals','matcherBuiltAt','matcherSchemaVersion')
        stable=bool(ending) and all(baseline.get(k)==ending.get(k) for k in keys)
        batches,verified=self.db.execute('SELECT COUNT(*),COALESCE(SUM(verified),0) FROM batches').fetchone()
        incomplete=self.db.execute("SELECT COUNT(*) FROM tasks WHERE state!='complete'").fetchone()[0]
        end_task=self.db.execute("SELECT state FROM tasks WHERE task_key='stats:end'").fetchone()
        complete=bool(count==details==baseline.get('totalJournals') and self.get('list_exhausted') and batches==verified
                      and not incomplete and stable and end_task and end_task[0]=='complete' and not self.get('list_blocker'))
        partition_info=None
        if self.get('partitions_enabled'):
            partition_info=self.partition_status()
            ready,bad=self.active_task_counts()
            audit=self.coverage_audit()
            complete=bool(count==details==baseline.get('totalJournals') and self.partitions_verified()
                          and audit['source_counts_match'] and audit['partition_union']==count
                          and not ready and not bad and stable and end_task and end_task[0]=='complete')
        return {'updated_at':now(),'complete':complete,'expected_journals':baseline.get('totalJournals'),
                'unique_journals':count,'details_saved':details,'missing_journals':max(0,baseline.get('totalJournals',count)-count),
                'pending_or_failed_details':count-details,'next_page':self.get('next_page',1),'page_size':self.get('page_size',50),
                'list_exhausted':self.get('list_exhausted',False),'list_blocker':self.get('list_blocker'),
                'tasks':task_counts,'batches':batches,'verified_batches':verified,'source_baseline_stable':stable,
                'partitioning':partition_info,
                'list_blocker_is_historical':bool(partition_info),
                'scope':'Search rows and complete public detail JSON; binary submission attachments are excluded.',
                'snapshot_note':'Completion requires ID sequence verification; live source fields are not a transactional source snapshot.'}


class Engine:
    def __init__(self, root, maximum=4, network_mode='thread'):
        if network_mode not in ('thread','async'):
            raise ValueError('network_mode must be thread or async')
        self.network_mode=network_mode
        self.network_pool=None
        self.inflight=0
        self.commit_times=deque(maxlen=600)
        self.root=Path(root)
        self.sessions=Sessions()
        self.rate=AdaptiveRate(maximum)
        self.rate.on_transition=self.record_rate_transition
        self.transport=Transport(self.root,self.rate,self.sessions)
        self.stop=threading.Event()
        self.pause=threading.Event()
        self.wake=threading.Event()
        self.retry=threading.Event()
        self.export_requested=threading.Event()
        self.state_lock=threading.Lock()
        self.state={'status':'starting','started_at':now(),'version':7,'network_mode':network_mode,'capabilities':['client_request','related_cache']}
        self.progress=deque(maxlen=7)
        self.commands=queue.SimpleQueue()

    def record_rate_transition(self, event):
        with (self.root/'rate_events.jsonl').open('a',encoding='utf-8') as stream:
            stream.write(compact(event)+'\n')

    def configure_rate(self, maximum):
        self.rate.set_maximum(maximum)
        self.wake.set()

    def current(self):
        with self.state_lock:
            return dict(self.state)

    def publish(self, store, status=None, **extra):
        summary=store.summary()
        metrics=self.rate.snapshot()
        tick=time.monotonic()
        self.progress.append((tick,summary['details_saved'],summary['unique_journals']))
        first=self.progress[0]
        elapsed=tick-first[0]
        committed_rps=(summary['details_saved']-first[1])/elapsed if elapsed>=10 else None
        discovered_rps=(summary['unique_journals']-first[2])/elapsed if elapsed>=10 else None
        detail_queue=summary['tasks'].get('detail',{})
        remaining=sum(detail_queue.get(s,0) for s in ('pending','retry','running'))
        with self.state_lock:
            self.state.update(extra,heartbeat_at=now(),rate=metrics,records=summary['unique_journals'],
                              details=summary['details_saved'],queue=summary['tasks'],next_page=summary['next_page'],
                              details_committed_rps=round(committed_rps,3) if committed_rps is not None else None,
                              estimated_remaining_seconds=round(remaining/committed_rps) if committed_rps and committed_rps>0 else None,
                              estimate_scope='Discovered runnable details only; excludes blocked/failed and undiscovered journals')
            self.state['partitioning']=summary['partitioning']
            self.state['new_journals_rps']=round(discovered_rps,3) if discovered_rps is not None else None
            self.state['scheduler']={'mode':store.get('scheduler_mode','balanced'),'workers':13,
                                     'detail_slots':9,'discovery_slots':2,'verification_slots':1}
            self.state['pipeline']={'inflight_tasks':self.inflight,
                'queued_tasks':sum(v.get('pending',0)+v.get('retry',0) for v in summary['tasks'].values()),
                'commit_mean_ms':round(sum(self.commit_times)/len(self.commit_times),2) if self.commit_times else None,
                'commit_p95_ms':round(sorted(self.commit_times)[int((len(self.commit_times)-1)*.95)],2) if self.commit_times else None,
                'network_p95_ms':metrics['latency_p95_ms']}
            if self.network_mode=='async' and self.network_pool:
                self.state['pipeline'].update(self.network_pool.snapshot())
            if status:
                self.state['status']=status
            snapshot=dict(self.state)
        atomic_json(self.root/'job_status.json',snapshot)
        atomic_json(self.root/'manifest.json',summary)
        if summary['partitioning']:
            atomic_json(self.root/'partition_status.json',summary['partitioning'])
        return summary

    def set_session(self, token):
        self.sessions.set(token)
        self.wake.set()

    def run(self):
        store=None
        pending={}
        last_publish=0
        seen_generation=-1
        last_export=None
        export_future=None
        last_audit=0
        try:
            store=Store(self.root)
            if store.get('scheduler_mode') is None:
                with store.db:
                    store.put('scheduler_mode','discovery_first')
            self.publish(store,'waiting_for_session')
            if self.network_mode=='async':
                from async_runtime import AsyncNetwork
                pool_context=AsyncNetwork(self.transport,max_workers=13)
            else:
                pool_context=ThreadPoolExecutor(max_workers=13)
            with pool_context as pool, ThreadPoolExecutor(max_workers=1) as exports:
                self.network_pool=pool
                while not self.stop.is_set() or pending:
                    self.inflight=len(pending)
                    boundary_state=None
                    while not self.commands.empty():
                        command=self.commands.get()
                        if command['action']=='start_partitions':
                            store.request_partitions(command['scope'])
                        elif command['action']=='audit_coverage':
                            atomic_json(self.root/'coverage_audit.json',store.coverage_audit())
                        elif command['action'] in ('pause_partitions','resume_partitions'):
                            with store.db:
                                store.put('partitions_paused',command['action']=='pause_partitions')
                        elif command['action']=='configure_scheduler':
                            with store.db:
                                store.put('scheduler_mode',command['mode'])
                        elif command['action']=='client_request':
                            store.request_client(command['journal_id'], command['kind'])
                    token,generation=self.sessions.read()
                    if token and generation!=seen_generation:
                        store.resume_auth()
                        seen_generation=generation
                    if self.retry.is_set():
                        store.retry_failed()
                        self.retry.clear()
                    if self.export_requested.is_set() and not export_future:
                        export_future=exports.submit(export_snapshot,self.root)
                        self.export_requested.clear()
                    if export_future and export_future.done():
                        try:
                            last_export=export_future.result()
                        except Exception as exc:
                            self.publish(store,export_error=type(exc).__name__+': '+str(exc))
                        export_future=None
                    if time.monotonic()-last_publish>=10:
                        store.prioritize_unseen()
                        if shutil.disk_usage(self.root).free<5*1024**3:
                            self.pause.set()
                            self.publish(store,'paused_low_disk')
                        elif not token:
                            self.publish(store,'awaiting_session',last_export=last_export)
                        elif self.pause.is_set():
                            self.publish(store,'paused',last_export=last_export)
                        else:
                            self.publish(store,'running',last_export=last_export)
                        last_publish=time.monotonic()
                    if store.get('partitions_enabled') and time.monotonic()-last_audit>=60:
                        atomic_json(self.root/'coverage_audit.json',store.coverage_audit())
                        last_audit=time.monotonic()
                    if store.partitions_verified() and not store.db.execute("SELECT 1 FROM tasks WHERE task_key='stats:partition-discovered'").fetchone():
                        with store.db:
                            store.enqueue('stats:partition-discovered','stats',{})
                    if token and not self.pause.is_set() and not self.stop.is_set():
                        boundary=store.get('boundary_task')
                        boundary_state=store.db.execute('SELECT state FROM tasks WHERE task_key=?',(boundary,)).fetchone() if boundary else None
                        boundary_ready=not boundary_state or boundary_state[0]=='complete'
                        active_kinds=[task['kind'] for task in pending.values()]
                        if not store.get('partitions_enabled') and 'boundary' not in active_kinds and boundary_state and boundary_state[0] in ('pending','retry'):
                            task=store.claim('boundary')
                            if task:
                                pending[pool.submit(self.transport.fetch,task['kind'],task['payload'],task['key'],task['attempt'])]=task
                        # A blocked list/boundary must never stop already discovered details.
                        kinds=[]
                        if not store.get('partitions_enabled') and boundary_ready and 'list' not in active_kinds:
                            kinds.append('list')
                        if not store.get('partitions_enabled') and 'verify' not in active_kinds:
                            kinds.append('verify')
                        if 'stats' not in active_kinds:
                            kinds.append('stats')
                        if 'related' not in active_kinds:
                            kinds.append('related')
                        partition_mode=store.get('partitions_enabled')
                        if partition_mode:
                            # Reserve progress for discovery, verification, and details.
                            for kind,slots in (('partition_boundary',1),('partition',2),('partition_verify',1)):
                                kinds += [kind]*max(0,slots-active_kinds.count(kind))
                        kinds += ['detail']*max(0,(9 if partition_mode and not store.get('partitions_paused') else 12)-active_kinds.count('detail'))
                        for kind in kinds:
                            if len(pending)>=13:
                                break
                            task=store.claim(kind)
                            if task:
                                wire={k:v for k,v in task['payload'].items() if not k.startswith('_')}
                                pending[pool.submit(self.transport.fetch,task['kind'],wire,task['key'],task['attempt'])]=task
                    if pending:
                        done,_=wait(pending,timeout=.25,return_when=FIRST_COMPLETED)
                        for future in done:
                            task=pending.pop(future)
                            try:
                                result=future.result()
                                commit_started=time.perf_counter()
                                store.apply(task,result)
                                self.commit_times.append((time.perf_counter()-commit_started)*1000)
                                if result['category']=='auth':
                                    self.sessions.invalidate(result['generation'])
                                    current_token,current_generation=self.sessions.read()
                                    if current_token and current_generation!=result['generation']:
                                        store.resume_auth()
                            except CancelledError:
                                with store.db:
                                    store.db.execute("UPDATE tasks SET state='pending',next_retry=0,updated_at=? WHERE task_key=? AND state='running'",(now(),task['key']))
                            except (ValueError,KeyError,TypeError,sqlite3.IntegrityError) as exc:
                                store.validation_failure(task,exc)
                    else:
                        ready,bad=store.active_task_counts()
                        if token and not ready and not bad and store.discovery_finished():
                            if not store.db.execute("SELECT 1 FROM tasks WHERE task_key='stats:end'").fetchone():
                                with store.db:
                                    store.enqueue('stats:end','stats',{})
                            else:
                                summary=self.publish(store,'exporting')
                                if not export_future:
                                    export_future=exports.submit(export_snapshot,self.root)
                                self.pause.set()
                                # Keep status completion stable while the control service remains available.
                                while not self.stop.wait(1):
                                    if export_future and export_future.done():
                                        last_export=export_future.result()
                                        export_future=None
                                    export_current=(last_export and last_export['records']==summary['unique_journals']
                                                    and last_export['details']==summary['details_saved'])
                                    if not export_current and not export_future:
                                        export_future=exports.submit(export_snapshot,self.root)
                                    self.publish(store,('complete' if summary['complete'] else 'incomplete') if export_current else 'exporting',last_export=last_export)
                                break
                        elif token and not ready and bad:
                            self.publish(store,'blocked',last_export=last_export)
                            if not last_export and not export_future:
                                export_future=exports.submit(export_snapshot,self.root)
                        elif token and boundary_state and boundary_state[0]=='blocked':
                            self.publish(store,'blocked_boundary',last_export=last_export)
                        self.wake.wait(.5)
                        self.wake.clear()
                self.publish(store,'stopped')
        except Exception as exc:
            with self.state_lock:
                self.state.update(status='failed',heartbeat_at=now(),error=type(exc).__name__+': '+str(exc))
                atomic_json(self.root/'job_status.json',dict(self.state))
        finally:
            self.sessions.set(None)
            if store:
                store.db.close()
