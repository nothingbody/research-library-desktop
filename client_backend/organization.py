from __future__ import annotations

"""Local project references, safe smart filters, and cross-document annotations."""

import json

from .common import bounded_int, dumps, norm, now, require, uid


class Organization:
    TABLES = {'item': 'items', 'search': 'ai_search_sessions', 'relations': 'relation_sessions',
              'note': 'notes', 'conversation': 'research_conversations'}
    RULE_KEYS = {'q', 'field', 'tag', 'readingState', 'year', 'hasAttachment', 'sort', 'direction', 'view', 'advanced', 'collectionIds'}

    def __init__(self, library):
        self.library = library

    def project_create(self, payload):
        title = str(payload.get('title') or '').strip()[:160]
        require(title, '请输入项目名称')
        project_id, timestamp = uid(), now()
        with self.library.db(True) as db:
            db.execute('INSERT INTO research_projects VALUES(?,?,?,?,?,?)',
                       (project_id, title, str(payload.get('description') or '')[:2000], 0, timestamp, timestamp))
        return self.project_get(project_id)

    def project_list(self, archived=False):
        with self.library.db() as db:
            rows = db.execute('''SELECT p.*,COUNT(l.entity_id) AS link_count FROM research_projects p
                LEFT JOIN research_project_links l ON l.project_id=p.id WHERE p.archived=?
                GROUP BY p.id ORDER BY p.updated_at DESC''', (int(bool(archived)),)).fetchall()
        return [{'id': row['id'], 'title': row['title'], 'description': row['description'],
                 'archived': bool(row['archived']), 'linkCount': row['link_count'], 'updatedAt': row['updated_at']}
                for row in rows]

    def project_get(self, project_id):
        with self.library.db() as db:
            row = db.execute('SELECT * FROM research_projects WHERE id=?', (project_id,)).fetchone()
            require(row, '研究项目不存在')
            links = [dict(value) for value in db.execute('''SELECT * FROM research_project_links
                WHERE project_id=? ORDER BY ordinal,entity_type,entity_id''', (project_id,))]
            for link in links:
                table = self.TABLES[link['entity_type']]
                reference = db.execute(f'SELECT title FROM {table} WHERE id=?', (link['entity_id'],)).fetchone()
                link['title'] = reference[0] if reference else '原记录已移除'
        return {'id': row['id'], 'title': row['title'], 'description': row['description'],
                'archived': bool(row['archived']), 'links': [{'type': link['entity_type'], 'id': link['entity_id'],
                    'title': link['title'], 'note': link['note'], 'ordinal': link['ordinal']} for link in links],
                'updatedAt': row['updated_at']}

    def project_archive(self, payload):
        project_id = payload.get('projectId')
        with self.library.db(True) as db:
            require(db.execute('SELECT 1 FROM research_projects WHERE id=?', (project_id,)).fetchone(), '研究项目不存在')
            db.execute('UPDATE research_projects SET archived=?,updated_at=? WHERE id=?',
                       (int(bool(payload.get('archived', True))), now(), project_id))
        return self.project_get(project_id)

    def project_link(self, payload):
        project_id, kind, entity_id = payload.get('projectId'), payload.get('type'), payload.get('id')
        require(kind in self.TABLES and isinstance(entity_id, str), '项目关联类型或标识不正确')
        with self.library.db(True) as db:
            require(db.execute('SELECT 1 FROM research_projects WHERE id=? AND archived=0', (project_id,)).fetchone(), '项目不存在或已归档')
            table = self.TABLES[kind]
            require(db.execute(f'SELECT 1 FROM {table} WHERE id=?', (entity_id,)).fetchone(), '目标记录不存在')
            db.execute('''INSERT INTO research_project_links(project_id,entity_type,entity_id,ordinal,note)
                VALUES(?,?,?,?,?) ON CONFLICT(project_id,entity_type,entity_id) DO UPDATE SET note=excluded.note''',
                (project_id, kind, entity_id, bounded_int(payload.get('ordinal'), 0, 0, 100000),
                 str(payload.get('note') or '')[:1000]))
            db.execute('UPDATE research_projects SET updated_at=? WHERE id=?', (now(), project_id))
        return self.project_get(project_id)

    def annotation_search(self, payload):
        q = norm(payload.get('q'))[:200]
        item_ids = payload.get('itemIds') or []
        require(isinstance(item_ids, list) and len(item_ids) <= 100, '文献范围过大')
        require(all(isinstance(item_id, str) and len(item_id) <= 100 for item_id in item_ids), '文献标识不正确')
        scope = f" AND at.item_id IN ({','.join('?' for _ in item_ids)})" if item_ids else ''
        with self.library.db() as db:
            rows = db.execute(f'''SELECT a.*,at.item_id,at.version AS current_version,i.title
                FROM annotations a JOIN attachments at ON at.id=a.attachment_id
                JOIN items i ON i.id=at.item_id WHERE i.deleted_at IS NULL{scope} ORDER BY a.updated_at DESC''', item_ids).fetchall()
        results = []
        for row in rows:
            data = json.loads(row['data'])
            if q and q not in norm((data.get('quote') or '') + ' ' + (data.get('comment') or '')):
                continue
            results.append({'id': row['id'], 'itemId': row['item_id'], 'title': row['title'],
                            'attachmentId': row['attachment_id'], 'page': int(data.get('pageIndex') or 0) + 1,
                            'quote': data.get('quote') or '', 'comment': data.get('comment') or '',
                            'stale': row['version'] != row['current_version'], 'updatedAt': row['updated_at']})
        limit = bounded_int(payload.get('limit'), 50, 1, 200)
        offset = bounded_int(payload.get('offset'), 0, 0, 1000000)
        return {'items': results[offset:offset + limit], 'total': len(results)}

    def annotation_export(self, payload):
        output_format = payload.get('format', 'markdown')
        require(output_format in ('markdown', 'json'), '批注导出格式不支持')
        options = {'q': payload.get('q', ''), 'itemIds': payload.get('itemIds') or [], 'limit': 200}
        rows, offset = [], 0
        while True:
            page = self.annotation_search({**options, 'offset': offset})
            require(page['total'] <= 30000, '批注数量超过单次导出上限，请缩小范围')
            rows.extend(page['items'])
            offset += len(page['items'])
            if offset >= page['total'] or not page['items']:
                break
        if output_format == 'json':
            return dumps({'exportedAt': now(), 'count': len(rows), 'annotations': rows})
        lines = ['# 文献批注汇总', '', f'导出时间：{now()} · 共 {len(rows)} 条', '']
        current_item = None
        for row in rows:
            if row['itemId'] != current_item:
                current_item = row['itemId']
                lines.extend([f"## {row['title']}", ''])
            link = f"research://attachment/{row['attachmentId']}?page={row['page']}&annotation={row['id']}"
            lines.extend([f"### 第 {row['page']} 页{' · 附件版本已变化' if row['stale'] else ''}",
                          f"[返回原文]({link}) · 更新于 {row['updatedAt']}", ''])
            if row['quote']:
                lines.extend(['> ' + line for line in row['quote'].splitlines()])
                lines.append('')
            if row['comment']:
                lines.extend([row['comment'], ''])
        return '\n'.join(lines)

    def smart_save(self, payload):
        name = str(payload.get('name') or '').strip()[:100]
        rule = payload.get('rule') or {}
        require(name and isinstance(rule, dict) and set(rule) <= self.RULE_KEYS, '智能集合名称或规则不正确')
        require(rule.get('view', 'all') in ('all', 'unread', 'starred'), '智能集合视图不正确')
        require(rule.get('hasAttachment', 'all') in ('all', 'yes', 'no'), '附件筛选不正确')
        require(rule.get('field', 'all') in ('all', 'title', 'authors', 'doi', 'container'), '检索字段不正确')
        require(all(len(dumps(value)) <= 12000 for value in rule.values()), '筛选规则过长')
        if rule.get('advanced'):
            from .advanced_search import compile_rule
            compile_rule(rule['advanced'])
        if rule.get('collectionIds'):
            require(isinstance(rule['collectionIds'], list) and len(rule['collectionIds']) <= 50 and
                    all(isinstance(value, str) and len(value) <= 100 for value in rule['collectionIds']), '集合范围不正确')
        collection_id, timestamp = payload.get('id') or uid(), now()
        with self.library.db(True) as db:
            db.execute('''INSERT INTO smart_collections(id,name,rule_json,created_at,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,rule_json=excluded.rule_json,updated_at=excluded.updated_at''',
                (collection_id, name, dumps(rule), timestamp, timestamp))
        return {'id': collection_id, 'name': name, 'rule': rule}

    def smart_list(self):
        with self.library.db() as db:
            rows = db.execute('SELECT * FROM smart_collections ORDER BY updated_at DESC').fetchall()
        return [{'id': row['id'], 'name': row['name'], 'rule': json.loads(row['rule_json'])} for row in rows]

    def smart_results(self, payload):
        with self.library.db() as db:
            row = db.execute('SELECT * FROM smart_collections WHERE id=?', (payload.get('id'),)).fetchone()
        require(row, '智能集合不存在')
        rule = json.loads(row['rule_json'])
        return self.library.query({**rule, 'limit': payload.get('limit', 100), 'offset': payload.get('offset', 0)})
