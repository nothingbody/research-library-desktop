from __future__ import annotations

"""Local, page-addressable evidence index for selected library documents."""

import hashlib
import json
import re

from .common import dumps, norm, now, require, uid


def _clean(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _pieces(value, size=1500, overlap=150):
    text = _clean(value)
    for start in range(0, len(text), size - overlap):
        part = text[start:start + size]
        if part:
            yield start, part
        if start + size >= len(text):
            break


class Documents:
    def __init__(self, library):
        self.library = library

    def scope(self, payload):
        ids = list(dict.fromkeys(payload.get('itemIds') or []))
        collection_id = payload.get('collectionId')
        if collection_id:
            with self.library.db() as db:
                require(db.execute('SELECT 1 FROM collections WHERE id=?', (collection_id,)).fetchone(), '集合不存在')
                ids.extend(row[0] for row in db.execute('''SELECT ci.item_id FROM collection_items ci JOIN items i ON i.id=ci.item_id
                    WHERE ci.collection_id=? AND i.deleted_at IS NULL ORDER BY i.updated_at DESC LIMIT 100''', (collection_id,)))
        ids = list(dict.fromkeys(ids))
        require(0 < len(ids) <= 100 and all(isinstance(value, str) for value in ids), '请选择 1–100 篇文献或一个集合')
        with self.library.db() as db:
            for item_id in ids:
                item = self.library._get(db, item_id)
                require(item.get('deletedAt') is None, '不能对回收站文献提问')
        return ids

    def _replace(self, db, item_id, attachment_id, source_type, source_id, version, pages):
        existing = db.execute('''SELECT source_version FROM document_chunks WHERE item_id=? AND source_type=? AND source_id=? LIMIT 1''',
                              (item_id, source_type, source_id)).fetchone()
        if existing and existing['source_version'] == version:
            return 0
        db.execute('DELETE FROM document_chunks WHERE item_id=? AND source_type=? AND source_id=?',
                   (item_id, source_type, source_id))
        inserted = 0
        for page, text in pages:
            for start, part in _pieces(text):
                db.execute('''INSERT INTO document_chunks(id,item_id,attachment_id,source_type,source_id,source_version,page,start_offset,text,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (uid(), item_id, attachment_id, source_type, source_id, version, page, start, part, now()))
                inserted += 1
        return inserted

    def index(self, item_id):
        with self.library.db(True) as db:
            item = self.library._get(db, item_id)
            count = self._replace(db, item_id, None, 'abstract', item_id, str(item['revision']),
                                  [(None, item.get('abstract') or '')])
            attachments = db.execute('''SELECT id,version,text_json FROM attachments
                WHERE item_id=? AND text_status='indexed' AND text_json IS NOT NULL''', (item_id,)).fetchall()
            current = {row['id'] for row in attachments}
            for row in attachments:
                pages = json.loads(row['text_json'])
                if not isinstance(pages, list):
                    continue
                fingerprint = row['version'] + ':' + hashlib.sha256(row['text_json'].encode('utf-8')).hexdigest()
                count += self._replace(db, item_id, row['id'], 'pdf', row['id'], fingerprint,
                                       [(page, value) for page, value in enumerate(pages, 1)])
            for row in db.execute('SELECT DISTINCT source_id FROM document_chunks WHERE item_id=? AND source_type=\'pdf\'', (item_id,)).fetchall():
                if row['source_id'] not in current:
                    db.execute("DELETE FROM document_chunks WHERE item_id=? AND source_type='pdf' AND source_id=?", (item_id, row['source_id']))
            card = db.execute('SELECT id,data,revision FROM reading_cards WHERE item_id=?', (item_id,)).fetchone()
            if card:
                text = ' '.join(str(value) for value in json.loads(card['data']).values() if isinstance(value, str))
                count += self._replace(db, item_id, None, 'reading_card', card['id'], str(card['revision']), [(None, text)])
            db.execute("DELETE FROM document_chunks WHERE item_id=? AND source_type='reading_card' AND source_id<>?",
                       (item_id, card['id'] if card else ''))
            annotations = db.execute('''SELECT a.id,a.attachment_id,a.revision,a.data
                FROM annotations a JOIN attachments at ON at.id=a.attachment_id WHERE at.item_id=?''', (item_id,)).fetchall()
            annotation_ids = {row['id'] for row in annotations}
            for row in db.execute("SELECT DISTINCT source_id FROM document_chunks WHERE item_id=? AND source_type='annotation'", (item_id,)).fetchall():
                if row['source_id'] not in annotation_ids:
                    db.execute("DELETE FROM document_chunks WHERE item_id=? AND source_type='annotation' AND source_id=?", (item_id, row['source_id']))
            for annotation in annotations:
                data = json.loads(annotation['data'])
                text = (data.get('quote') or '') + ' ' + (data.get('comment') or '')
                count += self._replace(db, item_id, annotation['attachment_id'], 'annotation', annotation['id'],
                                       str(annotation['revision']), [(int(data.get('pageIndex') or 0) + 1, text)])
        return {'itemId': item_id, 'newChunks': count}

    def search(self, payload):
        ids = self.scope(payload)
        question = _clean(payload.get('question'))[:1500]
        require(len(question) >= 2, '请输入研究问题')
        source_types = payload.get('sourceTypes') or ['abstract', 'pdf', 'annotation', 'reading_card']
        require(isinstance(source_types, list) and source_types and set(source_types) <= {'abstract', 'pdf', 'annotation', 'reading_card'}, '材料来源不正确')
        for item_id in ids:
            self.index(item_id)
        terms = set(re.findall(r'[a-z][a-z0-9-]{2,}', norm(question)))
        for run in re.findall(r'[\u3400-\u9fff]{2,}', question):
            terms.update(run[i:i + 2] for i in range(len(run) - 1))
        terms = {term for term in terms if term not in {'研究', '文章', '论文', '什么', '如何', '哪些', '这个', '进行'}}
        placeholders = ','.join('?' for _ in ids)
        type_placeholders = ','.join('?' for _ in source_types)
        with self.library.db() as db:
            rows = db.execute(f'''SELECT c.*,i.title FROM document_chunks c JOIN items i ON i.id=c.item_id
                WHERE c.item_id IN ({placeholders}) AND c.source_type IN ({type_placeholders})''', (*ids, *source_types)).fetchall()
        scored = []
        for row in rows:
            body = norm(row['text'])
            score = sum(min(body.count(term), 3) for term in terms)
            if score:
                scored.append((score, dict(row)))
        scored.sort(key=lambda value: (-value[0], value[1]['item_id'], value[1]['page'] or 0, value[1]['start_offset']))
        limit = max(1, min(30, int(payload.get('limit') or 12)))
        candidates = list(scored)
        # Chinese questions often have no shared tokens with English abstracts.
        # Within the user's explicit paper scope, include a bounded abstract (or
        # first PDF chunk) for each otherwise unrepresented paper so the model
        # can judge the question against real text and still cite exact quotes.
        if re.search(r'[\u3400-\u9fff]', question):
            represented = {row['item_id'] for _, row in candidates}
            for item_id in ids:
                if item_id in represented:
                    continue
                item_candidates = [dict(row) for row in rows if row['item_id'] == item_id]
                item_candidates.sort(key=lambda row: ({'abstract': 0, 'pdf': 1, 'reading_card': 2, 'annotation': 3}.get(row['source_type'], 4),
                                                      row['page'] or 0, row['start_offset']))
                if item_candidates:
                    candidates.append((0, item_candidates[0]))
                    represented.add(item_id)
        # Reserve one excerpt per represented paper before adding more pages
        # from any single paper. A long PDF must not consume the entire scope.
        selected, represented = [], set()
        for candidate in candidates:
            if candidate[1]['item_id'] not in represented and len(selected) < limit:
                selected.append(candidate)
                represented.add(candidate[1]['item_id'])
        selected_ids = {row['id'] for _, row in selected}
        for candidate in candidates:
            if len(selected) >= limit:
                break
            if candidate[1]['id'] not in selected_ids:
                selected.append(candidate)
        retrieved = {row['item_id'] for _, row in selected}
        evidence_coverage = {'selectedItemCount': len(ids), 'retrievedItemCount': len(retrieved), 'excerptCount': len(selected),
                             'retrievedItemIds': [item_id for item_id in ids if item_id in retrieved],
                             'missingItemIds': [item_id for item_id in ids if item_id not in retrieved]}
        return {'itemIds': ids, 'question': question, 'totalMatches': len(scored), 'coverage': sorted({row['source_type'] for row in rows}),
                'evidenceCoverage': evidence_coverage,
                'chunks': [{'id': row['id'], 'itemId': row['item_id'], 'title': row['title'], 'attachmentId': row['attachment_id'],
                            'sourceType': row['source_type'], 'sourceId': row['source_id'], 'sourceVersion': row['source_version'],
                            'page': row['page'], 'startOffset': row['start_offset'], 'quote': row['text'], 'score': score}
                           for score, row in selected]}
