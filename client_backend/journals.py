from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .common import AppError, bounded_int, dumps, norm, now, require, uid


def collector_alerts(status):
    """Surface actionable collector states so a stalled job is visible in the client."""
    alerts = []
    if not isinstance(status, dict):
        return alerts
    state = str(status.get('status') or '')
    if state == 'blocked':
        alerts.append({'level': 'warning', 'code': 'collector_blocked',
                       'message': '采集器处于阻塞状态：登录会话可能已失效。请在“数据中心 → 连接 / 更新登录”重新交接会话后恢复采集。'})
    heartbeat = status.get('heartbeat_at')
    if heartbeat and state not in ('stopped', 'complete', 'completed'):
        try:
            at = datetime.fromisoformat(str(heartbeat).replace('Z', '+00:00'))
            age = (datetime.now(timezone.utc) - at).total_seconds()
        except ValueError:
            age = None
        if age is not None and age > 120:
            alerts.append({'level': 'warning', 'code': 'collector_heartbeat_stale',
                           'message': f'采集器心跳已 {int(age // 60)} 分钟未更新（正常每 10 秒更新一次）。进程可能已退出或卡死；本地期刊数据仍可查询，恢复采集需重启采集器并重新交接会话。'})
    if status.get('snapshot'):
        alerts.append({'level': 'info', 'code': 'collector_snapshot',
                       'message': '采集器控制接口不可达，当前显示的是最近一次本地状态快照，不代表实时进度。'})
    return alerts


def number(value):
    try:
        return float(value) if value is not None and str(value).strip() else None
    except (ValueError, TypeError):
        return None


def main_record(value):
    return value.get('main', {}) if isinstance(value, dict) else {}


