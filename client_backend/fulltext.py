from __future__ import annotations

"""Resolve public full-text links and attach verified PDFs to a local item."""

from html.parser import HTMLParser
import json
import re
from urllib import parse

from .common import AppError, doi, now, require, uid
from .netsafe import open_url, public_url


class PdfLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.refresh = ''
        self._anchor = None
        self._anchor_text = []

    def handle_starttag(self, tag, attrs):
        data = {key.lower(): value for key, value in attrs if key and value}
        if tag == 'meta' and data.get('name', '').lower() == 'citation_pdf_url':
            self.links.append(data.get('content', ''))
        elif tag == 'link' and ('application/pdf' in data.get('type', '').lower() or
                                 'pdf' in data.get('rel', '').lower()):
            self.links.append(data.get('href', ''))
        elif tag == 'meta' and data.get('http-equiv', '').lower() == 'refresh':
            match = re.search(r'\burl\s*=\s*[\'"]?([^\'";]+)', data.get('content', ''), re.I)
            if match:
                self.refresh = match.group(1).strip()
        elif tag == 'a' and data.get('href'):
            self._anchor, self._anchor_text = data, []

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor_text.append(data)

    def handle_endtag(self, tag):
        if tag != 'a' or self._anchor is None:
            return
        data, label = self._anchor, ' '.join(self._anchor_text)
        label += ' ' + ' '.join(data.get(key, '') for key in ('href', 'title', 'aria-label', 'download'))
        label = label.lower()
        if re.search(r'(?:\.pdf(?:[?#]|$)|/pdf(?:/|\?|$)|/download(?:/|\?|$)|pdf\s*下载|下载\s*pdf|全文下载|download\s+pdf)', label) and not re.search(r'supplement|supporting|appendix|补充材料|参考文献', label):
            self.links.append(data['href'])
        self._anchor, self._anchor_text = None, []


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

    def discover(self, item_id):
        """Record OA copies found by DOI/PMID without assuming they are PDFs."""
        require(self.library.get_settings().get('online', True), '联网已关闭')
        item = self.library.get(item_id)
        identifier = doi(item.get('DOI')) if item.get('DOI') else ''
        pmid = re.sub(r'\D', '', str(item.get('PMID') or ''))
        require(identifier or pmid, '该文献没有 DOI 或 PMID；请手动添加 PDF 来源')
        key = 'https://doi.org/' + identifier if identifier else 'pmid:' + pmid
        found, errors = [], []

        def fetch(url):
            with open_url(url, accept='application/json', timeout=15) as response:
                return json.loads(response.read(4 * 1024 * 1024).decode('utf-8'))

        try:
            work = fetch('https://api.openalex.org/works/' + parse.quote(key, safe=':/'))
            locations = [work.get('best_oa_location') or {}] + list(work.get('locations') or [])
            for location in locations:
                if location.get('is_oa') or location == work.get('best_oa_location'):
                    found.append(location.get('pdf_url') or location.get('landing_page_url'))
            found.append((work.get('open_access') or {}).get('oa_url'))
        except (AppError, ValueError) as exc:
            errors.append('OpenAlex：' + str(exc))
        contact = str(self.library.get_settings().get('contactEmail') or '').strip()
        if identifier and re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', contact):
            try:
                url = 'https://api.unpaywall.org/v2/' + parse.quote(identifier, safe='/') + '?' + parse.urlencode({'email': contact})
                work = fetch(url)
                for location in [work.get('best_oa_location') or {}] + list(work.get('oa_locations') or []):
                    found.append(location.get('url_for_pdf') or location.get('url_for_landing_page'))
            except (AppError, ValueError) as exc:
                errors.append('Unpaywall：' + str(exc))
        added = 0
        with self.library.db(True) as db:
            for candidate in dict.fromkeys(link for link in found if link):
                try:
                    url = public_url(candidate)
                except AppError:
                    continue
                added += db.execute('''INSERT INTO fulltext_sources(id,item_id,url,origin,created_at,updated_at)
                    VALUES(?,?,?,'oa',?,?) ON CONFLICT(item_id,url) DO NOTHING''',
                    (uid(), item_id, url, now(), now())).rowcount
        return {'added': added, 'errors': errors, 'sources': self.sources(item_id)}

    def submit(self, payload):
        item_id = payload.get('itemId')
        sources = self.sources(item_id)
        source_id = payload.get('sourceId')
        require(not source_id or any(row['id'] == source_id for row in sources), '全文来源不存在或不属于此文献')
        return self.jobs.create('fulltext.obtain', {'itemId': item_id, **({'sourceId': source_id} if source_id else {})})

    def _source(self, payload):
        with self.library.db() as db:
            row = db.execute('SELECT * FROM fulltext_sources WHERE id=? AND item_id=?',
                             (payload.get('sourceId'), payload.get('itemId'))).fetchone()
        require(row, '全文来源不存在或不属于此文献')
        return dict(row)

    def _resolve(self, source, progress, depth=0):
        url = public_url(source['url'])
        progress(.05, '正在检查公开全文链接')
        with open_url(url, timeout=25) as response:
            final_url = public_url(response.geturl())
            raw = response.read(2 * 1024 * 1024)
            content_type = response.headers.get('Content-Type', '').lower()
        if b'%PDF-' in raw[:1024]:
            return final_url, 'pdf'
        if 'html' not in content_type and not raw.lstrip().lower().startswith((b'<!doctype html', b'<html')):
            return '', 'unknown'
        parser = PdfLinks()
        parser.feed(raw.decode('utf-8', errors='replace'))
        if not parser.links and parser.refresh and depth < 2:
            return self._resolve({'url': public_url(parse.urljoin(final_url, parser.refresh))}, progress, depth + 1)
        item_doi = ''
        if source.get('item_id'):
            item = self.library.get(source['item_id'])
            item_doi = doi(item.get('DOI')) if item.get('DOI') else ''
        for link in parser.links[:8]:
            try:
                candidate = public_url(parse.urljoin(final_url, link))
                decoded = parse.unquote(candidate).lower()
                embedded = re.search(r'10\.\d{4,9}/[^?#]+', decoded)
                if item_doi and embedded and item_doi.lower() not in decoded:
                    continue
                return candidate, 'pdf'
            except AppError:
                continue
        return final_url, 'landing'

    def obtain(self, payload, progress):
        require(self.library.get_settings().get('online', True), '联网已关闭')
        if not payload.get('sourceId'):
            item_id = payload.get('itemId')
            sources = self.sources(item_id)
            if not any(row['origin'] == 'oa' for row in sources):
                try:
                    self.discover(item_id)
                except AppError:
                    pass
                sources = self.sources(item_id)
            sources = [row for row in sources if row['origin'] != 'record']
            require(sources, '没有可用的全文来源；请添加 PDF 地址或用浏览器下载')
            failures = []
            for index, candidate in enumerate(sources):
                try:
                    return self.obtain({'itemId': item_id, 'sourceId': candidate['id']},
                                       lambda amount, message: progress((index + amount) / len(sources), message))
                except AppError as exc:
                    failures.append(str(exc))
                except Exception as exc:
                    failures.append(f'{parse.urlsplit(candidate["url"]).hostname}：{type(exc).__name__}')
            raise AppError('FULLTEXT_UNAVAILABLE', '所有全文来源均未取得 PDF：' + '；'.join(failures[:3]))
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
