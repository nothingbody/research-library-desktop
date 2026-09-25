"""Store a user-reviewed browser citation in the local library."""
from __future__ import annotations

import re
import json
import secrets
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .biblio import author_name, normalize, year_of
from .common import AppError, doi, dumps, norm, now, require, uid
from .downloads import validate_pdf
from .netsafe import public_url


_tickets = {}
_ticket_lock = threading.Lock()
_ticket_ttl = 2 * 60 * 60


def _issue_ticket(item_id, page_url, pdf_url):
    ticket = secrets.token_hex(16)
    with _ticket_lock:
        cutoff = time.time() - _ticket_ttl
        for key, value in list(_tickets.items()):
            if value['created'] < cutoff:
                _tickets.pop(key, None)
        _tickets[ticket] = {'itemId': item_id, 'pageUrl': page_url, 'pdfUrl': pdf_url, 'created': time.time()}
    return ticket


def _ticket_file(value, ticket, saved):
    path = Path(str(value or '')).expanduser().resolve()
    require(path.is_file() and path.suffix.lower() in ('.pdf', '.caj'), '浏览器下载文件不存在或格式不正确')
    require(ticket[:12] in path.name and path.stat().st_mtime >= saved['created'] - 120,
            '下载文件与本次保存不匹配，请重新下载')
    return path


def cnki_host(host):
    host = str(host or '').lower()
    return bool(re.search(r'(^|\.)cnki\.(net|com\.cn)$', host) or
                re.search(r'(^|[.-])cnki[.-](net|com[.-]cn)([.-]|$)', host))


def _arxiv_id(url):
    parts = urlsplit(url)
    if (parts.hostname or '').lower() not in ('arxiv.org', 'www.arxiv.org'):
        return ''
    match = re.fullmatch(r'/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?/?', parts.path, re.I)
    return match.group(1).lower() if match else ''


def _same_url(left, right):
    a, b = urlsplit(left), urlsplit(right)
    return bool(a.hostname and b.hostname and a.hostname.lower().removeprefix('www.') ==
                b.hostname.lower().removeprefix('www.') and a.path.rstrip('/') == b.path.rstrip('/') and a.query == b.query)


def _title_key(value):
    return re.sub(r'[^\w]+', '', norm(value))


def _first_author(data):
    authors = data.get('author') or []
    if not authors:
        return ''
    first = authors[0]
    return norm(first.get('family') or first.get('literal') or '') if isinstance(first, dict) else ''


def _match(db, data, page_url):
    identifier = doi(data.get('DOI')) if data.get('DOI') else ''
    if identifier:
        row = db.execute('SELECT id,title,year,doi,data FROM items WHERE doi=? AND deleted_at IS NULL LIMIT 1', (identifier,)).fetchone()
        if row:
            return row, 'DOI'
    year, title_key, arxiv = year_of(data), _title_key(data['title']), _arxiv_id(page_url)
    rows = db.execute('SELECT id,title,year,doi,data FROM items WHERE deleted_at IS NULL AND (title_norm=? OR year=? OR json_extract(data,\'$.URL\')=? OR (?<>\'\' AND json_extract(data,\'$.URL\') LIKE ?))',
                      (norm(data['title']), year or '0000', page_url, arxiv, f'%{arxiv}%')).fetchall()
    for row in rows:
        existing = json.loads(row['data'])
        old_url = str(existing.get('URL') or '')
        if identifier and row['doi'] and identifier != row['doi']:
            continue
        if arxiv and _arxiv_id(old_url) == arxiv:
            return row, 'arXiv ID'
        if _same_url(page_url, old_url):
            return row, '来源网址'
        if year and row['year'] == year and title_key == _title_key(row['title']) and _first_author(data) and _first_author(data) == _first_author(existing):
            return row, '题名、年份与首位作者'
    return None, ''


