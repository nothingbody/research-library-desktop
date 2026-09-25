"""Loss-aware standard bibliographic import/export; no network side effects."""
from __future__ import annotations

import json
import re
from pathlib import Path

import bibtexparser
from bibtexparser.bparser import BibTexParser
from bibtexparser.bwriter import BibTexWriter
from bibtexparser.bibdatabase import BibDatabase
from pylatexenc.latex2text import LatexNodes2Text

from .common import AppError, doi, require

TYPE_TO_CSL = {'article': 'article-journal', 'inproceedings': 'paper-conference', 'conference': 'paper-conference',
               'book': 'book', 'incollection': 'chapter', 'phdthesis': 'thesis', 'mastersthesis': 'thesis',
               'techreport': 'report', 'misc': 'document', 'online': 'webpage', 'unpublished': 'manuscript'}
SOURCE_TO_CSL = {'journal-article': 'article-journal', 'proceedings-article': 'paper-conference',
                 'book-chapter': 'chapter', 'posted-content': 'manuscript', 'preprint': 'manuscript',
                 'article': 'article-journal', 'review': 'article-journal', 'dissertation': 'thesis',
                 'reference-entry': 'entry', 'journal-issue': 'periodical', 'journal-volume': 'periodical',
                 'journal': 'periodical', 'proceedings': 'book', 'edited-book': 'book',
                 'book-set': 'book', 'book-series': 'collection', 'other': 'document',
                 'letter': 'personal_communication', 'editorial': 'article-journal',
                 'supplementary-material': 'document'}
CSL_TYPES = {'article', 'article-journal', 'article-magazine', 'article-newspaper', 'bill', 'book',
             'broadcast', 'chapter', 'classic', 'collection', 'dataset', 'document', 'entry',
             'entry-dictionary', 'entry-encyclopedia', 'event', 'figure', 'graphic', 'hearing',
             'interview', 'legal_case', 'legislation', 'manuscript', 'map', 'motion_picture',
             'musical_score', 'pamphlet', 'paper-conference', 'patent', 'performance',
             'periodical', 'personal_communication', 'post', 'post-weblog', 'regulation',
             'report', 'review', 'review-book', 'software', 'song', 'speech', 'standard',
             'thesis', 'treaty', 'webpage'}
CSL_TO_BIB = {'article-journal': 'article', 'paper-conference': 'inproceedings', 'book': 'book',
              'chapter': 'incollection', 'thesis': 'phdthesis', 'report': 'techreport',
              'manuscript': 'unpublished', 'webpage': 'misc', 'document': 'misc'}
CSL_FIELDS = {
    'type', 'title', 'title-short', 'author', 'editor', 'translator', 'container-author',
    'issued', 'accessed', 'submitted', 'original-date', 'event-date', 'container-title',
    'container-title-short', 'collection-title', 'collection-number', 'volume', 'issue',
    'number', 'page', 'page-first', 'number-of-pages', 'edition', 'chapter-number',
    'DOI', 'URL', 'PMID', 'PMCID', 'abstract', 'publisher', 'publisher-place',
    'ISSN', 'ISBN', 'language', 'genre', 'keyword', 'note', 'status', 'medium',
    'event', 'event-title', 'event-place', 'archive', 'archive_location',
    'archive-place', 'jurisdiction', 'version', 'source',
}
LATEX = LatexNodes2Text()


def latex_text(value):
    return LATEX.latex_to_text(str(value or '')).strip()


def csl_type(value):
    value = str(value or 'article-journal').lower()
    result = SOURCE_TO_CSL.get(value, value)
    return result if result in CSL_TYPES else 'document'


