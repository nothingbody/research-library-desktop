from __future__ import annotations

"""Scoped research conversations with source-checked, page-addressable citations."""

import json

from .common import AppError, dumps, now, require, uid


class ResearchAsk:
    def __init__(self, library, documents, assistant, jobs):
        self.library, self.documents, self.assistant, self.jobs = library, documents, assistant, jobs

    def create(self, payload):
        ids = self.documents.scope(payload)
        title = str(payload.get('title') or '文献问答').strip()[:160]
        timestamp, conversation_id = now(), uid()
        with self.library.db(True) as db:
            db.execute('''INSERT INTO research_conversations(id,title,item_ids_json,collection_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?)''',
                (conversation_id, title, dumps(ids), payload.get('collectionId'), timestamp, timestamp))
        return self.get(conversation_id)

    def list(self, limit=100):
        with self.library.db() as db:
            rows = db.execute('''SELECT * FROM research_conversations ORDER BY updated_at DESC LIMIT ?''',
                              (max(1, min(300, int(limit)) ),)).fetchall()
        return [{'id': row['id'], 'title': row['title'], 'itemIds': json.loads(row['item_ids_json']),
                 'updatedAt': row['updated_at']} for row in rows]

    def get(self, conversation_id):
        with self.library.db() as db:
            row = db.execute('SELECT * FROM research_conversations WHERE id=?', (conversation_id,)).fetchone()
            require(row, '文献问答不存在')
            messages = db.execute('SELECT * FROM research_messages WHERE conversation_id=? ORDER BY created_at,rowid',
                                  (conversation_id,)).fetchall()
            items = db.execute('SELECT id,title FROM items WHERE id IN (' + ','.join('?' for _ in json.loads(row['item_ids_json'])) + ')',
                               json.loads(row['item_ids_json'])).fetchall()
        return {'id': row['id'], 'title': row['title'], 'itemIds': json.loads(row['item_ids_json']),
                'collectionId': row['collection_id'], 'items': [dict(value) for value in items],
                'messages': [{'id': value['id'], 'role': value['role'], 'content': value['content'], 'status': value['status'],
                              'result': json.loads(value['result_json']), 'jobId': value['job_id'], 'createdAt': value['created_at']}
                             for value in messages], 'createdAt': row['created_at'], 'updatedAt': row['updated_at']}

    def send(self, payload):
        conversation_id = payload.get('conversationId')
        question = str(payload.get('question') or '').strip()
        require(2 <= len(question) <= 1500, '问题应为 2–1500 个字符')
        types = payload.get('sourceTypes') or ['abstract', 'pdf', 'annotation', 'reading_card']
        require(isinstance(types, list) and types and set(types) <= {'abstract', 'pdf', 'annotation', 'reading_card'}, '材料来源不正确')
        self.get(conversation_id)
        timestamp, answer_id = now(), uid()
        with self.library.db(True) as db:
            pending = db.execute("SELECT 1 FROM research_messages WHERE conversation_id=? AND role='assistant' AND status='pending' LIMIT 1", (conversation_id,)).fetchone()
            require(not pending, '已有问题正在处理，请等待完成后继续提问')
            db.execute('INSERT INTO research_messages(id,conversation_id,role,content,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                       (uid(), conversation_id, 'user', question, timestamp, timestamp))
            db.execute('''INSERT INTO research_messages(id,conversation_id,role,content,status,result_json,created_at,updated_at)
                VALUES(?,?,?,'','pending',?,?,?)''',
                (answer_id, conversation_id, 'assistant', dumps({'sourceTypes': types}), timestamp, timestamp))
            db.execute('UPDATE research_conversations SET updated_at=? WHERE id=?', (timestamp, conversation_id))
        job = self.jobs.create('research-ask.run', {'answerId': answer_id})
        with self.library.db(True) as db:
            db.execute('UPDATE research_messages SET job_id=? WHERE id=?', (job['jobId'], answer_id))
        return {'conversationId': conversation_id, 'answerId': answer_id, **job}

    def save_claim(self, payload):
        with self.library.db() as db:
            row = db.execute("SELECT * FROM research_messages WHERE id=? AND role='assistant' AND status='completed'",
                             (payload.get('messageId'),)).fetchone()
        require(row, '问答结论不存在或尚未完成')
        result = json.loads(row['result_json'])
        index = payload.get('claimIndex')
        claims = result.get('claims') or []
        require(type(index) is int and 0 <= index < len(claims), '结论序号不正确')
        claim = claims[index]
        citations = claim.get('citations') or []
        require(citations, '该结论没有原文引用')
        for item_id in {citation['itemId'] for citation in citations}:
            self.documents.index(item_id)
        with self.library.db() as db:
            for citation in citations:
                chunk = db.execute('SELECT source_version,text FROM document_chunks WHERE id=? AND item_id=?',
                                   (citation['id'], citation['itemId'])).fetchone()
                require(chunk and chunk['source_version'] == citation['sourceVersion'] and
                        ' '.join(citation['quote'].split()) in ' '.join(chunk['text'].split()),
                        '引用来源已变化，请重新提问并核对')
        lines = ['> 待研究者核对的 AI 辅助结论；保存时已重新校验下列原文。', '', claim['text'].strip(), '', '### 原文依据']
        for citation in citations:
            location = f"第 {citation['page']} 页" if citation.get('page') else citation['sourceType']
            target = (f"research://attachment/{citation['attachmentId']}?page={citation['page']}"
                      if citation.get('attachmentId') and citation.get('page') else '')
            lines.extend(['', f"- {citation['title']} · {location}" + (f" · [打开原文]({target})" if target else ''),
                          f"> {citation['quote']}"])
        return self.library.note_save({'title': '待核对：' + claim['text'].strip()[:80],
                                       'content': '\n'.join(lines), 'tags': ['AI辅助', '待核对']})

    def run(self, payload, progress):
        answer_id = payload.get('answerId')
        with self.library.db() as db:
            row = db.execute('SELECT * FROM research_messages WHERE id=? AND role=\'assistant\'', (answer_id,)).fetchone()
            require(row, '文献问答任务不存在')
            conversation = db.execute('SELECT * FROM research_conversations WHERE id=?', (row['conversation_id'],)).fetchone()
            history = db.execute('''SELECT role,content FROM research_messages WHERE conversation_id=? AND created_at<=?
                ORDER BY created_at,rowid''', (row['conversation_id'], row['created_at'])).fetchall()
        question = next((value['content'] for value in reversed(history) if value['role'] == 'user'), '')
        require(question, '原问题不存在')
        item_ids = json.loads(conversation['item_ids_json'])
        source_types = json.loads(row['result_json']).get('sourceTypes') or ['abstract', 'pdf']
        try:
            progress(.1, '正在本地检索所选文献的原文片段')
            found = self.documents.search({'itemIds': item_ids, 'question': question, 'sourceTypes': source_types,
                                           'limit': max(12, min(30, len(item_ids)))})
            selected = found['chunks']
            if not selected:
                result = {'claims': [], 'insufficient': '所选材料中没有找到与问题匹配的片段，请调整问题、文献范围或先索引全文。',
                          'coverage': found['coverage'], 'evidenceCoverage': found['evidenceCoverage'], 'droppedClaims': 0}
            else:
                context = {'question': question, 'history': [dict(value) for value in history[-6:] if value['content']][:6],
                           'sources': selected, 'evidenceCoverage': found['evidenceCoverage']}
                progress(.35, '正在询问已配置的阅读助手模型')
                raw = self.assistant.research_json(context)
                for item_id in item_ids:
                    self.documents.index(item_id)
                with self.library.db() as db:
                    current_ids = {value['id'] for value in db.execute('SELECT id FROM document_chunks WHERE id IN (' +
                        ','.join('?' for _ in selected) + ')', [value['id'] for value in selected])}
                known = {value['id']: value for value in selected if value['id'] in current_ids}
                claims, dropped = [], 0
                for claim in raw.get('claims', []) if isinstance(raw.get('claims'), list) else []:
                    if not isinstance(claim, dict) or not isinstance(claim.get('text'), str):
                        dropped += 1
                        continue
                    citations = []
                    for citation in claim.get('citations', []) if isinstance(claim.get('citations'), list) else []:
                        if not isinstance(citation, dict):
                            continue
                        chunk = known.get(citation.get('chunkId'))
                        quote = str(citation.get('quote') or '').strip()
                        if chunk and 8 <= len(quote) <= 900 and ' '.join(quote.split()) in ' '.join(chunk['quote'].split()):
                            citations.append({key: chunk[key] for key in ('id', 'itemId', 'title', 'attachmentId', 'sourceType',
                                                                           'sourceVersion', 'page', 'startOffset')} | {'quote': quote})
                    if citations and claim['text'].strip():
                        claims.append({'text': claim['text'].strip()[:1200], 'citations': citations[:5]})
                    else:
                        dropped += 1
                result = {'claims': claims[:15], 'insufficient': str(raw.get('insufficient') or '').strip()[:1200],
                          'coverage': found['coverage'], 'evidenceCoverage': found['evidenceCoverage'], 'droppedClaims': dropped,
                          'notice': '引用原句与来源归属已校验；结论含义仍需研究者核对。'}
                if not claims and not result['insufficient']:
                    result['insufficient'] = '模型没有提供可核对的原文引用，本次不生成结论。'
            content = '\n'.join(claim['text'] for claim in result['claims']) or result['insufficient']
            with self.library.db(True) as db:
                db.execute('''UPDATE research_messages SET content=?,status='completed',result_json=?,updated_at=? WHERE id=?''',
                           (content, dumps(result), now(), answer_id))
                db.execute('UPDATE research_conversations SET updated_at=? WHERE id=?', (now(), conversation['id']))
            progress(.98, '问答与引用已保存到本地')
            return {'answerId': answer_id, 'conversationId': conversation['id'], 'claims': len(result['claims'])}
        except Exception as exc:
            with self.library.db(True) as db:
                db.execute('''UPDATE research_messages SET status='failed',result_json=?,updated_at=? WHERE id=?''',
                           (dumps({'error': str(exc)[:500], 'sourceTypes': source_types}), now(), answer_id))
            raise
