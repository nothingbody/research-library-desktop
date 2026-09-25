from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfReader

from .biblio import author_name, normalize, parse_file, year_of
from .common import AppError, doi, dumps, norm, now, require, sha256, uid


class Imports:
    def __init__(self, library, attachments, jobs):
        self.library, self.attachments, self.jobs = library, attachments, jobs

    def preview(self, paths, item_id=None):
        require(isinstance(paths, list) and 0 < len(paths) <= 500, '一次最多选择500个文件')
        entries, errors, seen_in_batch = [], [], {}
        for file_index, source in enumerate(paths):
            path = Path(source).resolve()
            try:
                require(path.is_file(), '文件不存在')
                require(path.stat().st_size <= 2 * 1024 ** 3, '文件超过2GB')
                digest = sha256(path)
                if path.suffix.casefold() == '.pdf' or item_id:
                    data = {'title': path.stem, 'type': 'document', 'author': []}
                    warning = None
                    if path.suffix.casefold() == '.pdf':
                        try:
                            reader = PdfReader(path)
                            if reader.is_encrypted and not reader.decrypt(''):
                                warning = 'PDF需要密码，题录暂取文件名'
                            else:
                                metadata = reader.metadata or {}
                                title = str(metadata.get('/Title') or '').strip()
                                if title:
                                    data['title'] = title
                                if metadata.get('/Author'):
                                    data['author'] = [author_name(str(metadata['/Author']))]
                                warning = 'PDF内嵌元数据需核对，可导入后通过DOI补全'
                        except Exception:
                            warning = '无法读取PDF元数据，文件仍可保存，请检查文件是否损坏'
                    records = [normalize(data)]
                    attachment = True
                else:
                    records, attachment, warning = parse_file(path), False, None
                with self.library.db() as db:
                    for index, record in enumerate(records):
                        duplicate = None
                        if attachment:
                            duplicate = db.execute('''SELECT i.id,i.title FROM items i JOIN attachments a ON a.item_id=i.id JOIN objects o ON o.id=a.object_id
                              WHERE o.sha256=? AND i.deleted_at IS NULL LIMIT 1''', (digest,)).fetchone()
                        if not duplicate and record.get('DOI'):
                            duplicate = db.execute('SELECT id,title FROM items WHERE doi=? AND deleted_at IS NULL LIMIT 1', (doi(record['DOI']),)).fetchone()
                        if not duplicate and not attachment:
                            duplicate = db.execute('SELECT id,title FROM items WHERE title_norm=? AND year=? AND deleted_at IS NULL LIMIT 1', (norm(record['title']), year_of(record))).fetchone()
                        match_key = ('pdf:' + digest) if attachment else ('doi:' + doi(record['DOI']) if record.get('DOI') else 'title:' + norm(record['title']) + ':' + year_of(record))
                        if not duplicate and match_key in seen_in_batch:
                            duplicate = {'title': seen_in_batch[match_key]['title'], 'batchKey': seen_in_batch[match_key]['key']}
                        seen_in_batch.setdefault(match_key, {'key': f'{file_index}:{index}', 'title': record['title']})
                        entries.append({'key': f'{file_index}:{index}', 'path': str(path), 'fileName': path.name, 'digest': digest,
                                        'data': record, 'attachment': attachment, 'matchKey': match_key, 'warning': warning, 'duplicate': dict(duplicate) if duplicate else None})
            except Exception as exc:
                errors.append({'fileName': path.name, 'message': str(exc)[:300]})
        batch_id = uid()
        data = {'entries': entries, 'errors': errors, 'itemId': item_id}
        with self.library.db(True) as db:
            db.execute('INSERT INTO import_batches VALUES(?,?,?,?)', (batch_id, dumps(data), 'preview', now()))
        return {'batchId': batch_id, 'entries': [{k: v for k, v in e.items() if k not in ('path', 'digest')} for e in entries], 'errors': errors}

    def commit(self, payload, progress):
        with self.library.db() as db:
            row = db.execute('SELECT data FROM import_batches WHERE id=?', (payload.get('batchId'),)).fetchone()
            require(row, '导入预览已失效，请重新选择文件')
            batch = json.loads(row[0])
        keys = set(payload.get('keys', [entry['key'] for entry in batch['entries']]))
        entries = [entry for entry in batch['entries'] if entry['key'] in keys]
        mode = payload.get('mode') or self.library.get_settings().get('attachmentMode', 'managed')
        ids, errors, skipped, created = [], [], 0, set()
        matched = {}
        for index, entry in enumerate(entries):
            progress(index / max(len(entries), 1), f'导入 {index + 1}/{len(entries)}：{entry["fileName"]}')
            try:
                require(Path(entry['path']).is_file(), '源文件已移除')
                require(sha256(entry['path']) == entry['digest'], '文件在预览后已变化，请重新预览')
                with self.library.db(True) as db:
                    previous = db.execute('SELECT item_id FROM import_entries WHERE batch_id=? AND entry_key=?', (payload['batchId'], entry['key'])).fetchone()
                    if previous:
                        item_id = previous[0]
                        skipped += 1
                    elif batch.get('itemId'):
                        item_id = batch['itemId']
                        self.library._get(db, item_id)
                    elif entry.get('matchKey') and entry['matchKey'] in matched and not payload.get('keepDuplicates'):
                        item_id = matched[entry['matchKey']]
                        skipped += 1
                    elif entry['duplicate'] and not payload.get('keepDuplicates'):
                        if entry['duplicate'].get('id'):
                            item_id = entry['duplicate']['id']
                            skipped += 1
                        else:
                            item_id = self.library._create(db, entry['data'], payload.get('collectionId'), 'import:' + entry['fileName'])
                            created.add(item_id)
                    else:
                        item_id = self.library._create(db, entry['data'], payload.get('collectionId'), 'import:' + entry['fileName'])
                        created.add(item_id)
                    if entry.get('matchKey'):
                        matched[entry['matchKey']] = item_id
                    db.execute('INSERT OR IGNORE INTO import_entries VALUES(?,?,?)', (payload['batchId'], entry['key'], item_id))
                    if payload.get('collectionId'):
                        db.execute('INSERT OR IGNORE INTO collection_items VALUES(?,?)', (payload['collectionId'], item_id))
                if entry['attachment']:
                    attachment = self.attachments.add(item_id, entry['path'], mode)
                    if attachment.get('mime') == 'application/pdf' or not attachment.get('duplicate'):
                        record = self.attachments.record(attachment['id'])
                        if record['mime'] == 'application/pdf' and record['text_status'] in ('pending', 'error'):
                            self.jobs.create('pdf.index', {'attachmentId': attachment['id']})
                ids.append(item_id)
            except AppError as exc:
                if exc.code == 'CANCELLED':
                    raise
                errors.append({'fileName': entry['fileName'], 'message': str(exc)})
            except Exception as exc:
                errors.append({'fileName': entry['fileName'], 'message': str(exc)[:300]})
        with self.library.db(True) as db:
            db.execute('UPDATE import_batches SET state=? WHERE id=?', ('partial' if errors else 'completed', payload['batchId']))
        return {'itemIds': list(dict.fromkeys(ids)), 'imported': len(created), 'skipped': skipped, 'errors': errors}
