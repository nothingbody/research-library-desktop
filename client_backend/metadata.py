from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .biblio import normalize
from .common import AppError, doi, require


def request(url):
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'ResearchLibrary/0.1 (desktop reference manager)', 'Accept': 'application/json, application/xml;q=0.9'})
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            require(len(raw) <= 8 * 1024 * 1024, '来源响应过大')
            return raw
    except urllib.error.HTTPError as exc:
        raise AppError('NOT_FOUND' if exc.code == 404 else 'RATE_LIMITED' if exc.code == 429 else 'SOURCE_ERROR',
                       '未找到该标识的题录' if exc.code == 404 else f'元数据来源返回 HTTP {exc.code}', retryable=exc.code in (429, 500, 502, 503))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AppError('NETWORK_UNAVAILABLE', '无法连接元数据来源，请检查网络后重试', retryable=True) from exc


def lookup(identifier):
    identifier = str(identifier or '').strip()
    require(0 < len(identifier) < 2000, '请输入DOI或PMID')
    if re.fullmatch(r'(?:PMID:\s*)?\d+', identifier, flags=re.I):
        pmid = re.sub(r'\D', '', identifier)
        xml = request('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&retmode=xml&id=' + pmid)
        root = ET.fromstring(xml)
        article = root.find('.//PubmedArticle')
        require(article is not None, '未找到PubMed条目')
        def text(path):
            node = article.find(path)
            return ''.join(node.itertext()).strip() if node is not None else ''
        authors = []
        for node in article.findall('.//AuthorList/Author'):
            collective = node.findtext('CollectiveName')
            authors.append({'literal': collective} if collective else {'family': node.findtext('LastName') or '', 'given': node.findtext('ForeName') or node.findtext('Initials') or ''})
        values = {'type': 'article-journal', 'title': text('.//ArticleTitle'), 'author': authors, 'PMID': pmid,
                  'container-title': text('.//Journal/Title'), 'abstract': '\n'.join(''.join(n.itertext()) for n in article.findall('.//AbstractText')),
                  'volume': text('.//JournalIssue/Volume'), 'issue': text('.//JournalIssue/Issue'), 'ISSN': text('.//Journal/ISSN'), 'page': text('.//MedlinePgn')}
        year = text('.//PubDate/Year') or text('.//PubDate/MedlineDate')[:4]
        if year.isdigit():
            values['year'] = year
        for node in article.findall('.//ArticleId'):
            if node.get('IdType') == 'doi':
                values['DOI'] = node.text or ''
        return {'data': normalize(values), 'source': 'PubMed', 'sourceUrl': 'https://pubmed.ncbi.nlm.nih.gov/' + pmid + '/', 'identifier': pmid}
    identifier = doi(identifier)
    require(bool(re.match(r'^10\.\d{4,9}/\S+$', identifier)), '请输入有效DOI（10.xxxx/…）或PMID数字')
    body = json.loads(request('https://api.crossref.org/works/' + urllib.parse.quote(identifier, safe='')))
    record = body.get('message', {})
    values = {'type': {'journal-article': 'article-journal', 'proceedings-article': 'paper-conference', 'book': 'book', 'book-chapter': 'chapter', 'report': 'report'}.get(record.get('type'), 'document'),
              'title': (record.get('title') or [''])[0], 'author': [{k: a[k] for k in ('family', 'given', 'ORCID', 'name') if k in a} for a in record.get('author', [])],
              'container-title': (record.get('container-title') or [''])[0], 'DOI': record.get('DOI', identifier),
              'URL': record.get('URL', ''), 'publisher': record.get('publisher', ''), 'volume': record.get('volume', ''),
              'issue': record.get('issue', ''), 'page': record.get('page', ''), 'ISSN': ', '.join(record.get('ISSN', [])),
              'abstract': re.sub('<[^>]+>', '', record.get('abstract', '')), 'issued': record.get('issued', {}), 'sourceRecord': record}
    for author in values['author']:
        if author.get('name') and not author.get('family'):
            author['literal'] = author.pop('name')
    return {'data': normalize(values), 'source': 'Crossref', 'sourceUrl': 'https://doi.org/' + identifier, 'identifier': identifier}