def csl_record(item):
    result = {key: item[key] for key in sorted(CSL_FIELDS) if item.get(key) not in (None, '', [], {})}
    result['id'] = str(item.get('citationKey') or item.get('id') or '')
    result['type'] = csl_type(result.get('type'))
    if result.get('author'):
        result['author'] = [{key: name[key] for key in ('family', 'given', 'literal', 'suffix') if name.get(key)}
                            for name in result['author'] if isinstance(name, dict)]
    if result.get('page'):
        result['page'] = re.sub(r'\s*(?:--+|[–—])\s*', '-', str(result['page']))
    return result


def author_name(value):
    value = value.strip()
    if value.startswith('{') and value.endswith('}'):
        return {'literal': value[1:-1]}
    if ',' in value:
        family, given = value.split(',', 1)
        return {'family': family.strip(), 'given': given.strip()}
    parts = value.split()
    if len(parts) <= 1:
        return {'literal': value}
    return {'family': parts[-1], 'given': ' '.join(parts[:-1])}


def authors_text(authors):
    return '; '.join(a.get('literal') or ' '.join(filter(None, [a.get('family'), a.get('given')])) for a in (authors or []))


def normalize(data):
    require(isinstance(data, dict), '题录必须为对象')
    value = dict(data)
    value.pop('id', None)
    value['type'] = csl_type(value.get('type'))
    value['title'] = str(value.get('title') or '').strip()
    require(value['title'], '标题不能为空')
    require(len(value['title']) <= 20000, '标题过长')
    authors = value.get('author', [])
    if isinstance(authors, str):
        authors = [author_name(a) for a in authors.split(';') if a.strip()]
    require(isinstance(authors, list) and all(isinstance(a, dict) for a in authors), '作者格式不正确')
    value['author'] = authors
    if value.get('DOI'):
        value['DOI'] = doi(value['DOI'])
    if 'year' in value:
        year = str(value.pop('year') or '').strip()
        if year:
            require(bool(re.fullmatch(r'\d{4}', year)), '年份应为四位数字')
            value['issued'] = {'date-parts': [[int(year)]]}
        else:
            value.pop('issued', None)
    issued = value.get('issued', {})
    require(isinstance(issued, dict), '日期格式不正确')
    if issued.get('date-parts'):
        require(isinstance(issued['date-parts'], list) and isinstance(issued['date-parts'][0], list), '日期格式不正确')
    value.setdefault('readingState', 'unread')
    require(value['readingState'] in ('unread', 'reading', 'read'), '阅读状态不正确')
    value['tags'] = list(dict.fromkeys(str(x).strip() for x in value.get('tags', []) if str(x).strip()))
    return value


def year_of(value):
    parts = value.get('issued', {}).get('date-parts', [])
    return str(parts[0][0]) if parts and parts[0] else ''


def split_bib_authors(value):
    parts, start, depth, index = [], 0, 0, 0
    while index < len(value):
        char = value[index]
        if char == '{':
            depth += 1
        elif char == '}':
            depth = max(0, depth - 1)
        if depth == 0:
            match = re.match(r'\s+and\s+', value[index:])
            if match:
                parts.append(value[start:index])
                index += match.end()
                start = index
                continue
        index += 1
    parts.append(value[start:])
    return [{'literal': '等'} if part.strip().casefold() == 'others' else
            {'literal': latex_text(part.strip()[1:-1])} if part.strip().startswith('{') and part.strip().endswith('}') else
            author_name(latex_text(part))
            for part in parts if part.strip()]