def check_duplicate(library, payload):
    require(isinstance(payload, dict) and isinstance(payload.get('data'), dict), '浏览器题录格式不正确')
    page_url = str(payload.get('pageUrl') or '').strip()
    parts = urlsplit(page_url)
    require(parts.scheme in ('http', 'https') and parts.hostname, '网页来源地址不正确')
    raw = payload['data']
    authors = raw.get('author') or []
    if isinstance(authors, list):
        authors = [author_name(value) if isinstance(value, str) else value for value in authors]
    data = normalize({'title': raw.get('title'), 'author': authors,
                      'year': raw.get('year') or '', 'DOI': raw.get('DOI') or ''})
    with library.db() as db:
        row, basis = _match(db, data, page_url)
    return {'duplicate': {'itemId': row['id'], 'title': row['title'], 'year': row['year'], 'basis': basis} if row else None}


def capture(library, fulltext, payload):
    require(isinstance(payload, dict), '浏览器题录格式不正确')
    page_url = str(payload.get('pageUrl') or '').strip()
    parts = urlsplit(page_url)
    require(len(page_url) <= 8000 and parts.scheme in ('http', 'https') and parts.hostname and
            not parts.username and not parts.password, '网页来源地址不正确')
    raw = payload.get('data')
    require(isinstance(raw, dict), '浏览器题录格式不正确')
    allowed = {'type', 'title', 'author', 'year', 'issued', 'DOI', 'URL', 'container-title',
               'abstract', 'publisher', 'volume', 'issue', 'page', 'ISSN', 'PMID', 'tags'}
    cleaned = {key: value for key, value in raw.items() if key in allowed}
    if isinstance(cleaned.get('author'), list):
        cleaned['author'] = [author_name(value) if isinstance(value, str) else value for value in cleaned['author']]
    data = normalize(cleaned)
    require(not data.get('DOI') or bool(re.fullmatch(r'10\.\d{4,9}/\S+', data['DOI'], re.I)), 'DOI 格式不正确，请核对后保存')
    require(len(data.get('author', [])) <= 200 and len(str(data.get('abstract') or '')) <= 100000,
            '浏览器题录内容过长')
    data['URL'] = page_url
    collection_id = payload.get('collectionId') or None
    pdf_url = str(payload.get('pdfUrl') or '').strip()
    browser_download = bool(payload.get('browserDownload') and payload.get('downloadPdf'))
    browser_pdf_url = str(payload.get('browserDownloadUrl') or pdf_url).strip()
    if browser_download and browser_pdf_url:
        browser_parts = urlsplit(browser_pdf_url)
        require(len(browser_pdf_url) <= 8000 and browser_parts.scheme in ('http', 'https') and browser_parts.hostname and
                not browser_parts.username and not browser_parts.password, '浏览器 PDF 地址不正确')
    pdf_warning = ''
    if pdf_url:
        try:
            pdf_url = public_url(pdf_url)
        except AppError as exc:
            if not browser_download:
                pdf_warning = f'PDF 地址未保存：{exc}'
            pdf_url = ''
    online = bool(library.get_settings().get('online', True))
    download = bool(pdf_url and payload.get('downloadPdf') and online and not browser_download)
    with library.db(True) as db:
        if collection_id:
            require(db.execute('SELECT 1 FROM collections WHERE id=?', (collection_id,)).fetchone(), '集合不存在')
        existing, duplicate_basis = _match(db, data, page_url)
        item_id = existing['id'] if existing else library._create(db, data, collection_id, 'browser capture')
        if existing and collection_id:
            changed = db.execute('INSERT OR IGNORE INTO collection_items VALUES(?,?)', (collection_id, item_id)).rowcount
            if changed:
                db.execute('UPDATE items SET revision=revision+1,updated_at=? WHERE id=?', (now(), item_id))
        if existing:
            # Browser classification is additive: preserve a user's existing tags
            # while adding the deterministic source/type/venue tags extracted here.
            existing_item = library._get(db, item_id)
            prior_data = existing_item
            merged_tags = list(dict.fromkeys(prior_data.get('tags', []) + data.get('tags', [])))
            if merged_tags != prior_data.get('tags', []):
                library._update(db, item_id, {'tags': merged_tags}, existing_item['revision'])
            prior = db.execute('SELECT 1 FROM provenance WHERE item_id=? AND source=? AND data=? LIMIT 1',
                               (item_id, 'browser capture', dumps({'pageUrl': page_url}))).fetchone()
            if not prior:
                db.execute('INSERT INTO provenance VALUES(?,?,?,?,?)',
                           (uid(), item_id, 'browser capture', dumps({'pageUrl': page_url}), now()))
    source = fulltext.add({'itemId': item_id, 'url': pdf_url}) if pdf_url else None
    already_attached = bool(source and source.get('attachmentId'))
    job = fulltext.submit({'itemId': item_id, 'sourceId': source['id']}) if source and download and not already_attached else None
    ticket = _issue_ticket(item_id, page_url, browser_pdf_url) if browser_download and browser_pdf_url and not already_attached else None
    return {'itemId': item_id, 'created': not bool(existing), 'collectionId': collection_id,
            'pdfSourceId': source['id'] if source else None, 'downloadJobId': job['jobId'] if job else None,
            'pdfAlreadyAttached': already_attached, 'downloadSkippedOffline': bool(pdf_url and payload.get('downloadPdf') and not online),
            'duplicateBasis': duplicate_basis, 'pdfWarning': pdf_warning, 'downloadTicket': ticket}


