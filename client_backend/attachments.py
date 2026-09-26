from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import shutil
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Highlight, Rectangle
from pypdf.generic import ArrayObject, FloatObject, NameObject, TextStringObject

from .common import AppError, dumps, now, require, sha256, uid, within


class Attachments:
    def __init__(self, library):
        self.library = library

    def add(self, item_id, path, mode='managed', role=None):
        path = Path(path).resolve()
        require(path.is_file(), '附件文件不存在')
        require(mode in ('managed', 'linked'), '附件保存方式不正确')
        require(path.stat().st_size <= 2 * 1024 ** 3, '附件超过2GB')
        digest = sha256(path)
        size = path.stat().st_size
        with path.open('rb') as stream:
            pdf_header = b'%PDF-' in stream.read(1024)
        mime = 'application/pdf' if pdf_header else (mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        if role is None:
            role = 'supplementary' if re.search(r'(supplement(?:ary|al)?|supporting|appendix|(?:^|[_ .-])si(?:[_ .-]|$))', path.stem, re.I) else ('main' if mime == 'application/pdf' else 'other')
        require(role in ('main', 'supplementary', 'other'), '附件类型不正确')
        target = path
        with self.library.db(True) as db:
            self.library._get(db, item_id)
            # The writer also protects the last-reference check and unlink in
            # permanent deletion. A concurrent add must copy after that unlink.
            if mode == 'managed':
                target = within(self.library.storage / digest[:2] / digest, self.library.storage)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.is_file() or sha256(target) != digest:
                    temporary = target.with_name(target.name + '.' + uid() + '.tmp')
                    try:
                        shutil.copyfile(path, temporary)
                        require(sha256(temporary) == digest, '复制校验失败，请重试')
                        os.replace(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
            same = db.execute('''SELECT a.id FROM attachments a JOIN objects o ON a.object_id=o.id WHERE a.item_id=? AND o.sha256=?''', (item_id, digest)).fetchone()
            if same:
                return {'id': same[0], 'duplicate': True}
            obj = db.execute('SELECT id FROM objects WHERE sha256=? AND mode=? AND path=?', (digest, mode, str(target))).fetchone()
            object_id = obj[0] if obj else uid()
            if not obj:
                db.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?)', (object_id, digest, size, str(target), mode, mime, now()))
            attachment_id = uid()
            db.execute('''INSERT INTO attachments(id,item_id,object_id,name,version,text_status,role,created_at) VALUES(?,?,?,?,?,?,?,?)''',
                       (attachment_id, item_id, object_id, path.name, digest, 'pending' if mime == 'application/pdf' else 'not_applicable', role, now()))
            self.library._index(db, item_id)
        return {'id': attachment_id, 'duplicate': False, 'mime': mime}

    def record(self, attachment_id):
        with self.library.db() as db:
            row = db.execute('''SELECT a.*,o.path,o.sha256,o.mime,o.bytes,o.mode FROM attachments a JOIN objects o ON o.id=a.object_id WHERE a.id=?''', (attachment_id,)).fetchone()
            if not row:
                raise AppError('NOT_FOUND', '附件不存在')
            return dict(row)

    def path(self, attachment_id, verify=False):
        record = self.record(attachment_id)
        path = Path(record['path']).resolve()
        if record['mode'] == 'managed':
            within(path, self.library.storage)
        if not path.is_file():
            raise AppError('ATTACHMENT_MISSING', '附件文件丢失，请使用“重新定位”选择文件')
        if verify and sha256(path) != record['sha256']:
            raise AppError('ATTACHMENT_CHANGED', '附件已在外部修改，请重新定位以创建新的附件版本')
        return path

    def relink(self, attachment_id, path):
        old = self.record(attachment_id)
        path = Path(path).resolve()
        require(path.is_file(), '文件不存在')
        digest = sha256(path)
        if digest != old['sha256']:
            added = self.add(old['item_id'], path, old['mode'], old['role'])
            return {**added, 'newVersion': True, 'message': '已作为新附件版本添加，旧批注保留在原版本'}
        if old['mode'] == 'managed':
            with self.library.writer:
                self.record(attachment_id)
                destination = within(Path(old['path']), self.library.storage)
                destination.parent.mkdir(parents=True, exist_ok=True)
                temp = destination.with_name(destination.name + '.' + uid() + '.tmp')
                try:
                    shutil.copyfile(path, temp)
                    require(sha256(temp) == digest, '附件校验失败')
                    os.replace(temp, destination)
                finally:
                    temp.unlink(missing_ok=True)
        else:
            with self.library.db(True) as db:
                db.execute('UPDATE objects SET path=? WHERE id=?', (str(path), old['object_id']))
        return {'id': attachment_id, 'newVersion': False}

    def set_role(self, attachment_id, role):
        require(role in ('main', 'supplementary', 'other'), '附件类型不正确')
        with self.library.db(True) as db:
            row = db.execute('SELECT item_id FROM attachments WHERE id=?', (attachment_id,)).fetchone()
            require(row, '附件不存在')
            db.execute('UPDATE attachments SET role=? WHERE id=?', (role, attachment_id))
        return {'id': attachment_id, 'role': role}

    def convert_caj(self, attachment_id, progress=lambda *args: None, jobs=None):
        """Normalize KDH-backed CAJ into a readable PDF, retaining the original."""
        record = self.record(attachment_id)
        require(Path(record['name']).suffix.casefold() == '.caj', '请选择 CAJ 原件')
        source = self.path(attachment_id, verify=True)
        require(source.stat().st_size <= 512 * 1024 * 1024, 'CAJ 超过 512 MB，暂不能自动转换')
        with source.open('rb') as stream:
            signature = stream.read(254)
        require(signature.startswith(b'KDH '), '此 CAJ 的内部格式暂不支持自动转换；原件仍已保存，可用 CAJ 阅读器打开')
        key = b'FZHMEI'
        download_dir = self.library.root / 'downloads'
        download_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='caj-', dir=download_dir) as temporary:
            work = Path(temporary)
            decoded = work / 'decoded.pdf'
            progress(.05, '正在解码 KDH 全文')
            with source.open('rb') as input_file, decoded.open('wb') as output_file:
                input_file.seek(254)
                offset = 0
                while block := input_file.read(1024 * 1024):
                    output_file.write(bytes(value ^ key[(offset + index) % len(key)] for index, value in enumerate(block)))
                    offset += len(block)
                    progress(.05 + .35 * input_file.tell() / source.stat().st_size, '正在解码 KDH 全文')
            with decoded.open('r+b') as output_file:
                require(output_file.read(8).startswith(b'%PDF-'), 'CAJ 内容不能还原为 PDF，原件仍已保存')
                size = output_file.seek(0, os.SEEK_END)
                output_file.seek(max(0, size - 1024 * 1024))
                tail_offset = output_file.tell()
                eof = output_file.read().rfind(b'%%EOF')
                require(eof >= 0, 'CAJ 中没有完整的 PDF 结束标记，原件仍已保存')
                output_file.truncate(tail_offset + eof + 5)
            progress(.45, '正在修复 PDF 页面结构')
            try:
                reader = PdfReader(decoded, strict=False)
                page_count = len(reader.pages)
                require(page_count > 0, 'CAJ 中没有可阅读的页面')
                writer = PdfWriter()
                writer.append_pages_from_reader(reader)
                converted = work / (Path(record['name']).stem + '.pdf')
                with converted.open('wb') as output_file:
                    writer.write(output_file)
                verified = PdfReader(converted, strict=True)
                require(len(verified.pages) == page_count, 'PDF 页面校验失败，CAJ 原件仍已保存')
            except AppError:
                raise
            except Exception as exc:
                raise AppError('CAJ_CONVERSION_FAILED', 'CAJ 转换失败，原件仍已保存；可使用 CAJ 阅读器打开') from exc
            progress(.85, '正在将 PDF 加入文献库')
            added = self.add(record['item_id'], converted, mode='managed', role='main')
        if jobs:
            current = self.record(added['id'])
            if current['text_status'] in ('pending', 'error'):
                jobs.create('pdf.index', {'attachmentId': added['id']})
        progress(.99, 'PDF 已保存，正在建立全文索引')
        return {'attachmentId': added['id'], 'pageCount': page_count, 'duplicate': added['duplicate']}

    def index(self, attachment_id, progress=lambda *args: None):
        record = self.record(attachment_id)
        require(record['mime'] == 'application/pdf', '该附件不是PDF')
        try:
            path = self.path(attachment_id, True)
            reader = PdfReader(path)
            if reader.is_encrypted and not reader.decrypt(''):
                raise AppError('PDF_PROTECTED', 'PDF需要密码；可在阅读器中解锁阅读，自动全文索引暂不可用')
            texts, total = [], len(reader.pages)
            for index, page in enumerate(reader.pages):
                progress(index / max(total, 1), f'索引PDF第 {index + 1}/{total} 页')
                try:
                    texts.append((page.extract_text() or '')[:500000])
                except Exception:
                    texts.append('')
            status = 'indexed' if any(x.strip() for x in texts) else 'no_text'
            with self.library.db(True) as db:
                db.execute('UPDATE attachments SET text_status=?,page_count=?,text_json=? WHERE id=?', (status, total, dumps(texts), attachment_id))
                self.library._index(db, record['item_id'])
            return {'pageCount': total, 'textStatus': status}
        except Exception as exc:
            with self.library.db(True) as db:
                db.execute('UPDATE attachments SET text_status=? WHERE id=?', ('protected' if isinstance(exc, AppError) and exc.code == 'PDF_PROTECTED' else 'error', attachment_id))
            if isinstance(exc, AppError):
                raise
            raise AppError('PDF_INVALID', 'PDF解析失败：' + str(exc)[:200])

    def position(self, attachment_id, data):
        page = int(data.get('page', 1))
        require(1 <= page <= 100000, '页码不正确')
        clean = {'page': page, 'scale': max(.25, min(4, float(data.get('scale', 1)))), 'rotation': int(data.get('rotation', 0)) % 360}
        with self.library.db(True) as db:
            db.execute('UPDATE attachments SET position_json=? WHERE id=?', (dumps(clean), attachment_id))
        return clean

    def list_annotations(self, attachment_id):
        record = self.record(attachment_id)
        with self.library.db() as db:
            return [{**json.loads(row['data']), 'id': row['id'], 'revision': row['revision'], 'version': row['version'], 'stale': row['version'] != record['version']}
                    for row in db.execute('SELECT * FROM annotations WHERE attachment_id=? ORDER BY created_at', (attachment_id,))]

    def save_annotation(self, attachment_id, data):
        record = self.record(attachment_id)
        require(data.get('version') == record['version'], '附件版本已改变，请重新打开文件')
        require(data.get('type') in ('highlight', 'underline', 'area'), '批注类型不支持')
        page_index = int(data.get('pageIndex', -1))
        require(0 <= page_index < (record.get('page_count') or 100000), '批注页码不正确')
        rects = data.get('rects', [])
        require(isinstance(rects, list) and 0 < len(rects) <= 1000, '批注区域为空或过多')
        for rect in rects:
            require(isinstance(rect, list) and len(rect) == 4 and all(isinstance(n, (int, float)) and math.isfinite(n) and abs(n) < 1000000 for n in rect), '批注坐标不正确')
        require(bool(re.fullmatch(r'#[0-9a-fA-F]{6}', str(data.get('color', '')))), '批注颜色不正确')
        clean = {key: data.get(key) for key in ('type', 'pageIndex', 'pageLabel', 'rects', 'quote', 'prefix', 'suffix', 'comment', 'color', 'pageBox', 'rotation')}
        require(len(str(clean.get('comment') or '')) <= 200000 and len(str(clean.get('quote') or '')) <= 1000000, '批注文字过长')
        annotation_id = data.get('id') or uid()
        with self.library.db(True) as db:
            old = db.execute('SELECT * FROM annotations WHERE id=?', (annotation_id,)).fetchone()
            if old:
                require(old['attachment_id'] == attachment_id, '批注不属于该文件')
                if data.get('revision') != old['revision']:
                    raise AppError('REVISION_CONFLICT', '批注已更新，请重新载入')
                revision = old['revision'] + 1
                db.execute('UPDATE annotations SET data=?,revision=?,updated_at=? WHERE id=?', (dumps(clean), revision, now(), annotation_id))
            else:
                revision = 1
                db.execute('INSERT INTO annotations VALUES(?,?,?,?,?,?,?)', (annotation_id, attachment_id, record['version'], dumps(clean), revision, now(), now()))
        return {**clean, 'id': annotation_id, 'revision': revision, 'version': record['version']}

    def delete_annotation(self, annotation_id, revision):
        with self.library.db(True) as db:
            row = db.execute('SELECT revision FROM annotations WHERE id=?', (annotation_id,)).fetchone()
            require(row, '批注不存在')
            if row[0] != revision:
                raise AppError('REVISION_CONFLICT', '批注已更新，请重新载入')
            db.execute('DELETE FROM annotations WHERE id=?', (annotation_id,))
        return {'ok': True}

    def excerpt_note(self, annotation_id, note_id=None):
        with self.library.db() as db:
            row = db.execute('SELECT * FROM annotations WHERE id=?', (annotation_id,)).fetchone()
            require(row, '批注不存在')
            annotation = json.loads(row['data'])
            attachment = db.execute('SELECT item_id,name FROM attachments WHERE id=?', (row['attachment_id'],)).fetchone()
            note = db.execute('SELECT * FROM notes WHERE id=?', (note_id,)).fetchone() if note_id else None
        page = annotation['pageIndex'] + 1
        quote = str(annotation.get('quote') or '区域批注').replace('\n', '\n> ')
        excerpt = f'\n\n> {quote}\n\n[第 {page} 页 · 返回原文](research://attachment/{row["attachment_id"]}?page={page}&annotation={annotation_id})'
        if annotation.get('comment'):
            excerpt += '\n\n' + annotation['comment']
        return self.library.note_save({'id': note_id, 'itemId': attachment['item_id'], 'title': note['title'] if note else '阅读摘录',
                                       'content': (note['content'] if note else '') + excerpt, 'revision': note['revision'] if note else None})

    def export(self, attachment_id, destination):
        source = self.path(attachment_id, True)
        destination = Path(destination).resolve()
        require(destination != source and not destination.is_relative_to(self.library.storage), '请选择独立的导出文件，不能覆盖受管理原件')
        reader = PdfReader(source)
        if reader.is_encrypted and not reader.decrypt(''):
            raise AppError('PDF_PROTECTED', '请先使用解密副本导出批注')
        writer = PdfWriter(clone_from=reader)
        for annotation in self.list_annotations(attachment_id):
            if annotation['stale']:
                continue
            rects = annotation['rects']
            boxes = [[min(r[0], r[2]), min(r[1], r[3]), max(r[0], r[2]), max(r[1], r[3])] for r in rects]
            bounds = (min(r[0] for r in boxes), min(r[1] for r in boxes), max(r[2] for r in boxes), max(r[3] for r in boxes))
            color = annotation['color'].lstrip('#')
            if annotation['type'] == 'area':
                obj = Rectangle(bounds)
                obj[NameObject('/C')] = ArrayObject([FloatObject(int(color[i:i + 2], 16) / 255) for i in (0, 2, 4)])
            else:
                points = []
                for x0, y0, x1, y1 in boxes:
                    points.extend([x0, y1, x1, y1, x0, y0, x1, y0])
                obj = Highlight(rect=bounds, quad_points=ArrayObject([FloatObject(n) for n in points]), highlight_color=color, printing=True)
                if annotation['type'] == 'underline':
                    obj[NameObject('/Subtype')] = NameObject('/Underline')
            obj[NameObject('/Contents')] = TextStringObject(str(annotation.get('comment') or annotation.get('quote') or ''))
            obj[NameObject('/NM')] = TextStringObject(annotation['id'])
            writer.add_annotation(page_number=annotation['pageIndex'], annotation=obj)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name(destination.name + '.' + uid() + '.tmp')
        try:
            with temp.open('wb') as stream:
                writer.write(stream)
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        return {'path': str(destination), 'annotations': len(self.list_annotations(attachment_id))}