def parse_bib(text):
    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    parser.homogenize_fields = False
    database = bibtexparser.loads(text, parser=parser)
    result = []
    for record in database.entries:
        item = {'type': TYPE_TO_CSL.get(record.get('ENTRYTYPE'), 'document'), 'title': latex_text(record.get('title', '')),
                'citationKey': record.get('ID', ''), 'author': split_bib_authors(record.get('author', '')),
                'originalBib': record}
        for key, dest in [('journal', 'container-title'), ('booktitle', 'container-title'), ('year', 'year'),
                          ('doi', 'DOI'), ('url', 'URL'), ('volume', 'volume'), ('number', 'issue'), ('pages', 'page'),
                          ('abstract', 'abstract'), ('publisher', 'publisher'), ('issn', 'ISSN'), ('isbn', 'ISBN')]:
            if record.get(key):
                item[dest] = record[key] if dest in ('year', 'DOI', 'URL', 'ISSN', 'ISBN') else latex_text(record[key])
        if item.get('page'):
            item['page'] = re.sub(r'\s*(?:--+|[–—])\s*', '-', item['page'])
        item['tags'] = [latex_text(x) for x in re.split('[,;]', record.get('keywords', '')) if x.strip()]
        if record.get('journaltitle'):
            item['container-title'] = latex_text(record['journaltitle'])
        if record.get('date') and not item.get('year'):
            match = re.match(r'^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?', record['date'])
            if match:
                item['issued'] = {'date-parts': [[int(p) for p in match.groups() if p]]}
        result.append(normalize(item))
    require(result, '未解析到 BibTeX 条目，请检查文件格式')
    return result


def parse_ris(text):
    records, current, last = [], {}, None
    for line in text.splitlines():
        match = re.match(r'^([A-Z0-9]{2})\s+-\s?(.*)$', line)
        if match:
            key, value = match.groups()
            if key == 'TY':
                if current:
                    records.append(current)
                current = {}
            if key == 'ER':
                records.append(current)
                current, last = {}, None
            else:
                current.setdefault(key, []).append(value)
                last = key
        elif line.strip() and last and current.get(last):
            current[last][-1] += '\n' + line.strip()
    if current:
        records.append(current)
    result = []
    for record in records:
        def first(*keys):
            return next((record[k][0] for k in keys if record.get(k)), '')
        item = {'type': {'JOUR': 'article-journal', 'BOOK': 'book', 'CHAP': 'chapter', 'CONF': 'paper-conference',
                         'THES': 'thesis', 'RPRT': 'report'}.get(first('TY'), 'document'),
                'title': first('TI', 'T1'), 'author': [author_name(a) for a in record.get('AU', record.get('A1', []))],
                'container-title': first('T2', 'JO', 'JF', 'JA') if first('TY') == 'CHAP' else first('JO', 'JF', 'T2', 'JA'), 'abstract': first('AB', 'N2'),
                'DOI': first('DO'), 'URL': first('UR'), 'volume': first('VL'), 'issue': first('IS'),
                'page': first('SP') + (('-' + first('EP')) if first('EP') else ''),
                'tags': record.get('KW', []), 'originalRIS': record}
        year = re.search(r'\d{4}', first('PY', 'Y1', 'DA'))
        if year:
            item['year'] = year.group()
        if first('SN'):
            item['ISSN' if item['type'] == 'article-journal' else 'ISBN'] = first('SN')
        result.append(normalize(item))
    require(result, '未解析到 RIS 条目')
    return result


def parse_file(path):
    path = Path(path)
    raw = path.read_bytes()
    require(len(raw) <= 64 * 1024 * 1024, '题录文件超过64MB，请拆分导入')
    text = None
    encodings = ('utf-16', 'utf-8-sig', 'gb18030') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-8-sig', 'gb18030')
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            pass
    require(text is not None, '无法识别文件编码')
    if path.suffix.lower() in ('.bib', '.bibtex'):
        return parse_bib(text)
    if path.suffix.lower() == '.ris':
        return parse_ris(text)
    if path.suffix.lower() == '.json':
        try:
            values = json.loads(text)
            if isinstance(values, dict):
                values = values.get('items', [values])
            require(isinstance(values, list), 'CSL JSON应为条目数组')
            return [normalize(v) for v in values]
        except (ValueError, TypeError) as exc:
            raise AppError('INVALID_FORMAT', f'JSON格式不正确：{exc}')
    raise AppError('UNSUPPORTED_FORMAT', '支持PDF、RIS、BibTeX和CSL-JSON文件')