def _authorized_cnki_url(value):
    url = str(value or '').strip()
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    require(len(url) <= 8000 and parts.scheme == 'https' and host and not parts.username and not parts.password,
            '知网下载地址不正确')
    require(cnki_host(host), '只接受知网（含校园代理）的授权下载地址')
    return url


_PUBLISHER_HOSTS = ('link.springer.com', 'onlinelibrary.wiley.com', 'tandfonline.com',
                    'journals.sagepub.com', 'pubs.acs.org', 'dl.acm.org', 'mdpi.com',
                    'academic.oup.com', 'ieeexplore.ieee.org', 'sciencedirect.com')


def _authorized_publisher_url(source, download):
    page, pdf = public_url(source), public_url(download)
    a, b = urlsplit(page), urlsplit(pdf)
    require(a.scheme == b.scheme == 'https', '期刊下载地址必须使用 HTTPS')
    source_host, pdf_host = a.hostname.lower(), b.hostname.lower()
    require(any(source_host == host or source_host.endswith('.' + host) for host in _PUBLISHER_HOSTS),
            '只接受已适配期刊站点的浏览器 PDF 下载')
    require(source_host == pdf_host or source_host.endswith('.' + pdf_host) or pdf_host.endswith('.' + source_host),
            'PDF 下载地址与当前期刊站点不匹配')
    return page, pdf


def _browser_download_path(value, suffix):
    path = Path(str(value or '')).expanduser().resolve()
    # The extension receives this absolute filename from Chrome downloads API.
    # Confining imports to the current profile's standard download locations
    # prevents the loopback endpoint from becoming an arbitrary-file importer.
    home = Path.home().resolve()
    downloads = [home / 'Downloads', home / '下载']
    require(any(path.is_relative_to(root.resolve()) for root in downloads if root.exists()),
            '只能导入当前用户“下载”目录中的浏览器下载文件')
    require(path.is_file(), '浏览器下载文件不存在或尚未完成')
    require(path.suffix.lower() == '.' + suffix, f'下载文件不是 .{suffix.upper()} 格式')
    return path


