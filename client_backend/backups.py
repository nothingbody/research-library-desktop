from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from .common import AppError, atomic_json, now, require, sha256, uid, within


class Backups:
    def __init__(self, library):
        self.library = library

    def create(self, payload, progress):
        parent = Path(payload['directory']).resolve()
        require(parent.exists() and parent.is_dir(), '备份目录不存在')
        require(not parent.is_relative_to(self.library.storage), '不能备份到附件目录中')
        backup = parent / ('ResearchLibrary-' + now()[:19].replace(':', '') + '-' + uid()[:6])
        staging = parent / ('.' + backup.name + '-incomplete')
        require(staging.resolve().parent == parent and backup.resolve().parent == parent, '备份目标目录不安全')
        staging.mkdir()
        try:
            database = staging / 'library.sqlite3'
            with self.library.writer:
                with closing(sqlite3.connect(self.library.path)) as source, closing(sqlite3.connect(database)) as destination:
                    source.backup(destination)
            files, unavailable, warnings = [], [], []
            with closing(sqlite3.connect(database)) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA foreign_keys=ON')
                objects = [dict(row) for row in db.execute('SELECT * FROM objects')]
                for index, obj in enumerate(objects):
                    progress(index / max(1, len(objects)), f'备份附件 {index + 1}/{len(objects)}')
                    source = Path(obj['path']).resolve()
                    if not source.is_file():
                        unavailable.append({'objectId': obj['id'], 'sha256': obj['sha256'], 'bytes': obj['bytes'], 'reason': 'missing'})
                        warnings.append({'objectId': obj['id'], 'name': source.name, 'reason': '附件缺失，恢复后需要重新定位'})
                        continue
                    actual_hash = sha256(source)
                    actual_bytes = source.stat().st_size
                    relative = 'objects/' + actual_hash
                    target = within(staging / relative, staging)
                    target.parent.mkdir(exist_ok=True)
                    if not target.exists():
                        shutil.copyfile(source, target)
                    require(sha256(target) == actual_hash, '备份附件校验失败，请重试')
                    if actual_hash != obj['sha256']:
                        warnings.append({'objectId': obj['id'], 'name': source.name, 'reason': '附件已在外部修改；已保存当前文件，旧批注标为过期'})
                        db.execute('UPDATE objects SET sha256=?,bytes=? WHERE id=?', (actual_hash, actual_bytes, obj['id']))
                        db.execute("UPDATE attachments SET version=?,text_status='pending',text_json=NULL,page_count=NULL WHERE object_id=?", (actual_hash, obj['id']))
                        db.execute('DELETE FROM document_chunks WHERE attachment_id IN (SELECT id FROM attachments WHERE object_id=?)', (obj['id'],))
                    files.append({'objectId': obj['id'], 'path': relative, 'sha256': actual_hash, 'bytes': actual_bytes})
                db.commit()
            manifest = {'format': 'research-library-backup', 'version': 2, 'createdAt': now(),
                        'databaseSha256': sha256(database), 'objects': files, 'unavailableObjects': unavailable,
                        'warnings': warnings}
            atomic_json(staging / 'manifest.json', manifest)
            staging.rename(backup)
            return {'path': str(backup), 'objects': len(files), 'unavailable': len(unavailable),
                    'warnings': warnings, 'verified': True, 'complete': not warnings}
        except Exception:
            if staging.exists() and staging.resolve().parent == parent and staging.name.endswith('-incomplete'):
                shutil.rmtree(staging)
            raise

    def verify(self, backup):
        backup = Path(backup).resolve()
        try:
            require((backup / 'manifest.json').stat().st_size <= 64 * 1024 * 1024, '备份清单过大')
            manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise AppError('BACKUP_INVALID', '缺少有效的备份完成清单') from exc
        require(manifest.get('format') == 'research-library-backup' and manifest.get('version') in (1, 2), '不支持的备份格式')
        database = backup / 'library.sqlite3'
        require(sha256(database) == manifest['databaseSha256'], '备份数据库校验失败')
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', '备份数据库完整性检查失败')
            require(db.execute('PRAGMA user_version').fetchone()[0] in (1, 2), '备份数据库版本不受支持')
            counts = {name: db.execute(f'SELECT count(*) FROM {name}').fetchone()[0] for name in ('items', 'attachments', 'notes', 'annotations')}
            db_objects = {row[0]: row[1] for row in db.execute('SELECT id,sha256 FROM objects')}
        manifest_objects = {}
        for obj in manifest['objects']:
            path = within(backup / obj['path'], backup)
            require(path.is_file() and path.stat().st_size == obj['bytes'] and sha256(path) == obj['sha256'], '备份附件校验失败')
            require(obj['objectId'] not in manifest_objects, '备份包含重复对象')
            manifest_objects[obj['objectId']] = obj['sha256']
        unavailable = {obj['objectId']: obj['sha256'] for obj in manifest.get('unavailableObjects', [])}
        require(not (set(manifest_objects) & set(unavailable)) and db_objects == {**manifest_objects, **unavailable}, '备份附件清单与数据库不一致')
        return {'path': str(backup), 'manifest': manifest, 'counts': counts, 'verified': True,
                'complete': not unavailable and not manifest.get('warnings')}

    def restore(self, payload, progress):
        verified = self.verify(payload['backup'])
        parent = Path(payload['directory']).resolve()
        require(parent.is_dir(), '恢复目标目录不存在')
        target = parent / ('RestoredLibrary-' + uid()[:8])
        target.mkdir()
        shutil.copyfile(Path(verified['path']) / 'library.sqlite3', target / 'library.sqlite3')
        storage = target / 'storage'
        storage.mkdir()
        objects = verified['manifest']['objects']
        with closing(sqlite3.connect(target / 'library.sqlite3')) as db:
            for index, obj in enumerate(objects):
                progress(index / max(1, len(objects)), f'恢复附件 {index + 1}/{len(objects)}')
                source = within(Path(verified['path']) / obj['path'], verified['path'])
                destination = within(storage / obj['sha256'][:2] / obj['sha256'], storage)
                destination.parent.mkdir(exist_ok=True)
                if not destination.exists():
                    shutil.copyfile(source, destination)
                require(sha256(destination) == obj['sha256'], '恢复附件校验失败')
                db.execute("UPDATE objects SET path=?,mode='managed' WHERE id=?", (str(destination), obj['objectId']))
            for obj in verified['manifest'].get('unavailableObjects', []):
                destination = within(storage / obj['sha256'][:2] / obj['sha256'], storage)
                db.execute("UPDATE objects SET path=?,mode='managed' WHERE id=?", (str(destination), obj['objectId']))
            db.execute("UPDATE jobs SET state='cancelled',message='从备份恢复，待用户重新运行' WHERE state IN ('pending','running')")
            db.commit()
        atomic_json(target / 'restore-complete.json', {'source': verified['path'], 'restoredAt': now(), 'counts': verified['counts']})
        return {'path': str(target), 'counts': verified['counts'], 'unavailable': len(verified['manifest'].get('unavailableObjects', [])),
                'message': '已恢复为独立文献库；缺失附件需重新定位。原库未被覆盖'}