def export_records(items, fmt):
    if fmt == 'json':
        return json.dumps([csl_record(item) for item in items], ensure_ascii=False, indent=2)
    if fmt in ('bib', 'bibtex', 'biblatex'):
        records = []
        for item in items:
            record = dict(item.get('originalBib') or {})
            original_type = record.get('ENTRYTYPE')
            record['ENTRYTYPE'] = original_type if original_type and TYPE_TO_CSL.get(original_type, 'document') == csl_type(item.get('type')) else CSL_TO_BIB.get(csl_type(item.get('type')), 'misc')
            if fmt == 'biblatex' and csl_type(item.get('type')) == 'webpage':
                record['ENTRYTYPE'] = 'online'
            elif fmt != 'biblatex' and record['ENTRYTYPE'] == 'online':
                record['ENTRYTYPE'] = 'misc'
            record['ID'] = item.get('citationKey') or 'item' + str(item.get('id', ''))[:8]
            for src, dest in [('title', 'title'), ('DOI', 'doi'), ('URL', 'url'), ('volume', 'volume'), ('issue', 'number'),
                              ('page', 'pages'), ('abstract', 'abstract'), ('publisher', 'publisher'), ('ISSN', 'issn'), ('ISBN', 'isbn')]:
                if item.get(src):
                    record[dest] = str(item[src])
                else:
                    record.pop(dest, None)
            for key in ('journal', 'booktitle'):
                record.pop(key, None)
            if item.get('container-title'):
                record['journal' if csl_type(item.get('type')) == 'article-journal' else 'booktitle'] = item['container-title']
            if year_of(item):
                record['year'] = year_of(item)
            else:
                record.pop('year', None)
            if fmt == 'biblatex':
                if record.get('journal'):
                    record['journaltitle'] = record.pop('journal')
                parts = item.get('issued', {}).get('date-parts', [])
                if parts and parts[0]:
                    record['date'] = '-'.join(str(p).zfill(4 if index == 0 else 2) for index, p in enumerate(parts[0]))
            authors = ['others' if a.get('literal') == '等' else '{' + a['literal'] + '}' if a.get('literal') else ', '.join(filter(None, [a.get('family'), a.get('given')])) for a in item.get('author', [])]
            if authors:
                record['author'] = ' and '.join(authors)
            else:
                record.pop('author', None)
            if item.get('tags'):
                record['keywords'] = ', '.join(item['tags'])
            records.append(record)
        database = BibDatabase()
        database.entries = records
        return bibtexparser.dumps(database, writer=BibTexWriter())
    if fmt == 'ris':
        lines = []
        for item in items:
            def line(key, value):
                if value:
                    lines.append(f'{key}  - {str(value).replace(chr(13), " ").replace(chr(10), " ")}')
            kind = csl_type(item.get('type'))
            line('TY', {'article-journal': 'JOUR', 'book': 'BOOK', 'paper-conference': 'CONF', 'thesis': 'THES', 'chapter': 'CHAP', 'report': 'RPRT', 'webpage': 'ELEC'}.get(kind, 'GEN'))
            line('TI', item['title'])
            for author in item.get('author', []):
                line('AU', author.get('literal') or ', '.join(filter(None, [author.get('family'), author.get('given')])))
            for field, tag in [('container-title', 'T2' if kind == 'chapter' else 'JO'), ('DOI', 'DO'), ('URL', 'UR'), ('abstract', 'AB'), ('volume', 'VL'), ('issue', 'IS'), ('ISSN', 'SN')]:
                line(tag, item.get(field))
            line('PY', year_of(item))
            pages = re.split(r'[-–]+', str(item.get('page', '')), maxsplit=1)
            line('SP', pages[0])
            if len(pages) > 1:
                line('EP', pages[1])
            for tag in item.get('tags', []):
                line('KW', tag)
            lines.extend(['ER  -', ''])
        return '\n'.join(lines)
    raise AppError('INVALID_ARGUMENT', '未知导出格式')
