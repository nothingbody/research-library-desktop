"""Local, explicitly human-labelled search quality diagnostics."""

from collections import Counter

from .common import norm, now, require


class SearchEvaluation:
    def __init__(self, library, ai_search):
        self.library, self.ai_search = library, ai_search

    def _scope(self, payload):
        session_id = payload.get('sessionId')
        require(isinstance(session_id, str), '缺少检索任务')
        session = self.ai_search.get(session_id)
        run_id = payload.get('runId') or session['activeRunId']
        require(run_id, '请先运行检索')
        values = self.ai_search.results({'sessionId': session_id, 'runId': run_id, 'limit': 20, 'sort': 'screening'})
        return run_id, values['items']

    def save(self, payload):
        run_id, top = self._scope(payload)
        candidate_id = payload.get('candidateId')
        require(candidate_id in {item['id'] for item in top}, '只可评估当前版本的 Top 20 候选')
        grade = payload.get('grade')
        require(type(grade) is int and 0 <= grade <= 3, '相关性等级应为 0 至 3')
        hard_match = payload.get('hardMatch', 'unknown')
        require(hard_match in ('pass', 'fail', 'unknown'), '硬条件判断不正确')
        note = str(payload.get('note') or '').strip()[:2000]
        with self.library.db(True) as db:
            db.execute('''INSERT INTO search_evaluations(run_id,candidate_id,grade,hard_match,note,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(run_id,candidate_id) DO UPDATE SET
                grade=excluded.grade,hard_match=excluded.hard_match,note=excluded.note,updated_at=excluded.updated_at''',
                       (run_id, candidate_id, grade, hard_match, note, now()))
        return self.report({'sessionId': payload['sessionId'], 'runId': run_id})

    def report(self, payload):
        run_id, top = self._scope(payload)
        with self.library.db() as db:
            labels = {row['candidate_id']: dict(row) for row in db.execute(
                'SELECT candidate_id,grade,hard_match,note,updated_at FROM search_evaluations WHERE run_id=?', (run_id,))}
            imported = [item for item in top if item.get('importedItemId')]
            pdf_items = set()
            if imported:
                ids = [item['importedItemId'] for item in imported]
                marks = ','.join('?' for _ in ids)
                pdf_items = {row[0] for row in db.execute(
                    f"SELECT DISTINCT a.item_id FROM attachments a JOIN objects o ON o.id=a.object_id WHERE a.item_id IN ({marks}) AND o.mime='application/pdf'", ids)}
        graded = [labels[item['id']] for item in top if item['id'] in labels]
        hard = [item for item in graded if item['hard_match'] != 'unknown']
        keys = [item.get('doi') or (norm(item['title']), item.get('year')) for item in top]
        sources = Counter(source for item in top for source in item.get('sources', []))
        return {
            'runId': run_id,
            'topK': len(top),
            'labelled': len(graded),
            'relevantAmongLabelled': round(sum(item['grade'] >= 2 for item in graded) / len(graded), 3) if graded else None,
            'hardPassAmongLabelled': round(sum(item['hard_match'] == 'pass' for item in hard) / len(hard), 3) if hard else None,
            'hardLabelled': len(hard),
            'duplicateCount': len(keys) - len(set(keys)),
            'sourceCoverage': dict(sources),
            'importedCount': len(imported),
            'pdfAttachedCount': sum(item['importedItemId'] in pdf_items for item in imported),
            'items': [{**item, 'evaluation': labels.get(item['id'])} for item in top],
        }
