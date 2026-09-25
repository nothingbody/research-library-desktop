from __future__ import annotations

"""Local-first, evidence-preserving cross-paper reading.

The local pass only proposes topical or methodological *candidates*.  A remote
model is optional, requires an explicit session flag, and may promote a claim
only when it points to one saved evidence fragment for each paper.
"""

from collections import Counter
import hashlib
import json
import re

from .common import AppError, bounded_int, dumps, norm, now, require, uid


MODES = ('compare', 'findings', 'controversy', 'evolution', 'custom')
TYPES = ('topic_overlap', 'method_compare', 'supports', 'contradicts', 'extends', 'shared_gap')
TYPE_LABELS = {
    'topic_overlap': '研究主题相近', 'method_compare': '方法可比较', 'supports': '结论相互支持',
    'contradicts': '结论存在分歧', 'extends': '后续工作扩展', 'shared_gap': '共同研究空白',
}
STOPWORDS = {
    'about','after','also','among','and','are','been','between','both','can','could','data','does','each','for','from','have','into','its','more','most','not','only','our','over','paper','papers','research','results','should','such','than','that','the','their','these','this','through','using','was','were','with','within',
    '一个','一些','主要','其中','关于','包括','可以','进行','我们','文献','方法','研究','通过','以及','论文','作者','结果','发现','数据','使用','为了','模型','分析','基于','本文','该','其','和','与','对','在','是','的',
}


def _text(value, limit=12000):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:limit]


def _clip(value, limit=760):
    value = _text(value, limit + 1)
    return value if len(value) <= limit else value[:limit - 1].rstrip() + '…'


def _tokens(value):
    english = [word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z0-9+._-]{2,}", str(value or ''))]
    chinese = [piece for piece in re.findall(r'[\u3400-\u9fff]{2,}', str(value or ''))]
    return [token for token in english + chinese if token not in STOPWORDS and len(token) > 1]


def _sentence(value, hints=()):
    parts = re.split(r'(?<=[。！？.!?])\s+', _text(value, 2600))
    for part in parts:
        lowered = norm(part)
        if any(hint in lowered for hint in hints) and len(part) > 25:
            return _clip(part, 620)
    return _clip(next((part for part in parts if len(part) > 25), value), 620)


def _matching_excerpt(value, hints):
    """Select an exact, bounded substring instead of labelling unrelated text as evidence."""
    for part in re.split(r'(?<=[。！？.!?])\s+', _text(value)):
        lowered = norm(part)
        match = next((lowered.find(hint) for hint in hints if hint in lowered), -1)
        if match >= 0 and len(part) > 25:
            start = max(0, match - 180)
            return part[start:start + 900]
    return ''


