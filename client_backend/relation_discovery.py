from __future__ import annotations

"""Source-labelled seed-paper discovery for a local relation session."""

import json
import re
from difflib import SequenceMatcher
from urllib import parse

from .ai_search import SOURCE_ENDPOINTS, _text, _year
from .common import bounded_int, doi, dumps, norm, now, require, uid


FIELDS = 'id,doi,title,publication_year,authorships,primary_location,cited_by_count,open_access,best_oa_location,type,abstract_inverted_index,referenced_works,related_works'
DIRECTIONS = ('references', 'citing', 'related')


class RelationDiscovery:
    def __init__(self, library, jobs, ai_search):
        self.library, self.jobs, self.ai_search = library, jobs, ai_search

    def submit(self, payload):
        session_id, anchor_id = payload.get('sessionId'), payload.get('itemId')
        direction = payload.get('direction')
        require(direction in DIRECTIONS, '发现方向不正确')
        require(self.library.get_settings().get('online', True), '联网已关闭')
        with self.library.db() as db:
            require(db.execute('SELECT 1 FROM relation_session_items WHERE session_id=? AND item_id=?',
                               (session_id, anchor_id)).fetchone(), '种子论文不在当前分析集中')
            page = db.execute('''SELECT next_offset,has_more FROM relation_discovery_pages
                WHERE session_id=? AND anchor_item_id=? AND direction=?''', (session_id, anchor_id, direction)).fetchone()
            require(not page or page['has_more'], '这一方向已没有更多来源记录')
            offset = page['next_offset'] if page else 0
        return self.jobs.create('relations.discover', {'sessionId': session_id, 'itemId': anchor_id,
                                                       'direction': direction, 'limit': bounded_int(payload.get('limit'), 20, 1, 40),
                                                       'offset': offset})

    def _fetch(self, params):
        return self.ai_search._json(SOURCE_ENDPOINTS['openalex'] + '?' + parse.urlencode(params), 'openalex')

    def _seed(self, item):
        work = None
        key = doi(item.get('DOI'))
        if key:
            rows = self._fetch({'filter': 'doi:' + key, 'per-page': 2, 'select': FIELDS}).get('results') or []
            work = next((row for row in rows if doi(row.get('doi')) == key), None)
            if work:
                similarity = SequenceMatcher(None, norm(work.get('title', '')), norm(item['title'])).ratio()
                require(similarity >= .72, 'DOI 对应的 OpenAlex 题名与本地文献不一致，请先核对题录')
        if not work:
            rows = self._fetch({'search': item['title'], 'per-page': 5, 'select': FIELDS}).get('results') or []
            matches = [row for row in rows if norm(row.get('title', '')) == norm(item['title'])]
            year = _year(item.get('year'))
            if year:
                matches = [row for row in matches if not row.get('publication_year') or abs(row['publication_year'] - year) <= 1]
            require(len(matches) == 1, '无法唯一识别种子论文；请补充 DOI 后重试')
            work = matches[0]
        require(re.fullmatch(r'W\d+', str(work.get('id', '')).rsplit('/', 1)[-1]), 'OpenAlex 未返回有效论文标识')
        return work

    def run(self, payload, progress):
        session_id, anchor_id, direction, limit = (payload[key] for key in ('sessionId', 'itemId', 'direction', 'limit'))
        offset = payload.get('offset', 0)
        require(self.library.get_settings().get('online', True), '联网已关闭')
        with self.library.db() as db:
            require(db.execute('SELECT 1 FROM relation_session_items WHERE session_id=? AND item_id=?',
                               (session_id, anchor_id)).fetchone(), '种子论文不在当前分析集中')
            item = self.library._get(db, anchor_id)
        progress(.08, '核对种子论文的 OpenAlex 记录')
        seed = self._seed(item)
        seed_id = seed['id'].rsplit('/', 1)[-1]
        if direction == 'citing':
            result = self._fetch({'filter': 'cites:' + seed_id, 'per-page': limit, 'page': offset // limit + 1,
                                  'sort': 'cited_by_count:desc', 'select': FIELDS})
            works = result.get('results') or []
            has_more = offset + len(works) < int((result.get('meta') or {}).get('count') or 0)
        else:
            field = 'referenced_works' if direction == 'references' else 'related_works'
            all_ids = [value.rsplit('/', 1)[-1] for value in (seed.get(field) or []) if re.fullmatch(r'W\d+', str(value).rsplit('/', 1)[-1])]
            ids = all_ids[offset:offset + limit]
            has_more = offset + len(ids) < len(all_ids)
            works = self._fetch({'filter': 'openalex_id:' + '|'.join(ids), 'per-page': len(ids), 'select': FIELDS}).get('results') or [] if ids else []
            by_id = {work['id'].rsplit('/', 1)[-1]: work for work in works}
            works = [by_id[value] for value in ids if value in by_id]
        progress(.65, f'已获取 {len(works)} 篇来源记录，正在核对引用方向与去重')
        records = []
        for work in works:
            work_id = str(work.get('id') or '').rsplit('/', 1)[-1]
            if not re.fullmatch(r'W\d+', work_id) or work_id == seed_id or not _text(work.get('title'), 1500):
                continue
            if direction == 'citing' and seed['id'] not in (work.get('referenced_works') or []):
                continue
            source = ((work.get('primary_location') or {}).get('source') or {})
            authors = [_text((author.get('author') or {}).get('display_name'), 300) for author in (work.get('authorships') or [])]
            records.append({'workId': work_id, 'title': _text(work['title'], 1500), 'doi': doi(work.get('doi')),
                            'authors': [name for name in authors if name][:100], 'year': _year(work.get('publication_year')),
                            'venue': _text(source.get('display_name'), 600),
                            'abstract': self.ai_search._abstract_from_index(work.get('abstract_inverted_index')),
                            'citationCount': int(work.get('cited_by_count') or 0),
                            'url': _text((work.get('primary_location') or {}).get('landing_page_url') or work.get('id'), 1200),
                            'oaUrl': _text((work.get('best_oa_location') or {}).get('pdf_url') or
                                           (work.get('open_access') or {}).get('oa_url'), 1200),
                            'type': _text(work.get('type'), 100), 'source': 'OpenAlex',
                            'sourceUrl': f'https://openalex.org/{work_id}', 'retrievedAt': now(),
                            'direction': direction, 'anchorItemId': anchor_id})
        with self.library.db(True) as db:
            for record in records:
                existing = self._match(db, record)
                db.execute('''INSERT INTO relation_discoveries(session_id,anchor_item_id,work_id,direction,work_json,status,imported_item_id,retrieved_at)
                    VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id,anchor_item_id,work_id,direction)
                    DO UPDATE SET work_json=excluded.work_json,retrieved_at=excluded.retrieved_at,
                    imported_item_id=COALESCE(relation_discoveries.imported_item_id,excluded.imported_item_id)''',
                    (session_id, anchor_id, record['workId'], direction, dumps(record), 'pending', existing, record['retrievedAt']))
            db.execute('''INSERT INTO relation_discovery_pages(session_id,anchor_item_id,direction,next_offset,has_more,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(session_id,anchor_item_id,direction)
                DO UPDATE SET next_offset=excluded.next_offset,has_more=excluded.has_more,updated_at=excluded.updated_at''',
                (session_id, anchor_id, direction, offset + len(works), int(has_more), now()))
        progress(1, '来源记录和引用方向已保存到本地')
        return {'received': len(records), 'direction': direction, 'seedId': seed_id, 'source': 'OpenAlex',
                'hasMore': has_more, 'nextOffset': offset + len(works)}

    @staticmethod
    def _match(db, record):
        if record['doi']:
            row = db.execute('SELECT id FROM items WHERE doi=? AND deleted_at IS NULL LIMIT 1', (record['doi'],)).fetchone()
            if row:
                return row['id']
        if record['year'] and record['authors']:
            expected_author = re.sub(r'[^\w]+', '', norm(record['authors'][0]))
            rows = db.execute('SELECT id,doi,authors FROM items WHERE title_norm=? AND year=? AND deleted_at IS NULL',
                              (norm(record['title']), str(record['year']))).fetchall()
            matches = [row['id'] for row in rows if
                       (not record['doi'] or not row['doi'] or record['doi'] == row['doi']) and
                       re.sub(r'[^\w]+', '', norm(row['authors'].split(';')[0])) == expected_author]
            if len(matches) == 1:
                return matches[0]
        return None

    def list(self, payload):
        session_id = payload.get('sessionId')
        limit = bounded_int(payload.get('limit'), 500, 1, 10000)
        with self.library.db() as db:
            require(db.execute('SELECT 1 FROM relation_sessions WHERE id=?', (session_id,)).fetchone(), '关联分析不存在')
            rows = db.execute('''SELECT * FROM relation_discoveries WHERE session_id=?
                ORDER BY retrieved_at DESC,work_id LIMIT ?''', (session_id, limit)).fetchall()
            result = []
            for row in rows:
                record = json.loads(row['work_json'])
                decision = db.execute('''SELECT reason,created_at FROM relation_discovery_decisions
                    WHERE session_id=? AND anchor_item_id=? AND work_id=? AND direction=? ORDER BY rowid DESC LIMIT 1''',
                    (session_id, row['anchor_item_id'], row['work_id'], row['direction'])).fetchone()
                record.update(id=f"{row['anchor_item_id']}:{row['direction']}:{row['work_id']}",
                              status=row['status'], importedItemId=row['imported_item_id'],
                              decisionReason=decision['reason'] if decision else '', decidedAt=decision['created_at'] if decision else None)
                result.append(record)
        return result

    def export_graph(self, payload, relations):
        session_id = payload.get('sessionId')
        session = relations.get(session_id)
        discovered = self.list({'sessionId': session_id, 'limit': 10000})
        manual = relations.manual_list(session_id)
        nodes = [{'id': row['itemId'], 'title': row['title'], 'year': row['year'], 'inLibrary': True}
                 for row in session['items']]
        known = {row['id'] for row in nodes}
        for row in discovered:
            node_id = row['importedItemId'] if row['importedItemId'] in known else 'openalex:' + row['workId']
            if node_id not in known:
                nodes.append({'id': node_id, 'title': row['title'], 'year': row['year'],
                              'doi': row['doi'], 'inLibrary': False, 'sourceUrl': row['sourceUrl']})
                known.add(node_id)
        edges = [{'id': row['id'], 'source': row['leftItemId'], 'target': row['rightItemId'],
                  'kind': 'evidence-inference', 'directed': False, 'label': row['typeLabel'],
                  'status': row['status'], 'confidence': row['confidence'], 'rationale': row['rationale']}
                 for row in session['relations']]
        for row in discovered:
            target = row['importedItemId'] if row['importedItemId'] in known else 'openalex:' + row['workId']
            if row['direction'] == 'citing':
                source, destination = target, row['anchorItemId']
            else:
                source, destination = row['anchorItemId'], target
            edges.append({'id': row['id'], 'source': source, 'target': destination,
                          'kind': 'algorithmic-recommendation' if row['direction'] == 'related' else 'citation-record',
                          'directed': row['direction'] != 'related', 'sourceUrl': row['sourceUrl'],
                          'retrievedAt': row['retrievedAt'], 'status': row['status'], 'decisionReason': row['decisionReason']})
        references = {}
        for row in discovered:
            if row['direction'] == 'references' and row['status'] != 'ignored':
                references.setdefault(row['workId'], []).append(row)
        shared = {}
        for matches in references.values():
            anchors = {row['anchorItemId']: row for row in matches}
            ids = sorted(anchors)
            for i, left in enumerate(ids):
                for right in ids[i + 1:]:
                    shared.setdefault((left, right), []).append(anchors[left]['workId'])
        edges.extend({'id': f'shared:{left}:{right}', 'source': left, 'target': right,
                      'kind': 'shared-references', 'directed': False, 'sharedCount': len(work_ids),
                      'workIds': work_ids, 'coverage': 'currently-discovered-only'}
                     for (left, right), work_ids in shared.items() if len(work_ids) >= 2)
        edges.extend({'id': row['id'], 'source': row['leftItemId'], 'target': row['rightItemId'],
                      'kind': 'manual', 'directed': False, 'label': row['label'], 'note': row['note']}
                     for row in manual)
        return dumps({'format': 'research-library-graph-v1', 'exportedAt': now(),
                      'session': {'id': session_id, 'title': session['title'], 'mode': session['mode']},
                      'nodes': nodes, 'edges': edges,
                      'coverage': {'discoveryRowsExported': len(discovered), 'limit': 10000,
                                   'note': 'OpenAlex 引文覆盖取决于已获取的来源记录；未发现不代表不存在。'}})

    def progress(self, payload):
        session_id, anchor_id, direction = payload.get('sessionId'), payload.get('itemId'), payload.get('direction')
        require(direction in DIRECTIONS, '发现方向不正确')
        with self.library.db() as db:
            require(db.execute('SELECT 1 FROM relation_session_items WHERE session_id=? AND item_id=?',
                               (session_id, anchor_id)).fetchone(), '种子论文不在当前分析集中')
            row = db.execute('''SELECT next_offset,has_more,updated_at FROM relation_discovery_pages
                WHERE session_id=? AND anchor_item_id=? AND direction=?''', (session_id, anchor_id, direction)).fetchone()
        return {'nextOffset': row['next_offset'] if row else 0, 'hasMore': bool(row['has_more']) if row else True,
                'updatedAt': row['updated_at'] if row else None}

    def decide(self, payload):
        status = payload.get('status')
        require(status in ('pending', 'later', 'saved', 'ignored'), '处理状态不正确')
        reason = _text(payload.get('reason'), 1000)
        key = (payload.get('sessionId'), payload.get('itemId'), payload.get('workId'), payload.get('direction'))
        with self.library.db(True) as db:
            row = db.execute('''SELECT work_json,imported_item_id FROM relation_discoveries
                WHERE session_id=? AND anchor_item_id=? AND work_id=? AND direction=?''', key).fetchone()
            require(row, '发现记录不存在')
            record = json.loads(row['work_json'])
            item_id = row['imported_item_id']
            if status == 'saved':
                item_id = item_id or self._match(db, record)
                if not item_id:
                    authors = [{'literal': name} for name in record['authors']]
                    data = {'title': record['title'], 'author': authors, 'issued': {'date-parts': [[record['year']]]} if record['year'] else {},
                            'container-title': record['venue'], 'abstract': record['abstract'], 'DOI': record['doi'],
                            'URL': record['url'], 'citationCount': record['citationCount'],
                            'type': record['type'] or 'article-journal', 'tags': ['引文发现']}
                    item_id = self.library._create(db, data, source='openalex-relation-discovery')
                count = db.execute('SELECT count(*) FROM relation_session_items WHERE session_id=?', (key[0],)).fetchone()[0]
                if count < 25:
                    db.execute('INSERT OR IGNORE INTO relation_session_items(session_id,item_id,ordinal) VALUES(?,?,?)',
                               (key[0], item_id, count))
                    db.execute("UPDATE relation_sessions SET state='stale',updated_at=? WHERE id=?", (now(), key[0]))
            db.execute('''UPDATE relation_discoveries SET status=?,imported_item_id=?
                WHERE session_id=? AND anchor_item_id=? AND work_id=? AND direction=?''', (status, item_id, *key))
            db.execute('''INSERT INTO relation_discovery_decisions(id,session_id,anchor_item_id,work_id,direction,status,reason,created_at)
                VALUES(?,?,?,?,?,?,?,?)''', (uid(), *key, status, reason, now()))
        return {'status': status, 'itemId': item_id, 'workId': key[2]}
