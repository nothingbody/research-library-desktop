"""Store a user-reviewed browser citation in the local library."""
from __future__ import annotations

import re
import json
from pathlib import Path
from urllib.parse import urlsplit

from .biblio import author_name, normalize, year_of
from .common import doi, dumps, norm, now, require, uid, within
from .fulltext import public_url


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
    if pdf_url:
        pdf_url = public_url(pdf_url)
    download = bool(pdf_url and payload.get('downloadPdf') and library.get_settings().get('online', True))
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
    return {'itemId': item_id, 'created': not bool(existing), 'collectionId': collection_id,
            'pdfSourceId': source['id'] if source else None, 'downloadJobId': job['jobId'] if job else None,
            'pdfAlreadyAttached': already_attached, 'downloadSkippedOffline': bool(pdf_url and payload.get('downloadPdf') and not download),
            'duplicateBasis': duplicate_basis}


def _authorized_cnki_url(value):
    url = str(value or '').strip()
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    require(len(url) <= 8000 and parts.scheme == 'https' and host and not parts.username and not parts.password,
            '知网下载地址不正确')
    require(host == 'cnki.net' or host.endswith('.cnki.net') or host == 'cnki.com.cn' or host.endswith('.cnki.com.cn'),
            '只接受知网官方授权下载地址')
    return url


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
    """Import a completed CNKI browser download without reading browser credentials."""
    require(isinstance(payload, dict), '浏览器下载信息不正确')
    item_id = str(payload.get('itemId') or '')
    source_url = _authorized_cnki_url(payload.get('sourceUrl'))
    fmt = str(payload.get('format') or '').lower()
    require(fmt in ('pdf', 'caj'), '仅支持导入知网 PDF 或 CAJ 原件')
    path = _browser_download_path(payload.get('path'), fmt)
    added = fulltext.downloads.attachments.add(item_id, path, mode='managed', role='main')
    job = None
    if fmt == 'pdf' and not added.get('duplicate'):
        job = fulltext.jobs.create('pdf.index', {'attachmentId': added['id']})
    with library.db(True) as db:
        db.execute('INSERT INTO provenance VALUES(?,?,?,?,?)',
                   (uid(), item_id, 'browser authorized download',
                    dumps({'sourceUrl': source_url, 'format': fmt, 'fileName': path.name}), now()))
    return {'itemId': item_id, 'attachmentId': added['id'], 'duplicate': bool(added.get('duplicate')),
            'format': fmt, 'indexJobId': job['jobId'] if job else None,
            'message': ('PDF 已导入，正在解析并建立全文索引' if fmt == 'pdf' and job else
                        'PDF 已在文献库中' if fmt == 'pdf' else 'CAJ 原件已保存；当前版本暂不支持 CAJ 全文解析，请在知网导出 PDF 后再索引')}