class Relations:
    def __init__(self, library, jobs, assistant=None):
        self.library, self.jobs, self.assistant = library, jobs, assistant

    def _material(self, item_id):
        """Return local excerpts with durable item/attachment/page provenance."""
        with self.library.db() as db:
            item = self.library._get(db, item_id)
            card = db.execute('SELECT data FROM reading_cards WHERE item_id=?', (item_id,)).fetchone()
            notes = [dict(row) for row in db.execute('SELECT title,content FROM notes WHERE item_id=? ORDER BY updated_at DESC LIMIT 30', (item_id,))]
            terms = [dict(row) for row in db.execute('SELECT term,translation,explanation FROM terms WHERE item_id=? ORDER BY updated_at DESC LIMIT 80', (item_id,))]
            attachments = [dict(row) for row in db.execute('''SELECT id,text_json,text_status,page_count FROM attachments
                WHERE item_id=? AND text_status='indexed' AND text_json IS NOT NULL ORDER BY created_at''', (item_id,))]
        material = []
        def add(field, source_type, quote, attachment_id=None, page=None):
            quote = _text(quote, 12000)
            if quote:
                material.append({'field': field, 'sourceType': source_type, 'attachmentId': attachment_id, 'page': page, 'quote': quote})
        add('title', 'metadata', item.get('title'))
        add('abstract', 'abstract', item.get('abstract'))
        card_data = json.loads(card['data']) if card else {}
        if isinstance(card_data, dict):
            for key in ('researchQuestion', 'methods', 'findings', 'limitations', 'conclusion', 'summary'):
                add(key, 'reading_card', card_data.get(key))
        for note in notes:
            add('note', 'note', note['title'] + '\n' + note['content'])
        for term in terms:
            add('term', 'term', ' · '.join(filter(None, (term['term'], term['translation'], term['explanation']))))
        # Pages are deliberately bounded: profile evidence points to exact pages,
        # while no silent, unbounded PDF upload is ever possible.
        for attachment in attachments[:4]:
            try:
                pages = json.loads(attachment['text_json'])
            except (TypeError, ValueError):
                pages = []
            if not isinstance(pages, list):
                continue
            for page, value in enumerate(pages[:40], start=1):
                if _text(value, 50):
                    add('fulltext', 'pdf', value, attachment['id'], page)
        return item, card_data if isinstance(card_data, dict) else {}, material

    @staticmethod
    def _fingerprint(item, card, material):
        compact = [{'source': row['sourceType'], 'attachment': row['attachmentId'], 'page': row['page'], 'text': row['quote']} for row in material]
        value = dumps({'id': item['id'], 'revision': item['revision'], 'card': card, 'material': compact})
        return hashlib.sha256(value.encode('utf-8')).hexdigest()

    def _profile(self, item_id, force=False):
        item, card, material = self._material(item_id)
        fingerprint = self._fingerprint(item, card, material)
        with self.library.db() as db:
            existing = db.execute('SELECT * FROM article_profiles WHERE item_id=?', (item_id,)).fetchone()
            if existing and existing['fingerprint'] == fingerprint and existing['state'] == 'ready' and not force:
                return self._profile_value(existing)

        by_field = {}
        for row in material:
            by_field.setdefault(row['field'], row)
        pdf_pages = [row for row in material if row['sourceType'] == 'pdf']
        hints_by_field = {
            'researchQuestion': ('aim', 'objective', 'investigat', '目的', '旨在', '研究'),
            'methods': ('method', 'experiment', 'sample', 'model', '方法', '实验', '样本', '模型'),
            'findings': ('result', 'find', 'show', '发现', '表明', '结果'),
            'limitations': ('limitation', 'future', 'restrict', '局限', '未来', '不足'),
            'conclusion': ('conclude', 'suggest', 'implication', '结论', '提示', '建议'),
        }
        fields, evidence = {}, []
        for field, hints in hints_by_field.items():
            card_row = by_field.get(field)
            chosen = (card_row, _text(card_row['quote'], 900)) if card_row else None
            if not chosen:
                for row in [by_field.get('abstract'), *pdf_pages]:
                    if row:
                        excerpt = _matching_excerpt(row['quote'], hints)
                        if excerpt:
                            chosen = row, excerpt
                            break
            fields[field] = chosen[1] if chosen else ''
            if chosen:
                evidence.append({**chosen[0], 'field': field, 'quote': chosen[1]})
        summary_row = by_field.get('summary') or by_field.get('abstract') or by_field.get('title')
        summary = _text(summary_row['quote'], 1200) if summary_row else ''
        fields['summary'] = summary
        if summary_row:
            evidence.append({**summary_row, 'field': 'summary', 'quote': _text(summary, 900)})
        # Preserve page-level PDF material for relationships even when an abstract
        # already supplies every profile field. The page is part of its identity.
        for row in pdf_pages[:2]:
            evidence.append({**row, 'quote': _text(row['quote'], 900)})
        all_text = ' '.join(row['quote'][:6000] for row in material)
        counts = Counter(_tokens(all_text))
        keywords = [word for word, _count in counts.most_common(18)]
        if not evidence:
            evidence.append({'field': 'summary', 'sourceType': 'metadata', 'attachmentId': None, 'page': None, 'quote': _clip(item['title'], 900)})
        profile = {'itemId': item_id, 'title': item['title'], 'keywords': keywords, 'fields': fields,
                   'coverage': sorted({row['sourceType'] for row in material}), 'generatedBy': 'local-evidence-v1'}
        timestamp = now()
        with self.library.db(True) as db:
            old = db.execute('SELECT * FROM article_profiles WHERE item_id=?', (item_id,)).fetchone()
            profile_id, version = (old['id'], old['version'] + 1) if old else (uid(), 1)
            if old:
                db.execute('DELETE FROM article_profile_evidence WHERE profile_id=?', (profile_id,))
                db.execute('''UPDATE article_profiles SET fingerprint=?,profile_json=?,state='ready',warning='',version=?,updated_at=? WHERE id=?''',
                           (fingerprint, dumps(profile), version, timestamp, profile_id))
            else:
                db.execute('''INSERT INTO article_profiles(id,item_id,fingerprint,profile_json,state,warning,version,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?)''', (profile_id, item_id, fingerprint, dumps(profile), 'ready', '', version, timestamp, timestamp))
            values = []
            for source in evidence:
                values.append((uid(), profile_id, item_id, source['field'], source['sourceType'], source.get('attachmentId'), source.get('page'), source['quote'], timestamp))
            db.executemany('''INSERT INTO article_profile_evidence(id,profile_id,item_id,field,source_type,attachment_id,page,quote,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)''', values)
            row = db.execute('SELECT * FROM article_profiles WHERE id=?', (profile_id,)).fetchone()
        return self._profile_value(row)

    @staticmethod
    def _profile_value(row, evidence=None):
        value = dict(row)
        result = {'id': value['id'], 'itemId': value['item_id'], 'fingerprint': value['fingerprint'], 'profile': json.loads(value['profile_json']),
                  'state': value['state'], 'warning': value['warning'], 'version': value['version'], 'createdAt': value['created_at'], 'updatedAt': value['updated_at']}
        if evidence is not None:
            result['evidence'] = evidence
        return result

    def profile_get(self, item_id):
        with self.library.db() as db:
            row = db.execute('SELECT * FROM article_profiles WHERE item_id=?', (item_id,)).fetchone()
            evidence = [self._evidence_value(value) for value in db.execute('SELECT * FROM article_profile_evidence WHERE item_id=? ORDER BY created_at', (item_id,))]
        if not row:
            return {'itemId': item_id, 'state': 'missing', 'profile': None, 'evidence': []}
        return self._profile_value(row, evidence)

    @staticmethod
    def _evidence_value(row):
        value = dict(row)
        return {'id': value['id'], 'itemId': value['item_id'], 'field': value['field'], 'sourceType': value['source_type'],
                'attachmentId': value['attachment_id'], 'page': value['page'], 'quote': value['quote'], 'createdAt': value['created_at']}

    def submit_profile(self, payload):
        item_ids = list(dict.fromkeys(payload.get('itemIds') or ([payload['itemId']] if payload.get('itemId') else [])))
        require(0 < len(item_ids) <= 25 and all(isinstance(value, str) for value in item_ids), '请选择 1–25 篇文献')
        with self.library.db() as db:
            for item_id in item_ids:
                self.library._get(db, item_id)
        return self.jobs.create('relations.profile', {'itemIds': item_ids, 'force': bool(payload.get('force'))})

    def run_profile(self, payload, progress):
        ids = payload.get('itemIds') or []
        profiles = []
        for index, item_id in enumerate(ids):
            progress(index / max(len(ids), 1), f'提取文献画像 {index + 1}/{len(ids)}')
            profiles.append(self._profile(item_id, bool(payload.get('force'))))
        progress(.98, '文献画像已保存到本地')
        return {'profiles': [{'itemId': profile['itemId'], 'version': profile['version']} for profile in profiles]}

    def create(self, payload):
        item_ids = list(dict.fromkeys(payload.get('itemIds') or []))
        require(2 <= len(item_ids) <= 25 and all(isinstance(value, str) for value in item_ids), '请选择 2–25 篇文献进行关联分析')
        mode = payload.get('mode', 'compare')
        require(mode in MODES, '分析模式不正确')
        purpose = _text(payload.get('purpose'), 1800)
        require(len(purpose) <= 1800, '分析目标过长')
        if mode == 'custom':
            require(len(purpose) >= 4, '请说明自定义分析目标')
        title = _text(payload.get('title'), 180) or ('跨文献对比' if mode == 'compare' else '跨文献关联分析')
        remote_ai = bool(payload.get('remoteAI'))
        with self.library.db(True) as db:
            for item_id in item_ids:
                item = self.library._get(db, item_id)
                require(item.get('deletedAt') is None, '不能分析回收站中的文献')
            session_id, timestamp = uid(), now()
            settings = {'privacy': 'selected-local-material-only', 'version': 1}
            db.execute('''INSERT INTO relation_sessions(id,title,purpose,mode,remote_ai,state,settings_json,synthesis_json,created_at,updated_at)
                VALUES(?,?,?,?,?,'planned',?,?,?,?)''', (session_id, title, purpose, mode, int(remote_ai), dumps(settings), dumps({}), timestamp, timestamp))
            db.executemany('INSERT INTO relation_session_items(session_id,item_id,ordinal) VALUES(?,?,?)', [(session_id, item_id, index) for index, item_id in enumerate(item_ids)])
        return self.get(session_id)

    def _session_value(self, row, items=None, relations=None):
        value = dict(row)
        output = {'id': value['id'], 'title': value['title'], 'purpose': value['purpose'], 'mode': value['mode'], 'remoteAI': bool(value['remote_ai']),
                  'state': value['state'], 'jobId': value['job_id'], 'settings': json.loads(value['settings_json']), 'synthesis': json.loads(value['synthesis_json']),
                  'warning': value['warning'], 'createdAt': value['created_at'], 'updatedAt': value['updated_at']}
        if items is not None:
            output['items'] = items
        if relations is not None:
            output['relations'] = relations
        return output

    def _session_items(self, db, session_id):
        rows = db.execute('''SELECT si.item_id,si.profile_version,si.ordinal,i.title,i.authors,i.year,i.container,i.data,
            p.id AS profile_id,p.profile_json,p.state AS profile_state,p.version AS current_profile_version
            FROM relation_session_items si JOIN items i ON i.id=si.item_id LEFT JOIN article_profiles p ON p.item_id=si.item_id
            WHERE si.session_id=? ORDER BY si.ordinal''', (session_id,)).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            raw_citations = json.loads(value['data']).get('citationCount')
            citation_count = int(raw_citations) if isinstance(raw_citations, int) and raw_citations >= 0 else None
            result.append({'itemId': value['item_id'], 'profileVersion': value['profile_version'], 'currentProfileVersion': value['current_profile_version'],
                           'profileState': value['profile_state'] or 'missing', 'title': value['title'], 'authors': value['authors'], 'year': value['year'],
                           'container': value['container'], 'citationCount': citation_count,
                           'profile': json.loads(value['profile_json']) if value['profile_json'] else None})
        return result

    def _relation_value(self, row):
        value = dict(row)
        return {'id': value['id'], 'sessionId': value['session_id'], 'leftItemId': value['left_item_id'], 'rightItemId': value['right_item_id'], 'kind': 'evidence-inference',
                'type': value['relation_type'], 'typeLabel': TYPE_LABELS.get(value['relation_type'], value['relation_type']), 'confidence': value['confidence'],
                'rationale': value['rationale'], 'status': value['status'], 'engine': value['engine'], 'userNote': value['user_note'],
                'reviewState': value.get('review_state') or 'current',
                'createdAt': value['created_at'], 'updatedAt': value['updated_at']}

    def get(self, session_id):
        with self.library.db() as db:
            row = db.execute('SELECT * FROM relation_sessions WHERE id=?', (session_id,)).fetchone()
            require(row, '关联分析不存在')
            items = self._session_items(db, session_id)
            relations = [self._relation_value(value) for value in db.execute('''SELECT r.*,rr.review_state FROM article_relations r
                LEFT JOIN relation_reviews rr ON rr.relation_id=r.id WHERE r.session_id=? ORDER BY r.confidence DESC,r.created_at''', (session_id,))]
            latest = db.execute('SELECT id,summary_json FROM relation_runs WHERE session_id=? ORDER BY created_at DESC LIMIT 1', (session_id,)).fetchone()
        result = self._session_value(row, items, relations)
        result['latestRun'] = {'id': latest['id'], 'summary': json.loads(latest['summary_json'])} if latest else None
        return result

    def list(self, limit=100):
        limit = bounded_int(limit, 100, 1, 300)
        with self.library.db() as db:
            rows = db.execute('''SELECT s.*,COUNT(DISTINCT si.item_id) AS item_count,COUNT(DISTINCT r.id) AS relation_count
                FROM relation_sessions s LEFT JOIN relation_session_items si ON si.session_id=s.id LEFT JOIN article_relations r ON r.session_id=s.id
                GROUP BY s.id ORDER BY s.updated_at DESC LIMIT ?''', (limit,)).fetchall()
        return [{**self._session_value(row), 'itemCount': row['item_count'], 'relationCount': row['relation_count']} for row in rows]

    def manual_list(self, session_id):
        with self.library.db() as db:
            require(db.execute('SELECT 1 FROM relation_sessions WHERE id=?', (session_id,)).fetchone(), '关联分析不存在')
            rows = db.execute('''SELECT m.*,a.title AS left_title,b.title AS right_title FROM manual_relations m
                JOIN items a ON a.id=m.left_item_id JOIN items b ON b.id=m.right_item_id
                WHERE m.session_id=? ORDER BY m.created_at DESC''', (session_id,)).fetchall()
        return [{'id': row['id'], 'sessionId': session_id, 'leftItemId': row['left_item_id'],
                 'rightItemId': row['right_item_id'], 'leftTitle': row['left_title'], 'rightTitle': row['right_title'],
                 'label': row['label'], 'note': row['note'], 'kind': 'manual', 'createdAt': row['created_at']}
                for row in rows]

    def manual_add(self, payload):
        session_id = payload.get('sessionId')
        left_id, right_id = payload.get('leftItemId'), payload.get('rightItemId')
        require(isinstance(left_id, str) and isinstance(right_id, str) and left_id != right_id, '请选择两篇不同文献')
        label = _text(payload.get('label'), 80) or '人工关联'
        note = _text(payload.get('note'), 2000)
        left_id, right_id = sorted((left_id, right_id))
        with self.library.db(True) as db:
            count = db.execute('''SELECT count(*) FROM relation_session_items
                WHERE session_id=? AND item_id IN (?,?)''', (session_id, left_id, right_id)).fetchone()[0]
            require(count == 2, '两篇文献都必须在当前分析集中')
            timestamp = now()
            db.execute('''INSERT INTO manual_relations(id,session_id,left_item_id,right_item_id,label,note,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id,left_item_id,right_item_id,label)
                DO UPDATE SET note=excluded.note,updated_at=excluded.updated_at''',
                (uid(), session_id, left_id, right_id, label, note, timestamp, timestamp))
        return self.manual_list(session_id)

    def manual_remove(self, payload):
        with self.library.db(True) as db:
            row = db.execute('SELECT session_id FROM manual_relations WHERE id=?', (payload.get('id'),)).fetchone()
            require(row, '人工关联不存在')
            db.execute('DELETE FROM manual_relations WHERE id=?', (payload['id'],))
        return self.manual_list(row['session_id'])

    def runs(self, session_id):
        with self.library.db() as db:
            require(db.execute('SELECT 1 FROM relation_sessions WHERE id=?', (session_id,)).fetchone(), '关联分析不存在')
            rows = db.execute('SELECT * FROM relation_runs WHERE session_id=? ORDER BY created_at DESC LIMIT 30', (session_id,)).fetchall()
        return [{'id': row['id'], 'status': row['status'], 'summary': json.loads(row['summary_json']),
                 'createdAt': row['created_at'], 'completedAt': row['completed_at']} for row in rows]

    def diff(self, payload):
        session_id = payload.get('sessionId')
        runs = self.runs(session_id)
        require(runs, '尚无关联分析记录')
        new_run = payload.get('newRunId') or runs[0]['id']
        old_run = payload.get('oldRunId') or (runs[1]['id'] if len(runs) > 1 else None)
        known = {run['id'] for run in runs}
        require(new_run in known and (old_run is None or old_run in known), '关联分析版本不属于此会话')
        with self.library.db() as db:
            def snapshots(run_id):
                if not run_id:
                    return {}
                output = {}
                for row in db.execute('SELECT snapshot_json FROM relation_history WHERE run_id=?', (run_id,)):
                    snap = json.loads(row['snapshot_json'])
                    if snap['phase'] != 'current':
                        continue
                    relation = snap['relation']
                    key = self._relation_key(relation['left_item_id'], relation['right_item_id'], relation['relation_type'])
                    output[key] = snap
                return output
            before, after = snapshots(old_run), snapshots(new_run)
        changes = []
        for key in sorted(before.keys() | after.keys()):
            old, current = before.get(key), after.get(key)
            if not current or current['reviewState'] == 'missing':
                state = 'missing'
            elif not old or old['reviewState'] == 'missing':
                state = 'new'
            elif current['reviewState'] == 'changed' or [(e['item_id'], e['attachment_id'], e['page'], e['quote']) for e in old['evidence']] != [
                    (e['item_id'], e['attachment_id'], e['page'], e['quote']) for e in current['evidence']]:
                state = 'changed'
            else:
                state = 'unchanged'
            relation = (current or old)['relation']
            changes.append({'relationId': relation['id'], 'leftItemId': relation['left_item_id'],
                            'rightItemId': relation['right_item_id'], 'type': relation['relation_type'], 'state': state})
        return {'oldRunId': old_run, 'newRunId': new_run, 'changes': changes}

    def history(self, relation_id):
        with self.library.db() as db:
            require(db.execute('SELECT 1 FROM article_relations WHERE id=?', (relation_id,)).fetchone(), '关联记录不存在')
            rows = db.execute('''SELECT h.run_id,h.snapshot_json,h.created_at FROM relation_history h
                WHERE h.relation_id=? ORDER BY h.created_at DESC,h.rowid DESC LIMIT 20''', (relation_id,)).fetchall()
        output = []
        for row in rows:
            snapshot = json.loads(row['snapshot_json'])
            output.append({'runId': row['run_id'], 'phase': snapshot['phase'], 'reviewState': snapshot['reviewState'],
                           'createdAt': row['created_at'], 'evidence': [
                               {'itemId': value['item_id'], 'attachmentId': value['attachment_id'],
                                'page': value['page'], 'quote': value['quote']} for value in snapshot['evidence']]})
        return output

    def submit(self, payload):
        session_id = payload.get('sessionId')
        require(isinstance(session_id, str), '缺少关联分析标识')
        with self.library.db() as db:
            row = db.execute('SELECT * FROM relation_sessions WHERE id=?', (session_id,)).fetchone()
            require(row, '关联分析不存在')
            require(row['state'] not in ('running', 'queued'), '关联分析正在进行')
        job = self.jobs.create('relations.run', {'sessionId': session_id})
        with self.library.db(True) as db:
            db.execute("UPDATE relation_sessions SET state='queued',job_id=?,warning='',updated_at=? WHERE id=?", (job['jobId'], now(), session_id))
        return {**job, 'sessionId': session_id}

    @staticmethod
    def _best_evidence(evidence, profile, preferred=None):
        choices = [row for row in evidence if not preferred or row['field'] in preferred]
        choices = choices or evidence
        return max(choices, key=lambda row: len(set(_tokens(row['quote'])) & set(profile['profile'].get('keywords', []))), default=None)

    def _local_candidates(self, profiles):
        candidates = []
        for left_index in range(len(profiles)):
            for right_index in range(left_index + 1, len(profiles)):
                left, right = profiles[left_index], profiles[right_index]
                left_terms, right_terms = set(left['profile']['keywords']), set(right['profile']['keywords'])
                common = sorted(left_terms & right_terms, key=lambda value: (len(value), value), reverse=True)[:6]
                union = left_terms | right_terms
                similarity = len(common) / max(len(union), 1)
                methods = set(_tokens(left['profile']['fields'].get('methods', ''))) & set(_tokens(right['profile']['fields'].get('methods', '')))
                if not common and len(methods) < 2:
                    continue
                relation_type = 'method_compare' if len(methods) >= 2 else 'topic_overlap'
                confidence = round(min(.84, .30 + similarity * 2.4 + min(len(common), 5) * .055), 2)
                if not common:
                    confidence = .36
                keywords = '、'.join(common[:4]) if common else '共同使用的方法词：' + '、'.join(sorted(methods)[:3])
                rationale = f"本地材料显示两篇文献都涉及：{keywords}。这是待人工核对的关联候选，不表示它们的结论已经相互支持。"
                candidates.append({'left': left, 'right': right, 'type': relation_type, 'confidence': confidence, 'rationale': rationale,
                                   'engine': 'local', 'status': 'candidate', 'preferred': ('methods',) if relation_type == 'method_compare' else ('researchQuestion', 'summary', 'abstract')})
        return candidates

    def _remote_candidates(self, session, profiles, evidence_by_item):
        if not session['remote_ai']:
            return [], None, ''
        require(self.library.get_settings().get('online', True), '联网已关闭，不能使用联网 AI 关联分析')
        require(self.assistant and self.assistant.status().get('ready'), '请先在设置中配置并解锁阅读助手服务')
        papers = []
        for profile in profiles:
            paper_evidence = evidence_by_item.get(profile['itemId'], [])[:8]
            papers.append({'itemId': profile['itemId'], 'title': profile['profile']['title'], 'profile': profile['profile']['fields'],
                           'keywords': profile['profile']['keywords'][:16], 'evidence': [{'id': row['id'], 'field': row['field'], 'page': row['page'], 'quote': _clip(row['quote'], 620)} for row in paper_evidence]})
        value = self.assistant.relation_json({'purpose': session['purpose'], 'mode': session['mode'], 'papers': papers})
        raw_relations = value.get('relations') if isinstance(value, dict) else []
        output = []
        by_id = {profile['itemId']: profile for profile in profiles}
        known_evidence = {row['id']: row for rows in evidence_by_item.values() for row in rows}
        for raw in raw_relations if isinstance(raw_relations, list) else []:
            if not isinstance(raw, dict):
                continue
            left_id, right_id, relation_type = raw.get('leftItemId'), raw.get('rightItemId'), raw.get('type')
            if left_id not in by_id or right_id not in by_id or left_id == right_id or relation_type not in TYPES:
                continue
            raw_evidence = raw.get('evidence')
            if not isinstance(raw_evidence, list):
                continue
            source_ids = [value.get('evidenceId') for value in raw_evidence if isinstance(value, dict)]
            selected = [known_evidence[value] for value in source_ids if value in known_evidence]
            if (len(selected) != len(source_ids) or
                    {value['itemId'] for value in selected} != {left_id, right_id} or
                    any(value['itemId'] not in (left_id, right_id) for value in selected)):
                continue
            try:
                confidence = float(raw.get('confidence', .5))
            except (TypeError, ValueError):
                confidence = .5
            output.append({'left': by_id[left_id], 'right': by_id[right_id], 'type': relation_type, 'confidence': max(.05, min(.95, confidence)),
                           'rationale': _text(raw.get('rationale'), 1800) or '模型未提供充分说明。', 'engine': 'remote-evidence',
                           'status': 'candidate', 'evidence': selected})
        synthesis = _text(value.get('synthesis') if isinstance(value, dict) else '', 8000)
        return output, synthesis if output else None, '' if output else '联网 AI 未产生可验证的双侧引用，已使用本地候选'

    @staticmethod
    def _relation_key(left_id, right_id, relation_type):
        return (*sorted((left_id, right_id)), relation_type)

    @staticmethod
    def _evidence_hash(db, evidence):
        attachment_ids = {row.get('attachmentId') for row in evidence if row.get('attachmentId')}
        versions = {}
        if attachment_ids:
            placeholders = ','.join('?' for _ in attachment_ids)
            versions = {row['id']: row['version'] for row in db.execute(
                'SELECT id,version FROM attachments WHERE id IN (' + placeholders + ')', tuple(attachment_ids))}
        compact = sorted((row['itemId'], row.get('attachmentId') or '', versions.get(row.get('attachmentId'), ''),
                          row.get('page') or 0, _text(row['quote'])) for row in evidence)
        return hashlib.sha256(dumps(compact).encode('utf-8')).hexdigest()

    @staticmethod
    def _relation_sources(db, relation_id):
        return [dict(row) for row in db.execute('SELECT * FROM relation_evidence WHERE relation_id=? ORDER BY item_id,attachment_id,page,quote', (relation_id,))]

    def _store_relations(self, session_id, candidates, evidence_by_item):
        timestamp = now()
        with self.library.db(True) as db:
            run_id = uid()
            db.execute('INSERT INTO relation_runs(id,session_id,status,created_at) VALUES(?,?,?,?)',
                       (run_id, session_id, 'completed', timestamp))
            old_rows = {self._relation_key(row['left_item_id'], row['right_item_id'], row['relation_type']): dict(row)
                        for row in db.execute('SELECT * FROM article_relations WHERE session_id=?', (session_id,))}
            seen = set()
            counts = {'new': 0, 'unchanged': 0, 'changed': 0, 'missing': 0}
            for candidate in candidates:
                left_id, right_id = candidate['left']['itemId'], candidate['right']['itemId']
                key = self._relation_key(left_id, right_id, candidate['type'])
                if key in seen:
                    continue
                seen.add(key)
                evidence = candidate.get('evidence')
                if not evidence:
                    evidence = [self._best_evidence(evidence_by_item.get(left_id, []), candidate['left'], candidate.get('preferred')),
                                self._best_evidence(evidence_by_item.get(right_id, []), candidate['right'], candidate.get('preferred'))]
                evidence = [row for row in evidence if row]
                if {row['itemId'] for row in evidence} != {left_id, right_id}:
                    continue
                new_hash = self._evidence_hash(db, evidence)
                old = old_rows.pop(key, None)
                if old:
                    relation_id = old['id']
                    old_evidence = self._relation_sources(db, relation_id)
                    review = db.execute('SELECT evidence_hash,review_state FROM relation_reviews WHERE relation_id=?', (relation_id,)).fetchone()
                    old_hash = review['evidence_hash'] if review else self._evidence_hash(db, old_evidence)
                    changed = old_hash != new_hash
                    review_state = 'changed' if changed or (review and review['review_state'] in ('changed', 'missing')) else 'current'
                    counts['changed' if changed else 'unchanged'] += 1
                    previous = {'phase': 'previous', 'relation': old, 'evidence': old_evidence,
                                'reviewState': review['review_state'] if review else 'current'}
                    db.execute('INSERT INTO relation_history(id,relation_id,run_id,snapshot_json,created_at) VALUES(?,?,?,?,?)',
                               (uid(), relation_id, run_id, dumps(previous), timestamp))
                    db.execute('''UPDATE article_relations SET confidence=?,rationale=?,engine=?,updated_at=? WHERE id=?''',
                               (candidate['confidence'], candidate['rationale'], candidate['engine'], timestamp, relation_id))
                    db.execute('DELETE FROM relation_evidence WHERE relation_id=?', (relation_id,))
                else:
                    relation_id = uid()
                    review_state = 'current'
                    counts['new'] += 1
                    db.execute('''INSERT INTO article_relations(id,session_id,left_item_id,right_item_id,relation_type,confidence,rationale,status,engine,user_note,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (relation_id, session_id, left_id, right_id, candidate['type'], candidate['confidence'],
                         candidate['rationale'], 'candidate', candidate['engine'], '', timestamp, timestamp))
                for source in evidence:
                    db.execute('''INSERT INTO relation_evidence(id,relation_id,item_id,attachment_id,page,profile_evidence_id,quote,created_at)
                        VALUES(?,?,?,?,?,?,?,?)''', (uid(), relation_id, source['itemId'], source.get('attachmentId'), source.get('page'), source.get('id'), source['quote'], timestamp))
                db.execute('''INSERT INTO relation_reviews(relation_id,evidence_hash,review_state,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(relation_id) DO UPDATE SET evidence_hash=excluded.evidence_hash,review_state=excluded.review_state,updated_at=excluded.updated_at''',
                    (relation_id, new_hash, review_state, timestamp))
                snapshot = {'phase': 'current', 'relation': dict(db.execute('SELECT * FROM article_relations WHERE id=?', (relation_id,)).fetchone()),
                            'evidence': self._relation_sources(db, relation_id), 'reviewState': review_state}
                db.execute('INSERT INTO relation_history(id,relation_id,run_id,snapshot_json,created_at) VALUES(?,?,?,?,?)',
                           (uid(), relation_id, run_id, dumps(snapshot), timestamp))
            for old in old_rows.values():
                relation_id = old['id']
                counts['missing'] += 1
                db.execute('''INSERT INTO relation_reviews(relation_id,evidence_hash,review_state,updated_at) VALUES(?,?,?,?)
                    ON CONFLICT(relation_id) DO UPDATE SET review_state=excluded.review_state,updated_at=excluded.updated_at''',
                    (relation_id, self._evidence_hash(db, self._relation_sources(db, relation_id)), 'missing', timestamp))
                snapshot = {'phase': 'current', 'relation': old, 'evidence': self._relation_sources(db, relation_id), 'reviewState': 'missing'}
                db.execute('INSERT INTO relation_history(id,relation_id,run_id,snapshot_json,created_at) VALUES(?,?,?,?,?)',
                           (uid(), relation_id, run_id, dumps(snapshot), timestamp))
            db.execute('UPDATE relation_runs SET summary_json=?,completed_at=? WHERE id=?', (dumps(counts), timestamp, run_id))
        return {'runId': run_id, 'count': counts['new'] + counts['unchanged'] + counts['changed'], **counts}

    def run(self, payload, progress):
        session_id = payload.get('sessionId')
        try:
            with self.library.db(True) as db:
                session = db.execute('SELECT * FROM relation_sessions WHERE id=?', (session_id,)).fetchone()
                require(session, '关联分析不存在')
                db.execute("UPDATE relation_sessions SET state='running',warning='',updated_at=? WHERE id=?", (now(), session_id))
                item_ids = [row[0] for row in db.execute('SELECT item_id FROM relation_session_items WHERE session_id=? ORDER BY ordinal', (session_id,))]
            profiles = []
            for index, item_id in enumerate(item_ids):
                progress(.06 + index / max(len(item_ids), 1) * .37, f'分析文献画像 {index + 1}/{len(item_ids)}')
                profiles.append(self._profile(item_id))
            with self.library.db() as db:
                evidence_by_item = {profile['itemId']: [self._evidence_value(row) for row in db.execute('SELECT * FROM article_profile_evidence WHERE profile_id=? ORDER BY created_at', (profile['id'],))] for profile in profiles}
            progress(.49, '正在匹配研究主题、方法与证据')
            local = self._local_candidates(profiles)
            remote, synthesis, warning = [], None, ''
            try:
                remote, synthesis, warning = self._remote_candidates(session, profiles, evidence_by_item)
            except AppError as exc:
                # Local evidence candidates remain usable even when the optional
                # model is unavailable; the user sees the concrete reason.
                warning = '联网 AI 未完成：' + str(exc)[:500]
            progress(.77, '正在保存关联证据')
            saved = self._store_relations(session_id, remote if remote else local, evidence_by_item)
            count = saved['count']
            common = Counter(token for profile in profiles for token in profile['profile']['keywords']).most_common(8)
            local_synthesis = {'kind': 'local', 'summary': f'已基于 {len(profiles)} 篇文献建立 {count} 条可核验证据关联。请在确认后写入综述笔记。',
                               'commonKeywords': [value for value, _ in common], 'generatedAt': now()}
            if synthesis:
                local_synthesis.update({'kind': 'remote-evidence', 'summary': synthesis, 'generatedAt': now()})
            with self.library.db(True) as db:
                db.executemany('UPDATE relation_session_items SET profile_version=? WHERE session_id=? AND item_id=?',
                               [(profile['version'], session_id, profile['itemId']) for profile in profiles])
                db.execute("UPDATE relation_sessions SET state='completed',synthesis_json=?,warning=?,updated_at=? WHERE id=?",
                           (dumps(local_synthesis), warning, now(), session_id))
            progress(.97, '关联分析已保存到本地')
            return {'sessionId': session_id, 'profiles': len(profiles), 'relations': count, 'warning': warning, 'runId': saved['runId'],
                    'review': {key: saved[key] for key in ('new', 'unchanged', 'changed', 'missing')}}
        except Exception:
            with self.library.db(True) as db:
                db.execute("UPDATE relation_sessions SET state='failed',updated_at=? WHERE id=?", (now(), session_id))
            raise

    def results(self, payload):
        session_id = payload.get('sessionId')
        status = payload.get('status')
        relation_type = payload.get('type')
        require(isinstance(session_id, str), '缺少关联分析标识')
        where, args = ['r.session_id=?'], [session_id]
        if status in ('candidate', 'confirmed', 'rejected'):
            where.append('r.status=?'); args.append(status)
        if relation_type in TYPES:
            where.append('r.relation_type=?'); args.append(relation_type)
        with self.library.db() as db:
            rows = db.execute('''SELECT r.*,rr.review_state,li.title AS left_title,ri.title AS right_title FROM article_relations r
                JOIN items li ON li.id=r.left_item_id JOIN items ri ON ri.id=r.right_item_id
                LEFT JOIN relation_reviews rr ON rr.relation_id=r.id WHERE ''' + ' AND '.join(where) +
                ' ORDER BY r.confidence DESC,r.created_at', args).fetchall()
        return [{**self._relation_value(row), 'leftTitle': row['left_title'], 'rightTitle': row['right_title']} for row in rows]

    def evidence(self, relation_id):
        with self.library.db() as db:
            relation = db.execute('''SELECT r.*,rr.review_state,li.title AS left_title,ri.title AS right_title FROM article_relations r
                JOIN items li ON li.id=r.left_item_id JOIN items ri ON ri.id=r.right_item_id
                LEFT JOIN relation_reviews rr ON rr.relation_id=r.id WHERE r.id=?''', (relation_id,)).fetchone()
            require(relation, '关联记录不存在')
            rows = db.execute('''SELECT e.*,i.title AS item_title,a.version AS attachment_version
                FROM relation_evidence e JOIN items i ON i.id=e.item_id LEFT JOIN attachments a ON a.id=e.attachment_id
                WHERE e.relation_id=? ORDER BY e.created_at''', (relation_id,)).fetchall()
        return {**self._relation_value(relation), 'leftTitle': relation['left_title'], 'rightTitle': relation['right_title'],
                'evidence': [{'id': row['id'], 'itemId': row['item_id'], 'itemTitle': row['item_title'], 'attachmentId': row['attachment_id'],
                              'page': row['page'], 'attachmentVersion': row['attachment_version'],
                              'profileEvidenceId': row['profile_evidence_id'], 'quote': row['quote']} for row in rows]}

    def confirm(self, payload):
        relation_id, status = payload.get('relationId'), payload.get('status')
        require(status in ('candidate', 'confirmed', 'rejected'), '确认状态不正确')
        note = _text(payload.get('userNote'), 2000)
        with self.library.db(True) as db:
            row = db.execute('SELECT * FROM article_relations WHERE id=?', (relation_id,)).fetchone()
            require(row, '关联记录不存在')
            review = db.execute('SELECT review_state FROM relation_reviews WHERE relation_id=?', (relation_id,)).fetchone()
            require(status != 'confirmed' or not review or review['review_state'] != 'missing', '该关联未在最新分析中出现，不能直接确认')
            db.execute('UPDATE article_relations SET status=?,user_note=?,updated_at=? WHERE id=?', (status, note, now(), relation_id))
            if review and status == 'confirmed':
                db.execute("UPDATE relation_reviews SET review_state='current',updated_at=? WHERE relation_id=?", (now(), relation_id))
            db.execute('UPDATE relation_sessions SET updated_at=? WHERE id=?', (now(), row['session_id']))
        return self.evidence(relation_id)

    def export(self, payload):
        session_id = payload.get('sessionId')
        require(isinstance(session_id, str), '缺少关联分析标识')
        with self.library.db() as db:
            session = db.execute('SELECT * FROM relation_sessions WHERE id=?', (session_id,)).fetchone()
            require(session, '关联分析不存在')
            items = self._session_items(db, session_id)
            relations = [dict(row) for row in db.execute('''SELECT r.*,li.title AS left_title,ri.title AS right_title FROM article_relations r
                JOIN items li ON li.id=r.left_item_id JOIN items ri ON ri.id=r.right_item_id
                LEFT JOIN relation_reviews rr ON rr.relation_id=r.id
                WHERE r.session_id=? AND r.status='confirmed' AND COALESCE(rr.review_state,'current')='current'
                ORDER BY r.confidence DESC''', (session_id,))]
            pending_review = [dict(row) for row in db.execute('''SELECT r.*,rr.review_state FROM article_relations r
                JOIN relation_reviews rr ON rr.relation_id=r.id WHERE r.session_id=? AND r.status='confirmed' AND rr.review_state<>'current' ''', (session_id,))]
            evidence_by_relation = {relation['id']: [dict(row) for row in db.execute('''SELECT e.*,i.title AS item_title,a.version AS attachment_version
                FROM relation_evidence e JOIN items i ON i.id=e.item_id LEFT JOIN attachments a ON a.id=e.attachment_id
                WHERE e.relation_id=? ORDER BY e.item_id,e.page''', (relation['id'],))] for relation in relations}
        synthesis = json.loads(session['synthesis_json'])
        lines = [f'# {session["title"]}', '', f'分析目标：{session["purpose"] or "跨文献比较"}', '', '## 文献范围']
        lines.extend(f'- {item["title"]}（{item["year"] or "年份待补充"}）' for item in items)
        lines.extend(['', '## 待核实的自动线索', synthesis.get('summary') or '尚未生成综合线索。',
                      '此处为自动生成的阅读线索，只有下方逐条确认且证据仍有效的关系可作为研究笔记。', '', '## 已确认关联'])
        if relations:
            for relation in relations:
                lines.extend([f'### {TYPE_LABELS.get(relation["relation_type"], relation["relation_type"])}',
                              f'- {relation["left_title"]} ↔ {relation["right_title"]}', f'- {relation["rationale"]}',
                              f'- 置信度：{round(float(relation["confidence"]) * 100)}%'])
                if relation['user_note']:
                    lines.append(f'- 研究者备注：{relation["user_note"]}')
                lines.append('- 核对原句：')
                for source in evidence_by_relation[relation['id']]:
                    location = f'第 {source["page"]} 页' if source['page'] else '题录、摘要或阅读卡'
                    version = f' · 附件版本 {source["attachment_version"]}' if source['attachment_version'] else ''
                    link = (f' [打开原文](research://attachment/{source["attachment_id"]}?page={source["page"]})'
                            if source['attachment_id'] and source['page'] else '')
                    lines.append(f'  - {source["item_title"]} · {location}{version}：{source["quote"]}{link}')
        else:
            lines.append('尚无已确认关联。请先在关联工作区核对证据并确认。')
        if pending_review:
            lines.extend(['', '## 待复核的原有判断',
                          f'{len(pending_review)} 条曾确认的关系在重跑后证据发生变化或未再次出现，已保留原判断，但未列入上方已确认内容。'])
        note = self.library.note_save({'title': '关联综述 · ' + session['title'], 'content': '\n'.join(lines), 'tags': ['跨文献关联', '综述草稿']})
        with self.library.db(True) as db:
            db.execute('INSERT OR IGNORE INTO relation_synthesis_notes(id,session_id,note_id,created_at) VALUES(?,?,?,?)', (uid(), session_id, note['id'], now()))
        return note

    def for_item(self, item_id, limit=20):
        limit = bounded_int(limit, 20, 1, 100)
        with self.library.db() as db:
            rows = db.execute('''SELECT r.*,rr.review_state,li.title AS left_title,ri.title AS right_title FROM article_relations r
                JOIN items li ON li.id=r.left_item_id JOIN items ri ON ri.id=r.right_item_id
                LEFT JOIN relation_reviews rr ON rr.relation_id=r.id
                WHERE (r.left_item_id=? OR r.right_item_id=?) AND r.status<>'rejected' ORDER BY r.updated_at DESC LIMIT ?''', (item_id, item_id, limit)).fetchall()
        return [{**self._relation_value(row), 'leftTitle': row['left_title'], 'rightTitle': row['right_title']} for row in rows]