def import_downloaded(library, fulltext, payload):
    """Import a verified browser download without reading browser credentials."""
    require(isinstance(payload, dict), '浏览器下载信息不正确')
    item_id = str(payload.get('itemId') or '')
    ticket = str(payload.get('ticket') or '')
    if ticket:
        with _ticket_lock:
            saved = dict(_tickets.get(ticket) or {})
        require(saved and saved['itemId'] == item_id and saved['created'] >= time.time() - _ticket_ttl,
                '下载凭据已失效，请重新打开网页保存')
        path = _ticket_file(payload.get('path'), ticket, saved)
        source_url, download_url = saved['pageUrl'], saved['pdfUrl']
        publisher = False
    else:
        download_url = str(payload.get('downloadUrl') or '').strip()
        publisher = bool(download_url)
        source_url, download_url = (_authorized_publisher_url(payload.get('sourceUrl'), download_url)
                                    if publisher else (_authorized_cnki_url(payload.get('sourceUrl')), ''))
        fmt = str(payload.get('format') or '').lower()
        require(fmt == 'pdf' if publisher else fmt in ('pdf', 'caj'), '浏览器下载格式不正确')
        path = _browser_download_path(payload.get('path'), fmt)
    if publisher:
        with library.db() as db:
            require(db.execute('SELECT 1 FROM fulltext_sources WHERE item_id=? AND url=?',
                               (item_id, download_url)).fetchone(), '该文献没有对应的 PDF 来源')
    with path.open('rb') as stream:
        header = stream.read(1024).lstrip().lower()
    require(not header.startswith((b'<!doctype html', b'<html', b'<?xml')),
            '浏览器下载的是登录页或网页，并非全文文件')
    fmt = 'pdf' if b'%pdf-' in header[:1024] else 'caj'
    if fmt == 'pdf':
        validate_pdf(path)
    else:
        require(path.suffix.lower() == '.caj' and cnki_host(urlsplit(source_url).hostname),
                '下载文件不是 PDF，无法作为全文导入')
    temporary = None
    stored = path
    if fmt == 'pdf' and path.suffix.lower() != '.pdf':
        temporary = library.root / 'downloads' / (uid() + '.pdf')
        temporary.parent.mkdir(exist_ok=True)
        shutil.copyfile(path, temporary)
        stored = temporary
    try:
        added = fulltext.downloads.attachments.add(item_id, stored, mode='managed', role='main')
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    job = None
    if fmt == 'pdf' and not added.get('duplicate'):
        job = fulltext.jobs.create('pdf.index', {'attachmentId': added['id']})
    with library.db(True) as db:
        if not added.get('duplicate'):
            display_name = re.sub(r'-[0-9a-f]{12}(?=\.[^.]+$)', '', path.name) if ticket else path.name
            if fmt == 'pdf':
                display_name = re.sub(r'\.[^.]+$', '.pdf', display_name)
            db.execute('UPDATE attachments SET name=? WHERE id=?', (display_name[:240], added['id']))
        if (publisher or ticket) and download_url:
            db.execute('''UPDATE fulltext_sources SET state='attached',attachment_id=?,resolved_url=?,kind='pdf',error='',checked_at=?,updated_at=?
                WHERE item_id=? AND url=?''', (added['id'], download_url, now(), now(), item_id, download_url))
        db.execute('INSERT INTO provenance VALUES(?,?,?,?,?)',
                   (uid(), item_id, 'browser authorized download',
                   dumps({'sourceUrl': source_url, 'downloadUrl': download_url, 'format': fmt, 'fileName': path.name}), now()))
    if ticket:
        with _ticket_lock:
            _tickets.pop(ticket, None)
    return {'itemId': item_id, 'attachmentId': added['id'], 'duplicate': bool(added.get('duplicate')),
            'format': fmt, 'indexJobId': job['jobId'] if job else None,
            'message': ('PDF 已导入，正在解析并建立全文索引' if fmt == 'pdf' and job else
                        'PDF 已在文献库中' if fmt == 'pdf' else 'CAJ 原件已保存；当前版本暂不支持 CAJ 全文解析，请在知网导出 PDF 后再索引')}
