from __future__ import annotations

import os
from pathlib import Path
import urllib.parse

from pypdf import PdfReader

from .common import AppError, require, uid
from .netsafe import open_url, public_url


def validate_pdf(path):
    """Reject login pages and incomplete PDF files before attaching them."""
    path = Path(path)
    with path.open('rb') as stream:
        require(b'%PDF-' in stream.read(1024), '下载内容不是 PDF；可能是登录页或网站拦截页')
        stream.seek(max(0, path.stat().st_size - 4096))
        require(b'%%EOF' in stream.read(), 'PDF 下载未完成：缺少文件结束标记')
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted and not reader.decrypt(''):
            return
        require(len(reader.pages) > 0, 'PDF 文件没有可阅读页面')
    except AppError:
        raise
    except Exception as exc:
        raise AppError('PDF_INVALID', 'PDF 文件结构不完整或已损坏') from exc


class Downloads:
    def __init__(self, library, attachments, jobs):
        self.library, self.attachments, self.jobs = library, attachments, jobs

    def scratch_path(self):
        folder = self.library.root / 'downloads'
        folder.mkdir(exist_ok=True)
        return folder / (uid() + '.pdf')

    def attach_file(self, item_id, path, url):
        """Validate and attach a PDF that was already downloaded (internal use only:
        the path never comes from an RPC payload)."""
        path = Path(path)
        try:
            validate_pdf(path)
            result = self.attachments.add(item_id, path)
            name = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).name
            if not name.lower().endswith('.pdf'):
                name = '在线全文.pdf'
            if not result.get('duplicate'):
                with self.library.db(True) as db:
                    db.execute('UPDATE attachments SET name=? WHERE id=?', (name[:240], result['id']))
                self.jobs.create('pdf.index', {'attachmentId': result['id']})
            return {'attachmentId': result['id'], 'bytes': path.stat().st_size, 'source': url, 'duplicate': bool(result.get('duplicate'))}
        finally:
            path.unlink(missing_ok=True)

    def pdf(self, payload, progress):
        require(self.library.get_settings().get('online', True), '联网已关闭')
        self.library.get(payload['itemId'])
        # public_url rejects credentials, non-http(s) schemes and local/intranet
        # targets; the redirect target below is validated the same way.
        url = public_url(payload['url'])
        target = self.scratch_path()
        limit = 512 * 1024 * 1024
        fetched = lambda done, size: progress(min(.9, done / size) if size else .3, f'已下载 {done / 1024 ** 2:.1f} MB')
        try:
            with open_url(url, accept='application/pdf', timeout=30, referer=payload.get('referer') or '', browser=True,
                          max_bytes=limit, progress=fetched) as response, target.open('wb') as output:
                public_url(response.geturl())
                try:
                    length = int(response.headers.get('Content-Length') or 0)
                except ValueError:
                    length = 0
                require(length <= limit, '在线下载超过512MB，请通过浏览器保存后导入')
                total = 0
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    if total == 0:
                        require(b'%PDF-' in chunk[:1024], '该地址未返回PDF；若需要登录，请在浏览器获取后导入')
                    total += len(chunk)
                    require(total <= limit, '下载超过512MB限制')
                    output.write(chunk)
                    progress(min(.95, total / length) if length else .1, f'已下载 {total / 1024 ** 2:.1f} MB')
                output.flush()
                os.fsync(output.fileno())
            require(total > 0, '下载内容为空')
            if length and total != length:
                raise AppError('PDF_INCOMPLETE', f'PDF 下载未完成：只收到 {total}/{length} 字节', retryable=True)
            return self.attach_file(payload['itemId'], target, url)
        finally:
            target.unlink(missing_ok=True)
