"""Transactional source partitions with bounded bidirectional offset traversal."""
import json
import re
import time
import uuid

from robust_runtime import compact, id_hash, now


PART_KINDS=('partition','partition_verify','partition_boundary')
PAGE_LIMIT=2001
PAGE_SIZE=50


def sources_of(unified):
    value=unified.get('sources')
    if isinstance(value,str):
        return {s.strip() for s in value.split(',') if s.strip()}
    if isinstance(value,dict):
        return {s for s,v in value.items() if v}
    if isinstance(value,list):
        return set(value)
    return set()


class PartitionStore:
    def init_partition_schema(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS partitions (
          id TEXT PRIMARY KEY, source TEXT NOT NULL, filters TEXT NOT NULL, parent_id TEXT,
          state TEXT NOT NULL DEFAULT 'active', direction TEXT NOT NULL DEFAULT 'asc',
          next_asc INTEGER NOT NULL DEFAULT 1, next_desc INTEGER NOT NULL DEFAULT 1,
          last_asc INTEGER, last_desc INTEGER, reason TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS partition_batches (
          partition_id TEXT NOT NULL, direction TEXT NOT NULL, page INTEGER NOT NULL,
          row_count INTEGER NOT NULL, first_id INTEGER, last_id INTEGER,
          ids_sha256 TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
          raw_path TEXT, response_sha256 TEXT,
          PRIMARY KEY(partition_id,direction,page));
        CREATE TABLE IF NOT EXISTS partition_memberships (
          partition_id TEXT NOT NULL, journal_id INTEGER NOT NULL,
          PRIMARY KEY(partition_id,journal_id));
        CREATE INDEX IF NOT EXISTS partition_members_journal ON partition_memberships(journal_id);
        CREATE TABLE IF NOT EXISTS partition_direction_members (
          partition_id TEXT NOT NULL, direction TEXT NOT NULL, journal_id INTEGER NOT NULL,
          PRIMARY KEY(partition_id,direction,journal_id));
        ''')
        if 'partition_id' not in {r[1] for r in self.db.execute('PRAGMA table_info(tasks)')}:
            self.db.execute('ALTER TABLE tasks ADD COLUMN partition_id TEXT')
            self.db.commit()
        self.db.execute('CREATE INDEX IF NOT EXISTS tasks_partition ON tasks(partition_id,kind,state)')

    def partition_payload(self, pid, direction, page):
        filters=json.loads(self.db.execute('SELECT filters FROM partitions WHERE id=?',(pid,)).fetchone()[0])
        return {'_partition':pid,'type':'journal','page':page,'page_size':PAGE_SIZE,
                'sort_by':'id','sort_dir':direction,**filters}

    def partition_enqueue(self, pid, direction, page, kind='partition', suffix=''):
        key=f'{kind}:{pid}:{direction}:{page}{suffix}'
        self.enqueue(key,kind,self.partition_payload(pid,direction,page))
        return key

    def request_partitions(self, scope):
        if scope not in ('pilot','all'):
            raise ValueError('Partition scope must be pilot or all')
        with self.db:
            self.enqueue('stats:partition-start:'+uuid.uuid4().hex,'stats',
                         {'_command':'start_partitions','_scope':scope})

    def apply_partition_stats(self, task, data):
        baseline=self.get('stats_start',{})
        keys=('totalJournals','matcherBuiltAt','matcherSchemaVersion')
        if any(data.get(k)!=baseline.get(k) for k in keys):
            raise ValueError('Source baseline changed; reconcile before enabling partitions')
        previous=self.get('partition_baseline')
        if previous and data.get('sourceCoverage')!=previous.get('sourceCoverage'):
            raise ValueError('Source coverage changed during partition acquisition')
        if task['payload'].get('_command')=='start_partitions':
            coverage=data.get('sourceCoverage')
            if not isinstance(coverage,dict) or not coverage:
                raise ValueError('Missing source coverage')
            if any(not re.fullmatch(r'[a-z0-9_]+',s) or type(n) is not int or n<0 for s,n in coverage.items()):
                raise ValueError('Invalid source coverage')
            scope=task['payload']['_scope']
            if not self.get('partitions_enabled'):
                self.db.execute("UPDATE tasks SET task_key=? WHERE task_key='stats:end'",('stats:historical-end:'+str(time.time_ns()),))
                self.put('stats_end',{})
            self.put('partition_baseline',data)
            self.put('partitions_enabled',True)
            if scope=='all' or self.get('partition_scope')!='all':
                self.put('partition_scope',scope)
            selected=coverage if scope=='all' else {min(coverage,key=lambda s:coverage[s]):0}
            for source in selected:
                split=source=='openalex' or coverage[source]>PAGE_LIMIT*PAGE_SIZE*2
                self.db.execute('INSERT OR IGNORE INTO partitions(id,source,filters,state,updated_at) VALUES(?,?,?,?,?)',
                    (source,source,compact({'has_sources':[source]}),'split' if split else 'active',now()))
                if split:
                    for oa in (True,False):
                        pid=source+(':oa' if oa else ':non_oa')
                        self.db.execute('INSERT OR IGNORE INTO partitions(id,source,filters,parent_id,updated_at) VALUES(?,?,?,?,?)',
                            (pid,source,compact({'has_sources':[source],'is_oa':oa}),source,now()))
                        self.partition_enqueue(pid,'asc',1)
                else:
                    self.partition_enqueue(source,'asc',1)
        else:
            self.put('partition_discovery_stats',data)
            self.put('partition_discovery_stats_task',task['key'])
        self.put('stats_latest',data)

    def resume_partition_boundaries(self):
        if not self.get('partitions_enabled'):
            return
        with self.db:
            for pid,direction,page in self.db.execute('''SELECT b.partition_id,b.direction,MAX(b.page)
                FROM partition_batches b JOIN partitions p ON p.id=b.partition_id
                WHERE p.state='active' GROUP BY b.partition_id,b.direction''').fetchall():
                self.partition_enqueue(pid,direction,page,'partition_boundary',':'+str(time.time_ns()))

    def finish_partition(self, pid, reason):
        self.db.execute("UPDATE partitions SET state='verifying',reason=?,updated_at=? WHERE id=?",(reason,now(),pid))
        self.db.execute("""UPDATE tasks SET state='superseded',error='Covered by opposite direction',updated_at=?
            WHERE partition_id=? AND kind='partition' AND state IN ('pending','retry')""",(now(),pid))
        for direction,page in self.db.execute('SELECT direction,page FROM partition_batches WHERE partition_id=?',(pid,)).fetchall():
            self.partition_enqueue(pid,direction,page,'partition_verify')

    def prioritize_unseen(self):
        if self.get('scheduler_mode')!='discovery_first' or self.get('partitions_paused'):
            return
        with self.db:
            for pid, in self.db.execute("""SELECT id FROM partitions p WHERE state='active' AND direction='asc'
                AND last_asc IS NOT NULL AND next_desc=1
                AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.partition_id=p.id AND t.kind='partition' AND t.state='running')""").fetchall():
                self.db.execute("UPDATE partitions SET direction='desc',updated_at=? WHERE id=?",(now(),pid))
                self.partition_enqueue(pid,'desc',1)

    def apply_partition(self, task, result):
        payload=task['payload']
        pid,direction,page=payload['_partition'],payload['sort_dir'],payload['page']
        if direction not in ('asc','desc'):
            raise ValueError('Invalid partition direction')
        row=self.db.execute('SELECT state,direction,next_asc,next_desc,last_asc,last_desc,filters FROM partitions WHERE id=?',(pid,)).fetchone()
        if not row:
            raise ValueError('Unknown partition')
        data=result['data']
        records=data.get('rows')
        if not isinstance(records,list):
            raise ValueError('Missing partition rows')
        ids=[r.get('unified',{}).get('id') for r in records]
        if any(type(i) is not int or i<=0 for i in ids) or ids!=sorted(set(ids),reverse=direction=='desc'):
            raise ValueError('Invalid partition ordering or duplicate IDs')
        meta=data.get('pageMeta',{})
        more=data.get('hasMore',meta.get('has_more'))
        if data.get('truncated') or meta.get('truncated') or type(more) is not bool or (more and len(ids)!=PAGE_SIZE):
            raise ValueError('Truncated or ambiguous partition pagination')
        if len(ids)>PAGE_SIZE or data.get('page',page)!=page:
            raise ValueError('Partition page number or size mismatch')
        applied=meta.get('applied_sort')
        if applied and applied!={'by':'id','dir':direction}:
            raise ValueError('Source did not apply requested partition ordering')
        filters=json.loads(row[6])
        for record in records:
            unified=record['unified']
            if not set(filters['has_sources']).issubset(sources_of(unified)):
                raise ValueError('Source membership does not match partition filter')
            if 'is_oa' in filters:
                values=[unified.get('openalex_is_oa'),unified.get('doaj_is_in_doaj')]
                is_oa=any(v is True or v==1 for v in values)
                if is_oa!=filters['is_oa']:
                    raise ValueError('OA fields do not match partition filter')
        if task['kind'] in ('partition_verify','partition_boundary'):
            expected=self.db.execute('SELECT ids_sha256 FROM partition_batches WHERE partition_id=? AND direction=? AND page=?',
                                     (pid,direction,page)).fetchone()
            if not expected or expected[0]!=id_hash(ids):
                raise ValueError('Partition page IDs changed; reconciliation required')
            if task['kind']=='partition_verify':
                self.db.execute('UPDATE partition_batches SET verified=1 WHERE partition_id=? AND direction=? AND page=?',(pid,direction,page))
                remaining=self.db.execute('SELECT COUNT(*) FROM partition_batches WHERE partition_id=? AND verified=0',(pid,)).fetchone()[0]
                if not remaining and row[0]=='verifying':
                    self.db.execute("UPDATE partitions SET state='complete',updated_at=? WHERE id=?",(now(),pid))
            return
        expected_page=row[2] if direction=='asc' else row[3]
        previous=row[4] if direction=='asc' else row[5]
        if row[0]!='active' or row[1]!=direction or page!=expected_page:
            raise ValueError('Out-of-order partition commit')
        if ids and previous is not None and ((direction=='asc' and ids[0]<=previous) or (direction=='desc' and ids[0]>=previous)):
            raise ValueError('Partition page overlaps its previous page')
        for record in records:
            unified=record['unified']
            self.db.execute('INSERT OR IGNORE INTO journals(id,canonical_name,list_json) VALUES(?,?,?)',
                (unified['id'],unified.get('canonical_name'),compact(record)))
            self.db.execute('INSERT OR IGNORE INTO partition_memberships VALUES(?,?)',(pid,unified['id']))
            detail=self.db.execute('SELECT detail_status FROM journals WHERE id=?',(unified['id'],)).fetchone()[0]
            if detail!='ok':
                self.enqueue(f"detail:{unified['id']}",'detail',{'id':unified['id']})
        self.db.execute('INSERT INTO partition_batches VALUES(?,?,?,?,?,?,?,0,?,?)',
            (pid,direction,page,len(ids),ids[0] if ids else None,ids[-1] if ids else None,id_hash(ids),result.get('raw'),result.get('sha256')))
        last=ids[-1] if ids else previous
        # direction is validated from a durable task created by partition_payload.
        self.db.execute(f'UPDATE partitions SET next_{direction}=?,last_{direction}=?,updated_at=? WHERE id=?',(page+1,last,now(),pid))
        if not more:
            self.finish_partition(pid,'natural_end')
        elif ((direction=='desc' and row[4] is not None and last<=row[4])
              or (direction=='asc' and row[5] is not None and last>=row[5])):
            # Cross-check shared IDs in the opposite traversal, not just its range.
            opposite='asc' if direction=='desc' else 'desc'
            frontier=row[4] if direction=='desc' else row[5]
            shared=[i for i in ids if (i<=frontier if direction=='desc' else i>=frontier)]
            for i in shared:
                if not self.db.execute("SELECT 1 FROM partition_direction_members WHERE partition_id=? AND direction=? AND journal_id=?",(pid,opposite,i)).fetchone():
                    raise ValueError('Bidirectional overlap changed; source reconciliation required')
            self.finish_partition(pid,'bidirectional_overlap')
        elif page>=PAGE_LIMIT:
            opposite='desc' if direction=='asc' else 'asc'
            next_other=row[3] if direction=='asc' else row[2]
            if next_other<=PAGE_LIMIT:
                self.db.execute('UPDATE partitions SET direction=?,updated_at=? WHERE id=?',(opposite,now(),pid))
                self.partition_enqueue(pid,opposite,next_other)
            else:
                self.db.execute("UPDATE partitions SET state='blocked',reason='Uncovered gap after both paging limits',updated_at=? WHERE id=?",(now(),pid))
        else:
            self.partition_enqueue(pid,direction,page+1)
        self.db.executemany('INSERT OR IGNORE INTO partition_direction_members VALUES(?,?,?)',((pid,direction,i) for i in ids))

    def partition_failure(self, task, message):
        pid=task['payload'].get('_partition')
        if pid:
            self.db.execute("UPDATE partitions SET state='blocked',reason=?,updated_at=? WHERE id=?",(message,now(),pid))

    def partition_status(self):
        rows=[]
        for values in self.db.execute('SELECT id,source,filters,parent_id,state,direction,next_asc,next_desc,last_asc,last_desc,reason FROM partitions ORDER BY id'):
            item=dict(zip(('id','source','filters','parent_id','state','direction','next_asc','next_desc','last_asc','last_desc','reason'),values))
            item['filters']=json.loads(item['filters'])
            item['unique_members']=self.db.execute('SELECT COUNT(*) FROM partition_memberships WHERE partition_id=?',(item['id'],)).fetchone()[0]
            item['batches'],item['verified_batches']=self.db.execute('SELECT COUNT(*),COALESCE(SUM(verified),0) FROM partition_batches WHERE partition_id=?',(item['id'],)).fetchone()
            rows.append(item)
        return {'scope':self.get('partition_scope'),'groups':rows,'enabled':bool(self.get('partitions_enabled')),
                'paused':bool(self.get('partitions_paused',False))}

    def partitions_verified(self):
        return (self.get('partition_scope')=='all'
                and bool(self.db.execute('SELECT COUNT(*) FROM partitions').fetchone()[0])
                and not self.db.execute("SELECT COUNT(*) FROM partitions WHERE state NOT IN ('split','complete')").fetchone()[0])

    def coverage_audit(self):
        baseline=self.get('partition_baseline',{})
        source_counts={}
        for source,expected in baseline.get('sourceCoverage',{}).items():
            count=self.db.execute('''SELECT COUNT(DISTINCT m.journal_id) FROM partition_memberships m
              JOIN partitions p ON p.id=m.partition_id WHERE p.source=?''',(source,)).fetchone()[0]
            source_counts[source]={'expected':expected,'found':count,'difference':count-expected}
        all_count=self.db.execute('SELECT COUNT(*) FROM journals').fetchone()[0]
        covered=self.db.execute('SELECT COUNT(DISTINCT journal_id) FROM partition_memberships').fetchone()[0]
        anomalies=self.db.execute("SELECT kind,state,COUNT(*) FROM tasks WHERE state IN ('failed','blocked','auth_wait') GROUP BY kind,state").fetchall()
        return {'at':now(),'scope':self.get('partition_scope'),'expected_total':baseline.get('totalJournals'),
                'unique_journals':all_count,'partition_union':covered,'legacy_only_journals':all_count-covered,
                'missing_journals':max(0,baseline.get('totalJournals',all_count)-all_count),
                'source_counts':source_counts,'partitions_verified':bool(self.partitions_verified()),
                'source_counts_match':bool(source_counts) and all(x['difference']==0 for x in source_counts.values()),
                'anomalies':[{'kind':k,'state':s,'count':n} for k,s,n in anomalies],
                'note':'Legacy list-cap errors remain as history. Unclassified/null coverage is unresolved until totals and source unions reconcile.'}