class Journals:
    def __init__(self, library):
        self.library = library
        self.path = library.root / 'journal-index.sqlite3'
        self.index_lock = threading.Lock()
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries(generation TEXT NOT NULL,id INTEGER NOT NULL,name TEXT NOT NULL,name_norm TEXT NOT NULL,
              publisher TEXT,country TEXT,category TEXT,oa INTEGER,warning TEXT,impact REAL,jcr TEXT,fqb INTEGER,xr INTEGER,ccf TEXT,
              works REAL,issns TEXT,data TEXT NOT NULL,PRIMARY KEY(generation,id));
            CREATE INDEX IF NOT EXISTS je_impact ON entries(generation,impact DESC,id);
            CREATE INDEX IF NOT EXISTS je_works ON entries(generation,works DESC,id);
            CREATE INDEX IF NOT EXISTS je_category ON entries(generation,category);
            CREATE INDEX IF NOT EXISTS je_country ON entries(generation,country);
            CREATE INDEX IF NOT EXISTS je_filter ON entries(generation,jcr,fqb,ccf);
            CREATE TABLE IF NOT EXISTS entry_issns(generation TEXT NOT NULL,issn TEXT NOT NULL,id INTEGER NOT NULL,PRIMARY KEY(generation,issn,id));
            CREATE TABLE IF NOT EXISTS related(id INTEGER PRIMARY KEY,data TEXT NOT NULL,updated_at TEXT NOT NULL);
            ''')
            db.commit()

    def root(self):
        value = self.library.get_settings().get('journalRoot')
        if not value:
            raise AppError('DATA_PATH_UNAVAILABLE', '请在设置中选择现有期刊数据目录')
        root = Path(value).resolve()
        require((root / 'journals.sqlite3').is_file(), '期刊数据库不存在，请检查数据目录')
        return root

    def source(self):
        db = sqlite3.connect((self.root() / 'journals.sqlite3').as_uri() + '?mode=ro', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        return db

    def _meta(self, db):
        return {row[0]: json.loads(row[1]) for row in db.execute('SELECT key,value FROM metadata')}

    def index(self, payload, progress):
        require(self.index_lock.acquire(blocking=False), '已有期刊索引任务正在运行')
        generation = now() + '-' + uid()[:8]
        try:
            with closing(self.source()) as source:
                ids = [row[0] for row in source.execute('SELECT id FROM journals ORDER BY id')]
            count = 0
            for start in range(0, len(ids), 1000):
                progress(start / max(1, len(ids)), f'建立期刊索引 {start:,}/{len(ids):,}')
                chunk = ids[start:start + 1000]
                with closing(self.source()) as source:
                    rows = source.execute('SELECT id,list_json,detail_status,fetched_at FROM journals WHERE id IN (' + ','.join('?' for _ in chunk) + ')', chunk).fetchall()
                values = []
                for row in rows:
                    listing = json.loads(row['list_json'])
                    item = listing.get('unified', listing)
                    name = item.get('canonical_name') or str(row['id'])
                    issns = list(dict.fromkeys(str(item.get(k)) for k in ('print_issn', 'electronic_issn', 'issn_l') if item.get(k)))
                    oa = item.get('is_oa', item.get('openalex_is_oa'))
                    warning = item.get('gjqk_warning_level')
                    if not warning and isinstance(item.get('warning'), dict):
                        warning = (item['warning'].get('gjqk') or {}).get('level')
                    card = {'id': row['id'], 'name': name, 'publisher': item.get('crossref_publisher') or item.get('publisher') or '',
                            'country': item.get('country') or item.get('openalex_country') or '',
                            'category': item.get('fqb_major_category') or item.get('xr_major_category_cn') or '',
                            'impact': number(item.get('jcr_impact_factor')), 'jcr': item.get('jcr_quartile'),
                            'fqb': item.get('fqb_major_quartile'), 'xr': item.get('xr_major_quartile'), 'top': item.get('fqb_top'),
                            'ccf': item.get('ccf_rank'), 'works': number(item.get('openalex_works_count')), 'oa': oa,
                            'warning': warning, 'sourceCount': item.get('source_count'), 'type': item.get('type'), 'issns': issns,
                            'hasSubmission': bool(item.get('has_submission') or (listing.get('submission') or {}).get('has_guideline')),
                            'detailStatus': row['detail_status'], 'fetchedAt': row['fetched_at']}
                    values.append((generation, card['id'], name, norm(name), card['publisher'], card['country'], card['category'],
                                   None if oa is None else int(bool(oa)), warning, card['impact'], card['jcr'], card['fqb'], card['xr'],
                                   card['ccf'], card['works'], ' '.join(x.replace('-', '').upper() for x in issns), dumps(card)))
                with closing(sqlite3.connect(self.path, timeout=20)) as db:
                    db.executemany('INSERT OR REPLACE INTO entries VALUES(' + ','.join('?' for _ in range(17)) + ')', values)
                    db.executemany('INSERT OR IGNORE INTO entry_issns VALUES(?,?,?)', ((generation, identifier, value[1]) for value in values for identifier in value[15].split()))
                    db.commit()
                count += len(values)
            with closing(sqlite3.connect(self.path, timeout=20)) as db:
                old = self._meta(db).get('generation', '')
                metadata = {'generation': generation, 'previousGeneration': old, 'count': count, 'indexedAt': now(), 'sourceRoot': str(self.root())}
                for key, value in metadata.items():
                    db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (key, dumps(value)))
                db.execute('DELETE FROM entries WHERE generation NOT IN (?,?)', (generation, old))
                db.execute('DELETE FROM entry_issns WHERE generation NOT IN (?,?)', (generation, old))
                db.commit()
            return metadata
        except Exception:
            with closing(sqlite3.connect(self.path, timeout=20)) as db:
                db.execute('DELETE FROM entries WHERE generation=?', (generation,))
                db.execute('DELETE FROM entry_issns WHERE generation=?', (generation,))
                db.commit()
            raise
        finally:
            self.index_lock.release()

    def stats(self):
        result = {'local': None, 'remote': None}
        with closing(sqlite3.connect(self.path)) as db:
            result['index'] = self._meta(db)
        try:
            with closing(self.source()) as db:
                result['local'] = {'journals': db.execute('SELECT count(*) FROM journals').fetchone()[0],
                                   'details': db.execute("SELECT count(*) FROM journals WHERE detail_status IN ('ok','complete')").fetchone()[0]}
            stats_file = self.root() / 'stats_latest.json'
            if stats_file.is_file():
                result['remote'] = json.loads(stats_file.read_text(encoding='utf-8'))
                result['remoteObservedAt'] = stats_file.stat().st_mtime
        except (AppError, OSError, sqlite3.Error) as exc:
            result['message'] = str(exc)
        return result

    def search(self, params):
        with closing(sqlite3.connect(self.path)) as db:
            metadata = self._meta(db)
            generation = params.get('indexVersion') or metadata.get('generation')
            if not generation:
                raise AppError('INDEX_NOT_READY', '期刊索引尚未建立，请在数据中心开始索引')
            require(str(self.root()) == metadata.get('sourceRoot'), '数据目录已切换，请重新建立期刊索引')
            if generation not in (metadata.get('generation'), metadata.get('previousGeneration')):
                raise AppError('INDEX_VERSION_CHANGED', '索引版本已更新，请刷新列表')
            filters = params.get('filters') or {}
            require(set(filters) <= {'fqb', 'jcr', 'minImpact', 'ccf', 'publisher', 'country', 'category', 'oa', 'warning'}, '不支持的期刊筛选条件')
            where, args = ['generation=?'], [generation]
            q = norm(params.get('q', ''))[:500]
            if re.fullmatch(r'\d{4}-?\d{3}[\dXx]', q):
                where.append('id IN (SELECT id FROM entry_issns WHERE generation=? AND issn=?)')
                args.extend([generation, q.replace('-', '').upper()])
            elif q:
                for term in q.split():
                    where.append('(instr(name_norm,?)>0 OR instr(lower(publisher),?)>0)')
                    args.extend([term, term])
            for key in ('fqb', 'jcr', 'ccf', 'country', 'category'):
                if filters.get(key) not in (None, ''):
                    where.append(key + '=?')
                    args.append(filters[key])
            if filters.get('minImpact') not in (None, ''):
                threshold = number(filters['minImpact'])
                require(threshold is not None and 0 <= threshold <= 10000, '影响因子阈值不正确')
                where.append('impact>=?')
                args.append(threshold)
            publisher = filters.get('publisher')
            if publisher:
                require(publisher in ('Elsevier', 'Springer Nature', 'IEEE', 'ACM', 'Nature Portfolio'), '出版商选项不支持')
                if publisher == 'Springer Nature':
                    where.append("(lower(publisher) LIKE '%springer%' OR lower(publisher) LIKE '%nature%')")
                elif publisher == 'Nature Portfolio':
                    where.append("(lower(publisher) LIKE '%nature%' OR name_norm='nature' OR name_norm LIKE 'nature %')")
                else:
                    where.append('lower(publisher) LIKE ?')
                    args.append('%' + publisher.lower() + '%')
            if filters.get('oa') is not None:
                where.append('oa=?')
                args.append(int(bool(filters['oa'])))
            if filters.get('warning'):
                where.append("warning IS NOT NULL AND warning<>''")
            sort = params.get('sort') or ('relevance' if q else 'impact')
            require(sort in ('relevance', 'impact', 'works', 'recent'), '期刊排序不支持')
            sort_args = []
            if sort == 'relevance' and q:
                order = 'CASE WHEN name_norm=? THEN 0 WHEN name_norm LIKE ? THEN 1 WHEN instr(name_norm,?)>0 THEN 2 ELSE 3 END,impact IS NULL,impact DESC,id'
                sort_args = [q, q.replace('%', '') + '%', q]
            elif sort == 'recent':
                order = 'id DESC'
            else:
                key = 'works' if sort == 'works' else 'impact'
                # SQLite sorts NULL after numeric values in descending order.
                order = f'{key} DESC,id'
            limit = bounded_int(params.get('pageSize'), 8, 1, 100)
            page = bounded_int(params.get('page'), 1, 1, 1000000)
            clause = ' AND '.join(where)
            total = db.execute('SELECT count(*) FROM entries WHERE ' + clause, args).fetchone()[0]
            rows = db.execute('SELECT data FROM entries WHERE ' + clause + ' ORDER BY ' + order + ' LIMIT ? OFFSET ?', [*args, *sort_args, limit, (page - 1) * limit]).fetchall()
            return {'items': [json.loads(row[0]) for row in rows], 'total': total, 'page': page, 'totalPages': max(1, (total + limit - 1) // limit),
                    'hasMore': page * limit < total, 'indexVersion': generation, 'newVersionAvailable': generation != metadata.get('generation')}

    def catalog(self):
        with closing(sqlite3.connect(self.path)) as db:
            metadata = self._meta(db)
            generation = metadata.get('generation')
            if not generation:
                raise AppError('INDEX_NOT_READY', '请先建立期刊索引')
            result = {}
            for field in ('country', 'category'):
                result[field] = [{'name': row[0], 'count': row[1]} for row in db.execute(f"SELECT {field},count(*) FROM entries WHERE generation=? AND {field}<>'' GROUP BY {field} ORDER BY count(*) DESC", (generation,))]
            result['oa'] = db.execute('SELECT count(*) FROM entries WHERE generation=? AND oa=1', (generation,)).fetchone()[0]
            result['warning'] = db.execute("SELECT count(*) FROM entries WHERE generation=? AND warning IS NOT NULL AND warning<>''", (generation,)).fetchone()[0]
            result['unclassified'] = db.execute("SELECT count(*) FROM entries WHERE generation=? AND category=''", (generation,)).fetchone()[0]
            result['indexVersion'] = generation
            return result

    def match_issn(self, value):
        """Return a local journal only for an unambiguous exact ISSN match."""
        identifier = str(value or '').strip().upper()
        require(re.fullmatch(r'\d{4}-?\d{3}[\dX]', identifier), 'ISSN 格式不正确')
        return self.match_issns([identifier])[identifier]

    def match_issns(self, values):
        """Resolve a page of ISSNs with one index lookup; never infer a venue from its name."""
        identifiers = {str(value).strip().upper().replace('-', '') for value in values
                       if re.fullmatch(r'\d{4}-?\d{3}[\dXx]', str(value or '').strip())}
        if not identifiers:
            return {}
        require(len(identifiers) <= 200, '单次期刊匹配不能超过 200 个 ISSN')
        with closing(sqlite3.connect(self.path)) as db:
            metadata = self._meta(db)
            generation = metadata.get('generation')
            if not generation:
                raise AppError('INDEX_NOT_READY', '期刊索引尚未建立，请在数据中心开始索引')
            require(str(self.root()) == metadata.get('sourceRoot'), '数据目录已切换，请重新建立期刊索引')
            rows = db.execute('SELECT i.issn,i.id,e.data FROM entry_issns i JOIN entries e '
                              'ON e.generation=i.generation AND e.id=i.id '
                              'WHERE i.generation=? AND i.issn IN (' + ','.join('?' for _ in identifiers) + ')',
                              [generation, *identifiers]).fetchall()
        matches = {identifier: [] for identifier in identifiers}
        for identifier, journal_id, data in rows:
            matches[identifier].append((journal_id, json.loads(data)))
        unique_ids = {items[0][0] for items in matches.values() if len(items) == 1}
        years = {}
        if unique_ids:
            with closing(self.source()) as source:
                journal_rows = source.execute('SELECT id,list_json,detail_json FROM journals WHERE id IN (' +
                                              ','.join('?' for _ in unique_ids) + ')', list(unique_ids)).fetchall()
            for row in journal_rows:
                listing = json.loads(row['list_json'])
                detail = json.loads(row['detail_json']) if row['detail_json'] else listing
                years[row['id']] = (detail.get('unified', detail).get('jcr_year') or
                                    listing.get('unified', listing).get('jcr_year'))
        result = {}
        for identifier, items in matches.items():
            if len(items) != 1:
                result[identifier] = {'matched': False, 'reason': '未找到唯一 ISSN 匹配期刊'}
                continue
            journal_id, card = items[0]
            year = years.get(journal_id)
            result[identifier] = {'matched': True, 'matchMethod': 'exact_issn',
                                  'issn': identifier[:4] + '-' + identifier[4:],
                                  'journalId': journal_id, 'name': card['name'],
                                  'jcrQuartile': card.get('jcr') if year else None,
                                  'impactFactor': card.get('impact') if year else None,
                                  'jcrYear': year}
        return result

    def get(self, journal_id):
        journal_id = bounded_int(journal_id, 0, 1, 1000000000)
        with closing(self.source()) as db:
            row = db.execute('SELECT * FROM journals WHERE id=?', (journal_id,)).fetchone()
            if not row:
                raise AppError('NOT_FOUND', '该期刊尚未保存到本地')
            listing = json.loads(row['list_json'])
            detail = json.loads(row['detail_json']) if row['detail_json'] else listing
        sources, unified = detail.get('sources', {}), detail.get('unified', {})
        metrics = {}
        for label, source, field, collection in [('impact', 'jcr', 'impact_factor', 'all_years'), ('sjr', 'scimago', 'sjr', 'all_years'),
                                               ('snip', 'cwts', 'snip', 'all_years'), ('works', 'openalex', 'works_count', 'counts_by_year'),
                                               ('cited', 'openalex', 'cited_by_count', 'counts_by_year')]:
            records = (sources.get(source) or {}).get(collection, [])
            metrics[label] = sorted([{'year': r.get('year'), 'value': number(r.get(field)), 'source': source} for r in records
                                     if isinstance(r, dict) and r.get('year') and number(r.get(field)) is not None], key=lambda x: x['year'])
        openalex = main_record(sources.get('openalex', {}))
        return {'id': journal_id, 'name': unified.get('canonical_name', row['canonical_name']), 'unified': unified, 'sources': sources,
                'issns': detail.get('issn_details', []), 'metrics': metrics, 'comments': detail.get('comments', {}),
                'submission': detail.get('submission', {}), 'homepage': openalex.get('homepage_url'), 'detailStatus': row['detail_status'],
                'fetchedAt': row['fetched_at'], 'sourceUrl': f'https://www.scholay.com/journal/{journal_id}', 'raw': detail}

    def link(self, item):
        issns = re.findall(r'\d{4}-?\d{3}[\dXx]', str(item.get('ISSN') or ''))
        if issns:
            exact = []
            for identifier in issns:
                exact.extend(self.search({'q': identifier, 'pageSize': 100})['items'])
            return {'basis': 'ISSN', 'exact': True, 'items': list({r['id']: r for r in exact}.values())}
        if item.get('container-title'):
            return {'basis': '刊名候选，需人工核对', 'exact': False, 'items': self.search({'q': item['container-title'], 'pageSize': 20})['items']}
        return {'basis': '无期刊标识', 'exact': False, 'items': []}

    def collector(self, action='status', **params):
        root = self.root()
        handoff_path = root / 'handoff.json'
        require(handoff_path.is_file(), '未发现采集器控制信息，请启动采集器或通过连接窗口登录')
        handoff = json.loads(handoff_path.read_text(encoding='utf-8'))
        url = handoff['status_url' if action == 'status' else 'control_url']
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port, '采集器控制地址不合法')
        if action != 'status':
            require(action in ('pause', 'resume', 'retry_failed', 'export', 'audit_coverage', 'pause_partitions', 'resume_partitions', 'client_request'), '不支持的采集操作')
            raw = dumps({'action': action, **params}).encode('utf-8')
        else:
            raw = None
        req = urllib.request.Request(url, data=raw, headers={'Origin': 'https://www.scholay.com', 'Content-Type': 'application/json'}, method='GET' if raw is None else 'POST')
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read(2 * 1024 * 1024))
            if action == 'status' and isinstance(result, dict):
                result['alerts'] = collector_alerts(result)
            return result
        except urllib.error.HTTPError as exc:
            if exc.code == 400 and action == 'client_request':
                raise AppError('COLLECTOR_UPGRADE_REQUIRED', '当前采集器尚未载入桌面任务接口，请在连接管理中重启新版采集器并登录')
            raise AppError('COLLECTOR_UNAVAILABLE', f'采集器返回 HTTP {exc.code}')
        except (urllib.error.URLError, TimeoutError) as exc:
            if action == 'status':
                snapshot = root / 'job_status.json'
                if snapshot.is_file():
                    result = {**json.loads(snapshot.read_text(encoding='utf-8')), 'connection': 'disconnected', 'snapshot': True}
                    result['alerts'] = collector_alerts(result)
                    return result
            raise AppError('COLLECTOR_UNAVAILABLE', '采集器未连接，本地期刊数据仍可查询') from exc

    def related(self, journal_id):
        journal_id = bounded_int(journal_id, 0, 1, 1000000000)
        try:
            with closing(self.source()) as db:
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='journal_related'").fetchone()
                if exists:
                    row = db.execute('SELECT data_json,fetched_at FROM journal_related WHERE journal_id=?', (journal_id,)).fetchone()
                    if row:
                        return {'status': 'cached', 'data': json.loads(row[0]), 'fetchedAt': row[1]}
        except (OSError, sqlite3.Error):
            pass
        return {'status': 'not_cached', 'data': None}

    def ensure(self, journal_id, kind='detail'):
        require(self.library.get_settings().get('online', True), '联网已关闭')
        require(kind in ('detail', 'related'), '任务类型不支持')
        return self.collector('client_request', journal_id=bounded_int(journal_id, 0, 1, 1000000000), kind=kind)
