from __future__ import annotations

"""Resolve public full-text links and attach verified PDFs to a local item."""

from html.parser import HTMLParser
import ipaddress
from urllib import error, parse, request

from .common import AppError, now, require, uid


def public_url(value):
    url = str(value or '').strip()
    parts = parse.urlsplit(url)
    require(len(url) <= 8000 and parts.scheme in ('https', 'http') and parts.hostname and
            not parts.username and not parts.password, '请输入公开的 http(s) 文献地址')
    try:
        require(ipaddress.ip_address(parts.hostname).is_global, '不允许访问本机或内网地址')
    except ValueError:
        require(parts.hostname.lower() not in ('localhost', 'localhost.localdomain'), '不允许访问本机地址')
    return url


class PdfLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        data = {key.lower(): value for key, value in attrs if key and value}
        if tag == 'meta' and data.get('name', '').lower() == 'citation_pdf_url':
            self.links.append(data.get('content', ''))
        elif tag == 'link' and ('application/pdf' in data.get('type', '').lower() or
                                 'pdf' in data.get('rel', '').lower()):
            self.links.append(data.get('href', ''))


class Fulltext:
    def __init__(self, library, downloads, jobs):
        self.library, self.downloads, self.jobs = library, downloads, jobs

    def sources(self, item_id):
        self.library.get(item_id)
        with self.library.db() as db:
            rows = db.execute('''SELECT s.*,a.text_status FROM fulltext_sources s LEFT JOIN attachments a ON a.id=s.attachment_id
                WHERE s.item_id=? ORDER BY CASE s.origin WHEN 'oa' THEN 0 WHEN 'manual' THEN 1 ELSE 2 END,s.created_at''', (item_id,)).fetchall()
        return [{'id': row['id'], 'itemId': row['item_id'], 'candidateId': row['candidate_id'], 'url': row['url'],
                 'resolvedUrl': row['resolved_url'], 'origin': row['origin'], 'kind': row['kind'],
                 'state': ('indexed' if row['text_status'] == 'indexed' else 'attached') if row['attachment_id'] else row['state'],
                 'attachmentId': row['attachment_id'], 'textStatus': row['text_status'], 'error': row['error'],
                 'checkedAt': row['checked_at']} for row in rows]

    def add(self, payload):
        item_id, url = payload.get('itemId'), public_url(payload.get('url'))
        self.library.get(item_id)
        with self.library.db(True) as db:
            db.execute('''INSERT INTO fulltext_sources(id,item_id,url,origin,created_at,updated_at)
                VALUES(?,?,?,'manual',?,?) ON CONFLICT(item_id,url) DO NOTHING''', (uid(), item_id, url, now(), now()))
        return next(row for row in self.sources(item_id) if row['url'] == url)

    def submit(self, payload):
        item_id = payload.get('itemId')
        sources = self.sources(item_id)
        source_id = payload.get('sourceId') or next((row['id'] for row in sources if row['origin'] in ('oa', 'manual')), None)
        require(source_id and any(row['id'] == source_id for row in sources), '没有可用的全文来源；请添加公开 PDF 地址或手动导入')
        return self.jobs.create('fulltext.obtain', {'itemId': item_id, 'sourceId': source_id})

    def _source(self, payload):
        with self.library.db() as db:
            row = db.execute('SELECT * FROM fulltext_sources WHERE id=? AND item_id=?',
                             (payload.get('sourceId'), payload.get('itemId'))).fetchone()
        require(row, '全文来源不存在或不属于此文献')
        return dict(row)

    def _resolve(self, source, progress):
        url = public_url(source['url'])
        progress(.05, '正在检查公开全文链接')
        try:
            req = request.Request(url, headers={'User-Agent': 'ResearchLibrary/0.7', 'Accept': 'application/pdf,text/html'})
            with request.urlopen(req, timeout=25) as response:
                final_url = public_url(response.geturl())
                raw = response.read(1024 * 1024)
                content_type = response.headers.get('Content-Type', '').lower()
        except (error.URLError, TimeoutError, OSError) as exc:
            raise AppError('FULLTEXT_NETWORK', '全文链接无法访问，请检查网络或在浏览器中手动取得 PDF', retryable=True) from exc
        if b'%PDF-' in raw[:1024]:
            return final_url, 'pdf'
        if 'html' not in content_type and not raw.lstrip().lower().startswith((b'<!doctype html', b'<html')):
            return '', 'unknown'
        parser = PdfLinks()
        parser.feed(raw.decode('utf-8', errors='replace'))
        for link in parser.links[:8]:
            try:
                return public_url(parse.urljoin(final_url, link)), 'pdf'
            except AppError:
                continue
        return final_url, 'landing'

    def obtain(self, payload, progress):
        require(self.library.get_settings().get('online', True), '联网已关闭')
        source = self._source(payload)
        if source['attachment_id']:
            with self.library.db() as db:
                if db.execute('SELECT 1 FROM attachments WHERE id=?', (source['attachment_id'],)).fetchone():
                    progress(.99, '全文已在文献库中')
                    return {'sourceId': source['id'], 'attachmentId': source['attachment_id'], 'duplicate': True}
        with self.library.db(True) as db:
            db.execute("UPDATE fulltext_sources SET state='checking',error='',updated_at=? WHERE id=?", (now(), source['id']))
        kind = ''
        try:
            resolved, kind = self._resolve(source, progress)
            with self.library.db(True) as db:
                db.execute('''UPDATE fulltext_sources SET resolved_url=?,kind=?,state=?,checked_at=?,updated_at=? WHERE id=?''',
                           (resolved, kind, 'ready' if kind == 'pdf' else 'landing' if kind == 'landing' else 'failed', now(), now(), source['id']))
            if kind != 'pdf':
                raise AppError('FULLTEXT_NOT_PDF', '该来源只有论文落地页或未返回 PDF，请在浏览器取得全文后手动导入')
            progress(.2, '已确认 PDF 地址，正在下载')
            with self.library.db(True) as db:
                db.execute("UPDATE fulltext_sources SET state='downloading',updated_at=? WHERE id=?", (now(), source['id']))
            result = self.downloads.pdf({'itemId': source['item_id'], 'url': resolved},
                                        lambda amount, message: progress(.2 + .75 * amount, message))
            with self.library.db(True) as db:
                db.execute("UPDATE fulltext_sources SET state='attached',attachment_id=?,error='',updated_at=? WHERE id=?",
                           (result['attachmentId'], now(), source['id']))
            progress(.99, 'PDF 已挂接，正在后台建立全文索引')
            return {'sourceId': source['id'], 'attachmentId': result['attachmentId'], 'duplicate': False}
        except Exception as exc:
            with self.library.db(True) as db:
                db.execute('UPDATE fulltext_sources SET state=?,error=?,updated_at=? WHERE id=?',
                           ('landing' if isinstance(exc, AppError) and exc.code == 'FULLTEXT_NOT_PDF' and kind == 'landing' else 'failed',
                            str(exc)[:500], now(), source['id']))
            raise
