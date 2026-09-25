from __future__ import annotations

"""Local-first, evidence-preserving academic discovery.

The service deliberately queries documented public scholarly APIs rather than
scraping search-engine result pages.  Every network record is normalised into a
local SQLite cache and keeps its source and retrieval time so an AI relevance
explanation never becomes the only evidence for a result.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
import hashlib
import json
import re
import threading
import time
from urllib import error, parse, request
from xml.etree import ElementTree

from .biblio import normalize
from .common import AppError, bounded_int, doi, dumps, norm, now, require, uid
from .search_evidence import infer, validate, fragments, screen, disposition, verified_model


SOURCES = ('openalex', 'crossref', 'pubmed', 'arxiv')
SOURCE_LABELS = {'openalex': 'OpenAlex', 'crossref': 'Crossref', 'pubmed': 'PubMed', 'arxiv': 'arXiv'}
SOURCE_ENDPOINTS = {
    'openalex': 'https://api.openalex.org/works',
    'crossref': 'https://api.crossref.org/works',
    'pubmed': 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/',
    'arxiv': 'https://export.arxiv.org/api/query',
}
STOPWORDS = {
    'about', 'after', 'also', 'among', 'and', 'are', 'been', 'between', 'can', 'does', 'for', 'from', 'into', 'its', 'not',
    'only', 'our', 'over', 'paper', 'papers', 'research', 'review', 'should', 'such', 'that', 'the', 'their', 'these', 'this',
    'through', 'using', 'with', '以及', '一个', '一些', '主要', '其中', '关于', '包括', '可以', '进行', '我们', '文献', '方法', '研究',
}


def _text(value, limit=20000):
    plain = re.sub(r'<[^>]+>', ' ', str(value or ''))
    return re.sub(r'\s+', ' ', unescape(plain)).strip()[:limit]


def _year(value):
    if isinstance(value, dict):
        return _year(value.get('date-parts'))
    if isinstance(value, int):
        return value if 1000 <= value <= 9999 else None
    if isinstance(value, list) and value and isinstance(value[0], list) and value[0]:
        return _year(value[0][0])
    try:
        found = re.search(r'\b(1[5-9]\d{2}|20\d{2})\b', str(value or ''))
        return int(found.group(1)) if found else None
    except (TypeError, ValueError):
        return None


def _authors(values):
    result = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        literal = _text(value.get('name') or value.get('collective') or value.get('literal'), 300)
        if literal:
            result.append({'literal': literal})
            continue
        given, family = _text(value.get('given'), 200), _text(value.get('family'), 200)
        if given or family:
            result.append({key: item for key, item in {'given': given, 'family': family}.items() if item})
    return result[:100]


class AiSearch:
    """Search sessions, public-source adapters, local deduplication and import."""

    def __init__(self, library, jobs, assistant=None):
        self.library, self.jobs, self.assistant = library, jobs, assistant
        self._running = {}
        self._verifying = set()
        self._lock = threading.RLock()

    @staticmethod
    def _clean_sources(value):
        values = list(dict.fromkeys(str(source) for source in (value or SOURCES)))
        require(0 < len(values) <= len(SOURCES) and set(values) <= set(SOURCES), '数据源选择不正确')
        return values

    @staticmethod
    def _terms(brief):
        english = [word.casefold() for word in re.findall(r"[A-Za-z][A-Za-z0-9+._-]{2,}", brief) if word.casefold() not in STOPWORDS]
        chinese = [part for part in re.findall(r'[\u3400-\u9fff]{2,}', brief) if part not in STOPWORDS]
        return list(dict.fromkeys(english + chinese))[:28]

    def _fallback_plan(self, brief, mode, sources):
        quoted = [_text(part, 180) for part in re.findall(r'[“\"”]([^“\"”]{3,180})[”\"]', brief)]
        terms = self._terms(brief)
        criteria = infer(brief)
        concepts = {criterion['id'] for criterion in criteria}
        query_pairs = []
        if ('物流' in brief or 'logistics' in brief.casefold()) and (
                '大模型' in brief or '语言模型' in brief or 'llm' in brief.casefold()):
            criteria = [
                {'id': 'logistics-domain', 'label': '物流或供应链领域', 'kind': 'include', 'required': True,
                 'terms': ['logistics', 'supply chain', 'freight', 'warehousing', '物流', '供应链'], 'negativeTerms': []},
                {'id': 'large-language-model', 'label': '大模型或生成式 AI', 'kind': 'include', 'required': True,
                 'terms': ['large language model', 'large language models', 'LLM', 'generative AI',
                           'foundation model', 'ChatGPT', '大模型', '生成式人工智能'], 'negativeTerms': []},
            ] + criteria
            query_pairs = [
                ('大模型与智慧物流', 'large language models smart logistics review'),
                ('生成式 AI 与物流', 'generative AI logistics supply chain survey'),
                ('智慧物流发展', 'smart logistics artificial intelligence literature review'),
            ]
        elif 'fjsp' in concepts and 'transport' in concepts:
            # Public English-language indexes perform poorly when an entire Chinese
            # instruction paragraph is sent as a query. Search domain concepts instead.
            suffix = ' review' if 'review' in concepts else ''
            query_pairs = [
                ('柔性作业车间与 AGV', 'flexible job shop scheduling automated guided vehicle' + suffix),
                ('运输资源视角', 'flexible job shop transportation resources' + (' survey' if suffix else '')),
                ('生产与运输协同', 'integrated production transportation scheduling AGV' + suffix),
            ]
            if 'review' in concepts:
                query_pairs.append(('集成调度综述', 'survey integrated flexible job shop scheduling'))
            if 'surrogate' in concepts:
                query_pairs.append(('代理评价视角', 'surrogate assisted flexible job shop AGV scheduling'))
            if 'multiobjective' in concepts:
                query_pairs.append(('多目标视角', 'multi objective flexible job shop AGV scheduling'))
        elif 'fjsp' in concepts:
            suffix = ' review' if 'review' in concepts else ''
            query_pairs = [('柔性作业车间', 'flexible job shop scheduling' + suffix)]
            if 'multiobjective' in concepts:
                query_pairs.append(('多目标视角', 'multi objective flexible job shop scheduling' + suffix))
            if 'surrogate' in concepts:
                query_pairs.insert(0, ('代理评价视角', 'surrogate model flexible job shop scheduling'))
            if 'distributed' in concepts:
                query_pairs.append(('多车间视角', 'distributed flexible job shop scheduling'))
        if not query_pairs:
            english = ' '.join(word for word in terms if re.search(r'[a-z]', word))[:220].strip()
            first_clause = re.split(r'[。；;\n]', brief, maxsplit=1)[0]
            primary = _text(quoted[0] if quoted else english or first_clause, 220)
            query_pairs = [('核心问题', primary or 'research literature')]
        for index, phrase in enumerate(quoted[:2], start=1):
            if re.search(r'[a-zA-Z]{3}', phrase) and not any(norm(phrase) == norm(q) for _, q in query_pairs):
                query_pairs.append((f'用户术语 {index}', phrase))
        queries = [{'id': uid(), 'label': label, 'query': query} for label, query in query_pairs[:6]]
        mappings = [{'term': c['label'], 'translation': c['terms'][0]} for c in criteria]
        return {
            'summary': '系统会先在公开学术数据源中召回候选记录，再按题名、摘要和研究约束进行本地透明排序。',
            'questions': [brief[:500]],
            'queries': queries,
            'inclusion': ['与研究任务主题直接相关', '具备可核验的题名、作者或来源记录'],
            'exclusion': ['来源无法识别的重复记录', '仅由宽泛关键词偶然命中的条目'],
            'terms': terms,
            'mode': mode,
            'sources': sources,
            'generatedAt': now(),
            'generator': 'transparent-local-v1',
            'criteria': criteria,
            'termMappings': mappings,
        }

    def _validate_plan(self, value, session=None):
        require(isinstance(value, dict), '检索计划格式不正确')
        source_default = session.get('sources', SOURCES) if session else SOURCES
        sources = self._clean_sources(value.get('sources', source_default))
        raw_queries = value.get('queries') or []
        require(isinstance(raw_queries, list) and 0 < len(raw_queries) <= 6, '请保留 1–6 条检索式')
        queries = []
        for index, raw in enumerate(raw_queries):
            raw = raw if isinstance(raw, dict) else {'query': raw}
            query = _text(raw.get('query'), 420)
            require(2 <= len(query) <= 420, '单条检索式长度应为2–420个字符')
            queries.append({'id': str(raw.get('id') or uid()), 'label': _text(raw.get('label') or f'检索式 {index + 1}', 80), 'query': query, 'enabled': raw.get('enabled', True) is not False})
        require(any(q['enabled'] for q in queries), '请至少启用一条检索式')
        def string_list(key, maximum, item_limit):
            values = value.get(key) or []
            require(isinstance(values, list) and len(values) <= maximum, f'{key} 格式不正确')
            return [_text(item, item_limit) for item in values if _text(item, item_limit)]
        return {
            'summary': _text(value.get('summary'), 1600),
            'questions': string_list('questions', 8, 700),
            'queries': queries,
            'inclusion': string_list('inclusion', 12, 400),
            'exclusion': string_list('exclusion', 12, 400),
            'terms': self._terms(' '.join(query['query'] for query in queries)),
            'mode': value.get('mode') if value.get('mode') in ('quick', 'deep') else (session or {}).get('mode', 'quick'),
            'sources': sources,
            'generatedAt': value.get('generatedAt') or now(),
            'generator': value.get('generator') or 'user-edited',
            'criteria': validate(value.get('criteria', infer((session or {}).get('brief', ' '.join(q['query'] for q in queries))))),
            'termMappings': [{'term': _text(m.get('term'), 160), 'translation': _text(m.get('translation'), 200)} for m in value.get('termMappings', [])[:20] if isinstance(m, dict)],
        }

    @staticmethod
    def _session_value(row, source_runs=None):
        value = dict(row)
        value.update(
            brief=value.pop('brief'), plan=json.loads(value.pop('plan_json')), sources=json.loads(value.pop('sources_json')),
            resultCount=value.pop('result_count'), planningWarning=value.pop('planning_warning', ''), createdAt=value.pop('created_at'), updatedAt=value.pop('updated_at'),
        )
        if source_runs is not None:
            value['sourceRuns'] = source_runs
        return value

    def create(self, payload):
        brief = _text(payload.get('brief'), 6000)
        require(2 <= len(brief) <= 6000, '请输入2–6000个字符的研究问题')
        mode = payload.get('mode', 'quick')
        require(mode in ('quick', 'deep'), '检索模式不正确')
        sources = self._clean_sources(payload.get('sources'))
        plan = self._fallback_plan(brief, mode, sources)
        planning_warning = ''
        if payload.get('useModel', True) and self.assistant and self.library.get_settings().get('online', True):
            try:
                if self.assistant.status().get('ready'):
                    generated = self.assistant.search_plan(brief, plan)
                    plan = self._validate_plan({**plan, **generated, 'mode': mode, 'sources': sources, 'generator': 'configured-model'}, {'brief': brief})
            except AppError as exc:
                planning_warning = str(exc)[:300]
        title = _text(payload.get('title'), 180) or brief[:72]
        session_id, timestamp = uid(), now()
        with self.library.db(True) as db:
            db.execute('''INSERT INTO ai_search_sessions(id,title,brief,mode,plan_json,sources_json,state,result_count,planning_warning,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,0,?,?,?)''', (session_id, title, brief, mode, dumps(plan), dumps(sources), 'planned', planning_warning, timestamp, timestamp))
        return self.get(session_id)

    def list(self, limit=100):
        limit = bounded_int(limit, 100, 1, 300)
        with self.library.db() as db:
            rows = db.execute('SELECT * FROM ai_search_sessions ORDER BY updated_at DESC LIMIT ?', (limit,)).fetchall()
        return [self._session_value(row) for row in rows]

    def _repair_verification(self, session_id):
        # A cancelled queued job has no handler finally block. Keep busy while any
        # handler is still unwinding so a rerun cannot overwrite its snapshot.
        with self.jobs.lock:
            active_jobs = set(self.jobs.active)
        with self.library.db(True) as db:
            pending = db.execute("SELECT state FROM ai_search_sessions WHERE id=?", (session_id,)).fetchone()
            if pending and pending[0] in ('verifying','queued','cancelling') and session_id not in self._verifying and session_id not in self._running:
                kind = 'ai-search.verify' if pending[0] == 'verifying' else 'ai-search.run'
                jobs = db.execute('SELECT id,payload,state FROM jobs WHERE kind=?', (kind,)).fetchall()
                if not any(json.loads(j['payload']).get('sessionId') == session_id and (j['state'] in ('pending','running') or j['id'] in active_jobs) for j in jobs):
                    last = db.execute('SELECT state FROM search_runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (session_id,)).fetchone()
                    state = (last[0] if last else 'planned') if pending[0] == 'verifying' else 'cancelled'
                    db.execute('UPDATE ai_search_sessions SET state=? WHERE id=?', (state, session_id))

    def get(self, session_id):
        with self._lock:
            self._repair_verification(session_id)
        with self.library.db() as db:
            row = db.execute('SELECT * FROM ai_search_sessions WHERE id=?', (session_id,)).fetchone()
            require(row, '检索任务不存在')
            runs = [dict(item) for item in db.execute('''SELECT id,source,state,progress,query,received,accepted,message,error,
                started_at AS startedAt,completed_at AS completedAt FROM ai_search_source_runs WHERE session_id=? ORDER BY source''', (session_id,))]
            for run in runs:
                run['error'] = json.loads(run['error']) if run['error'] else None
        result = self._session_value(row, runs)
        result['runs'] = self.runs(session_id)
        result['activeRunId'] = result['runs'][0]['id'] if result['runs'] else None
        return result

    def save_plan(self, payload):
        session_id = payload.get('sessionId')
        require(isinstance(session_id, str), '缺少检索任务标识')
        with self.library.db(True) as db:
            row = db.execute('SELECT * FROM ai_search_sessions WHERE id=?', (session_id,)).fetchone()
            require(row, '检索任务不存在')
            require(row['state'] not in ('running', 'queued', 'cancelling', 'verifying', 'expanding', 'linking'), '检索运行中，完成或取消后再修改计划')
            session = self._session_value(row)
            plan = self._validate_plan(payload.get('plan'), session)
            db.execute("UPDATE ai_search_sessions SET plan_json=?,sources_json=?,mode=?,state='planned',updated_at=? WHERE id=?",
                       (dumps(plan), dumps(plan['sources']), plan['mode'], now(), session_id))
        return self.get(session_id)

    def submit(self, payload):
        with self._lock:
            return self._submit(payload)

    def _submit(self, payload):
        session_id = payload.get('sessionId')
        require(isinstance(session_id, str), '缺少检索任务标识')
        require(self.library.get_settings().get('online', True), '联网已关闭，请先在设置中开启')
        with self.library.db(True) as db:
            row = db.execute('SELECT * FROM ai_search_sessions WHERE id=?', (session_id,)).fetchone()
            require(row, '检索任务不存在')
            require(row['state'] not in ('running', 'queued', 'cancelling', 'verifying', 'expanding', 'linking'), '该检索任务正在运行或等待执行')
            db.execute("UPDATE ai_search_sessions SET state='queued',updated_at=? WHERE id=?", (now(), session_id))
        try:
            job = self.jobs.create('ai-search.run', {'sessionId': session_id, 'plan': json.loads(row['plan_json'])})
        except Exception:
            with self.library.db(True) as db:
                db.execute('UPDATE ai_search_sessions SET state=? WHERE id=?', (row['state'], session_id))
            raise
        return {'sessionId': session_id, **job}

    def _fetch(self, url, source, accept):
        label = SOURCE_LABELS[source]
        attempts, delay = 3, 1.0
        for attempt in range(attempts):
            req = request.Request(url, headers={'Accept': accept, 'User-Agent': 'ResearchLibrary/0.3 (local academic search)'})
            try:
                with request.urlopen(req, timeout=22) as response:
                    raw = response.read(4 * 1024 * 1024 + 1)
            except error.HTTPError as exc:
                retryable = exc.code >= 500 or exc.code == 429
                if retryable and attempt < attempts - 1:
                    time.sleep(self._retry_delay(exc, delay))
                    delay *= 2
                    continue
                raise AppError('SOURCE_HTTP_ERROR', f'{label} 返回 HTTP {exc.code}', retryable=retryable) from exc
            except error.URLError as exc:
                raise AppError('SOURCE_NETWORK_ERROR', f'无法连接 {label}', retryable=True) from exc
            except TimeoutError as exc:
                raise AppError('SOURCE_TIMEOUT', f'{label} 响应超时', retryable=True) from exc
            require(len(raw) <= 4 * 1024 * 1024, f'{label} 返回内容过大')
            return raw

    @staticmethod
    def _retry_delay(exc, fallback):
        # Honor a numeric Retry-After up to 10s; the HTTP-date form falls back.
        retry_after = exc.headers.get('Retry-After') if exc.headers else None
        if retry_after:
            try:
                return min(max(float(retry_after), 0.0), 10.0)
            except ValueError:
                pass
        return fallback

    def _json(self, url, source):
        raw = self._fetch(url, source, 'application/json')
        try:
            return json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AppError('SOURCE_RESPONSE_INVALID', f'{SOURCE_LABELS[source]} 返回格式异常') from exc

    def _xml(self, url, source):
        raw = self._fetch(url, source, 'application/atom+xml')
        try:
            return ElementTree.fromstring(raw)
        except ElementTree.ParseError as exc:
            raise AppError('SOURCE_RESPONSE_INVALID', f'{SOURCE_LABELS[source]} 返回格式异常') from exc

    @staticmethod
    def _abstract_from_index(value):
        if not isinstance(value, dict):
            return ''
        words = {}
        for word, positions in value.items():
            for position in positions if isinstance(positions, list) else []:
                if isinstance(position, int):
                    words[position] = word
        return ' '.join(words[index] for index in sorted(words))[:20000]

    def _openalex(self, query, limit, offset=0):
        params = {'search': query, 'per-page': str(limit), 'page': str(offset // limit + 1), 'select': 'id,doi,title,publication_year,authorships,primary_location,cited_by_count,open_access,type,abstract_inverted_index'}
        # Polite pool: a configured contact address gets a separate, more generous quota.
        contact = str(self.library.get_settings().get('contactEmail') or '').strip()
        if re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', contact):
            params['mailto'] = contact
        data = self._json(SOURCE_ENDPOINTS['openalex'] + '?' + parse.urlencode(params), 'openalex')
        return self._openalex_rows(data.get('results') or [])

    def _openalex_rows(self, items):
        records = []
        for item in items:
            source = ((item.get('primary_location') or {}).get('source') or {})
            authors = []
            for author in item.get('authorships') or []:
                name = _text((author.get('author') or {}).get('display_name'), 300)
                if name:
                    authors.append({'literal': name})
            records.append({'source': 'openalex', 'sourceId': _text(item.get('id'), 500), 'title': _text(item.get('title'), 1500),
                            'authors': authors, 'year': _year(item.get('publication_year')), 'venue': _text(source.get('display_name'), 600),
                            'issn': _text(source.get('issn_l') or next(iter(source.get('issn') or []), ''), 20),
                            'abstract': self._abstract_from_index(item.get('abstract_inverted_index')), 'doi': doi(item.get('doi')),
                            'url': _text((item.get('primary_location') or {}).get('landing_page_url') or item.get('id'), 1200),
                            'citationCount': int(item.get('cited_by_count') or 0), 'oaUrl': _text((item.get('open_access') or {}).get('oa_url'), 1200),
                             'type': _text(item.get('type'), 100),
                             'keywords': [_text(value.get('display_name'), 160) for value in (item.get('keywords') or [])
                                          if isinstance(value, dict) and _text(value.get('display_name'), 160)][:20], 'raw': item})
        return records

    def _crossref(self, query, limit, offset=0):
        params = {'query.bibliographic': query, 'rows': str(limit), 'offset': str(offset), 'select': 'DOI,title,author,published-print,published-online,container-title,abstract,URL,type,is-referenced-by-count,link,ISSN'}
        data = self._json(SOURCE_ENDPOINTS['crossref'] + '?' + parse.urlencode(params), 'crossref')
        records = []
        for item in ((data.get('message') or {}).get('items') or []):
            links = item.get('link') or []
            records.append({'source': 'crossref', 'sourceId': doi(item.get('DOI')) or _text(item.get('URL'), 800), 'title': _text((item.get('title') or [''])[0], 1500),
                            'authors': _authors(item.get('author')), 'year': _year(item.get('published-print') or item.get('published-online')),
                            'issn': _text(next(iter(item.get('ISSN') or []), ''), 20),
                            'venue': _text((item.get('container-title') or [''])[0], 600), 'abstract': _text(item.get('abstract'), 20000),
                            'doi': doi(item.get('DOI')), 'url': _text(item.get('URL'), 1200), 'citationCount': int(item.get('is-referenced-by-count') or 0),
                            'oaUrl': '', 'type': _text(item.get('type'), 100), 'raw': item})
        return records

    def _pubmed(self, query, limit, offset=0):
        search_url = SOURCE_ENDPOINTS['pubmed'] + 'esearch.fcgi?' + parse.urlencode({'db': 'pubmed', 'term': query, 'retmax': str(limit), 'retstart': str(offset), 'retmode': 'json', 'sort': 'relevance'})
        ids = (self._json(search_url, 'pubmed').get('esearchresult') or {}).get('idlist') or []
        if not ids:
            return []
        summary_url = SOURCE_ENDPOINTS['pubmed'] + 'esummary.fcgi?' + parse.urlencode({'db': 'pubmed', 'id': ','.join(ids), 'retmode': 'json'})
        result = self._json(summary_url, 'pubmed').get('result') or {}
        abstracts = self._pubmed_abstracts(ids)
        records = []
        for identifier in ids:
            item = result.get(identifier) or {}
            authors = [{'literal': _text(author.get('name'), 300)} for author in item.get('authors') or [] if _text(author.get('name'), 300)]
            article_ids = item.get('articleids') or []
            record_doi = next((doi(value.get('value')) for value in article_ids if value.get('idtype') == 'doi'), '')
            records.append({'source': 'pubmed', 'sourceId': str(identifier), 'title': _text(item.get('title'), 1500), 'authors': authors,
                            'year': _year(item.get('pubdate') or item.get('sortpubdate')), 'venue': _text(item.get('fulljournalname') or item.get('source'), 600),
                            'abstract': abstracts.get(str(identifier), ''), 'doi': record_doi, 'url': 'https://pubmed.ncbi.nlm.nih.gov/' + str(identifier) + '/',
                            'citationCount': 0, 'oaUrl': '', 'type': 'article-journal', 'raw': item})
        return records

    def _pubmed_abstracts(self, ids):
        """Fetch abstracts via efetch; esummary never includes them. Failures degrade to ''."""
        fetch_url = SOURCE_ENDPOINTS['pubmed'] + 'efetch.fcgi?' + parse.urlencode({'db': 'pubmed', 'id': ','.join(ids), 'retmode': 'xml'})
        try:
            root = self._xml(fetch_url, 'pubmed')
        except AppError:
            return {}
        abstracts = {}
        for article in root.iter('PubmedArticle'):
            pmid = (article.findtext('.//MedlineCitation/PMID') or '').strip()
            if not pmid:
                continue
            parts = [''.join(node.itertext()) for node in article.iter('AbstractText')]
            text = ' '.join(re.sub(r'\s+', ' ', part).strip() for part in parts if part and part.strip())
            if text:
                abstracts[pmid] = _text(text, 20000)
        return abstracts

    def _arxiv(self, query, limit, offset=0):
        words = re.findall(r'[A-Za-z0-9+._-]+', query)
        require(words, 'arXiv需要英文检索词，请编辑检索计划')
        expression = ' AND '.join('all:' + word for word in words)
        root = self._xml(SOURCE_ENDPOINTS['arxiv'] + '?' + parse.urlencode({'search_query': expression or 'all:research', 'start': offset, 'max_results': limit}), 'arxiv')
        atom = '{http://www.w3.org/2005/Atom}'
        records = []
        for item in root.findall(atom + 'entry'):
            source_id = _text(item.findtext(atom + 'id'), 1000)
            records.append({'source': 'arxiv', 'sourceId': source_id.rsplit('/', 1)[-1], 'title': _text(item.findtext(atom + 'title'), 1500),
                            'authors': [{'literal': _text(author.findtext(atom + 'name'), 300)} for author in item.findall(atom + 'author') if _text(author.findtext(atom + 'name'), 300)],
                            'year': _year(item.findtext(atom + 'published')), 'venue': 'arXiv', 'abstract': _text(item.findtext(atom + 'summary'), 20000),
                            'doi': doi(item.findtext('{http://arxiv.org/schemas/atom}doi')),
                            'url': source_id, 'citationCount': 0, 'oaUrl': source_id, 'type': 'article', 'raw': {'id': source_id}})
        return records

    def _source_records(self, source, queries, limit, offset=0):
        method = getattr(self, '_' + source)
        records = []
        seen = set()
        for query in queries:
            for record in method(query, limit, offset):
                key = record.get('doi') or record.get('sourceId') or norm(record.get('title'))
                if key and key not in seen and record.get('title'):
                    seen.add(key)
                    records.append(record)
        return records

    @staticmethod
    def _canonical_key(record):
        return ('doi:' + record['doi']) if record.get('doi') else ('source:' + record['source'] + ':' + record.get('sourceId', '') if record.get('sourceId') else 'title:' + norm(record.get('title')) + ':' + str(record.get('year') or ''))

    def _score(self, session, record):
        plan = session['plan']
        terms = plan.get('terms') or self._terms(session['brief'])
        haystack = norm(' '.join([record.get('title', ''), record.get('abstract', ''), record.get('venue', '')]))
        hits = [term for term in terms if norm(term) and norm(term) in haystack]
        title_hits = [term for term in hits if norm(term) in norm(record.get('title'))]
        denominator = max(3, min(8, len(terms)))
        score = .18 + min(.58, len(hits) / denominator * .58) + min(.14, len(title_hits) * .04)
        if record.get('abstract'):
            score += .06
        if record.get('doi'):
            score += .03
        if record.get('citationCount', 0) >= 25:
            score += .03
        score = round(min(.99, score), 3)
        tier = 'core' if score >= .70 else ('related' if score >= .48 else 'candidate')
        if plan.get('criteria') and disposition(screen(plan['criteria'], fragments([record])), fragments([record])) != 'eligible':
            tier = 'candidate'
        source = SOURCE_LABELS[record['source']]
        if hits:
            reason = f"题名或摘要匹配 {('、'.join(hits[:4]))}；记录来自 {source}" + ('，并提供摘要证据。' if record.get('abstract') else '。')
        else:
            reason = f'来自 {source} 的候选记录；尚未找到足够的题名或摘要词汇证据。'
        evidence = [{'kind': 'title', 'label': '题名', 'text': record.get('title', ''), 'source': source}]
        if record.get('abstract'):
            abstract = record['abstract']
            evidence.append({'kind': 'abstract', 'label': '摘要片段', 'text': abstract[:900], 'source': source})
        evidence.append({'kind': 'provenance', 'label': '数据来源', 'text': f'{source} · 本次检索获取', 'source': source})
        return score, tier, reason, evidence

    @staticmethod
    def _author_key(authors):
        if not authors:
            return ''
        a = authors[0]
        return ' '.join(sorted(re.findall(r'\w+', (a.get('literal') or (a.get('given', '') + ' ' + a.get('family', ''))).casefold())))

    def _upsert_record(self, db, record):
        key, timestamp = self._canonical_key(record), now()
        row = db.execute('SELECT * FROM ai_search_works WHERE canonical_key=? OR (doi<>\'\' AND doi=?)', (key, record.get('doi') or '')).fetchone()
        # Conservative fallback: exact title, year and first author; never merge conflicting DOIs.
        if not row and record.get('year') and self._author_key(record.get('authors')):
            matches = db.execute('SELECT * FROM ai_search_works WHERE title_norm=? AND year=?', (norm(record['title']), record['year'])).fetchall()
            matches = [r for r in matches if self._author_key(json.loads(r['authors_json'])) == self._author_key(record['authors'])
                       and not (r['doi'] and record.get('doi') and r['doi'] != record['doi'])]
            if len(matches) == 1:
                row = matches[0]
        if row:
            return row['id']
        work_id = uid()
        db.execute('INSERT INTO ai_search_works(id,canonical_key,title,title_norm,authors_json,year,venue,abstract,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (work_id, key, record['title'], norm(record['title']), dumps(record.get('authors') or []), record.get('year'), record.get('venue') or '', record.get('abstract') or '', timestamp, timestamp))
        return work_id

    @staticmethod
    def _merge(variants):
        fields = ['title', 'authors', 'year', 'venue', 'issn', 'abstract', 'keywords', 'doi', 'url', 'citationCount', 'oaUrl', 'type']
        result, provenance = {}, {}
        ranks = {'crossref': 0, 'pubmed': 1, 'openalex': 2, 'arxiv': 3}
        for field in fields:
            options = [v for v in variants if v.get(field) not in (None, '', [])]
            def order(v):
                priority = (0 if v['source'] == 'openalex' else 1) if field in ('citationCount', 'oaUrl') else ranks.get(v['source'], 9)
                return (priority, v['source'], str(v.get('sourceId', '')))
            options.sort(key=order)
            result[field] = options[0][field] if options else ([] if field in ('authors', 'keywords') else None if field == 'year' else 0 if field == 'citationCount' else '')
            provenance[field] = {'chosenSource': options[0]['source'] if options else None,
                                 'values': [{'value': v[field], 'source': v['source'], 'retrievedAt': v.get('retrievedAt')} for v in options]}
        result['fieldSources'] = provenance
        return result

    def _store_source(self, session, source, records):
        accepted = 0
        with self.library.db(True) as db:
            for record in records:
                if not record.get('title'):
                    continue
                record = {**record, 'source': source, 'doi': doi(record.get('doi')), 'retrievedAt': now()}
                work_id = self._upsert_record(db, record)
                db.execute('INSERT INTO ai_search_work_sources(work_id,source,source_id,retrieved_at,raw_json) VALUES(?,?,?,?,?) ON CONFLICT(work_id,source,source_id) DO UPDATE SET retrieved_at=excluded.retrieved_at,raw_json=excluded.raw_json',
                           (work_id, source, str(record.get('sourceId') or record['title']), now(), dumps(record.get('raw') or {})))
                existing = db.execute('SELECT c.id,d.data_json FROM ai_search_candidates c LEFT JOIN search_candidate_details d ON d.candidate_id=c.id WHERE c.session_id=? AND c.work_id=?', (session['id'], work_id)).fetchone()
                old = json.loads(existing['data_json']) if existing and existing['data_json'] else {}
                variants = [v for v in old.get('variants', []) if (v['source'], v.get('sourceId')) != (source, record.get('sourceId'))]
                previous = next((v for v in old.get('variants', []) if (v['source'], v.get('sourceId')) == (source, record.get('sourceId'))), {})
                variant = {**previous, **{k: v for k, v in record.items() if k != 'raw' and v not in (None, '', [])}}
                variants.append(variant)
                merged = self._merge(variants)
                evidence = fragments(variants)
                checks = screen(session['plan'].get('criteria', []), evidence)
                screening = disposition(checks, evidence)
                score, tier, explanation, _ = self._score(session, {**merged, 'source': source})
                if screening != 'eligible' and session['plan'].get('criteria'):
                    tier = 'candidate'
                candidate_id = existing['id'] if existing else uid()
                data = {**merged, 'id': candidate_id, 'workId': work_id, 'variants': variants, 'score': score, 'tier': tier,
                         'explanation': explanation, 'evidence': evidence, 'checks': checks, 'screening': screening,
                         'sources': sorted({v['source'] for v in variants}), 'fields': {}, 'verification': 'rules', 'runId': session['runId']}
                if old.get('introZh'):
                    data['introZh'] = old['introZh']
                # Update the global cache from merged values; current candidates always read their own snapshot.
                db.execute('UPDATE ai_search_works SET title=?,title_norm=?,authors_json=?,year=?,venue=?,abstract=?,doi=?,url=?,citation_count=?,oa_url=?,type=?,updated_at=? WHERE id=?',
                           (merged['title'], norm(merged['title']), dumps(merged['authors']), merged['year'], merged['venue'], merged['abstract'], merged['doi'], merged['url'], merged['citationCount'], merged['oaUrl'], merged['type'], now(), work_id))
                if existing:
                    db.execute('UPDATE ai_search_candidates SET score=?,tier=?,explanation=?,evidence_json=?,updated_at=? WHERE id=?', (score, tier, explanation, dumps(evidence), now(), candidate_id))
                else:
                    db.execute('INSERT INTO ai_search_candidates(id,session_id,work_id,score,tier,explanation,evidence_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                               (candidate_id, session['id'], work_id, score, tier, explanation, dumps(evidence), now(), now()))
                    accepted += 1
                db.execute('INSERT INTO search_candidate_details VALUES(?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET data_json=excluded.data_json', (candidate_id, session['runId'], dumps(data)))
        return accepted

    def runs(self, session_id):
        with self.library.db() as db:
            return [dict(r) for r in db.execute('SELECT id,state,created_at AS createdAt,completed_at AS completedAt FROM search_runs WHERE session_id=? ORDER BY rowid DESC', (session_id,))]

    def _current(self, session_id):
        with self.library.db() as db:
            rows = db.execute('SELECT c.*, d.data_json,w.title,w.authors_json,w.year,w.venue,w.abstract,w.doi,w.url,w.oa_url,w.citation_count FROM ai_search_candidates c JOIN ai_search_works w ON w.id=c.work_id LEFT JOIN search_candidate_details d ON d.candidate_id=c.id WHERE c.session_id=?', (session_id,)).fetchall()
            decisions = {r['work_id']: dict(r) for r in db.execute('SELECT * FROM search_decisions WHERE session_id=?', (session_id,))}
            values = []
            for r in rows:
                data = json.loads(r['data_json']) if r['data_json'] else {
                    'id': r['id'], 'workId': r['work_id'], 'title': r['title'], 'authors': json.loads(r['authors_json']), 'year': r['year'], 'venue': r['venue'],
                    'abstract': r['abstract'], 'doi': r['doi'], 'url': r['url'], 'oaUrl': r['oa_url'], 'citationCount': r['citation_count'],
                    'score': r['score'], 'tier': 'candidate', 'explanation': r['explanation'], 'evidence': json.loads(r['evidence_json']), 'checks': [], 'sources': [], 'screening': 'pending', 'fields': {}, 'verification': 'legacy'}
                data['importedItemId'] = r['imported_item_id']
                data['decision'] = decisions.get(r['work_id'])
                values.append(data)
            return values

    def _freeze(self, session_id, run_id):
        values = self._current(session_id)
        with self.library.db(True) as db:
            db.execute('UPDATE search_runs SET results_json=? WHERE id=?', (dumps(values), run_id))

    def cancel(self, payload):
        session_id = payload.get('sessionId')
        with self.library.db() as db:
            row = db.execute('SELECT state FROM ai_search_sessions WHERE id=?', (session_id,)).fetchone()
            verification_jobs = db.execute("SELECT id,payload FROM jobs WHERE kind='ai-search.verify' AND state IN ('pending','running')").fetchall()
        if row and row[0] == 'verifying':
            for job in verification_jobs:
                if json.loads(job['payload']).get('sessionId') == session_id:
                    self.jobs.action(job['id'], 'cancel')
            return {'ok': True}
        with self._lock:
            event = self._running.get(session_id)
            if event:
                event.set()
        with self.library.db(True) as db:
            row = db.execute('SELECT state FROM ai_search_sessions WHERE id=?', (session_id,)).fetchone()
            require(row and row['state'] in ('queued','running','expanding','linking'), '检索当前未在运行')
            # Pending jobs check this state on entry. Active jobs unwind at the next bounded request boundary.
            db.execute("UPDATE ai_search_sessions SET state='cancelling' WHERE id=?", (session_id,))
        return {'ok': True}

    def run(self, payload, progress):
        session_id = payload.get('sessionId')
        require(isinstance(session_id, str), '缺少检索任务标识')
        require(self.library.get_settings().get('online', True), '联网已关闭')
        event = threading.Event()
        with self._lock:
            require(session_id not in self._running and session_id not in self._verifying, '任务尚在运行或停止，请稍后重试')
            self._running[session_id] = event
        run_id = uid()
        completed, errors, accepted_total = 0, [], 0
        try:
            previous = self._current(session_id)
            previous_runs = self.runs(session_id)
            if previous and previous_runs:
                self._freeze(session_id, previous_runs[0]['id'])
            if previous and not self.runs(session_id):
                with self.library.db(True) as db:
                    old = db.execute('SELECT plan_json FROM ai_search_sessions WHERE id=?', (session_id,)).fetchone()
                    db.execute('INSERT INTO search_runs(id,session_id,plan_json,state,results_json,created_at,completed_at) VALUES(?,?,?,?,?,?,?)',
                               (uid(), session_id, old[0], 'legacy', dumps(previous), now(), now()))
            with self.library.db(True) as db:
                row = db.execute('SELECT * FROM ai_search_sessions WHERE id=?', (session_id,)).fetchone()
                require(row, '检索任务不存在')
                if row['state'] == 'cancelling':
                    raise AppError('CANCELLED', '检索已取消')
                session = self._session_value(row)
                session['plan'] = self._validate_plan(payload.get('plan') or session['plan'], session)
                session['sources'] = session['plan']['sources']
                session['runId'] = run_id
                db.execute("UPDATE search_runs SET state='interrupted',completed_at=? WHERE session_id=? AND state='running'", (now(), session_id))
                db.execute('INSERT INTO search_runs(id,session_id,plan_json,state,created_at) VALUES(?,?,?,?,?)', (run_id, session_id, dumps(session['plan']), 'running', now()))
                # Previous completed runs retain immutable result snapshots, not mutable global works.
                db.execute('DELETE FROM ai_search_candidates WHERE session_id=?', (session_id,))
                db.execute('DELETE FROM ai_search_source_runs WHERE session_id=?', (session_id,))
                db.execute("UPDATE ai_search_sessions SET state='running',result_count=0,updated_at=? WHERE id=?", (now(), session_id))
                for source in session['sources']:
                    db.execute('INSERT INTO ai_search_source_runs(id,session_id,source,state) VALUES(?,?,?,?)', (uid(), session_id, source, 'queued'))
            queries = [q for q in session['plan']['queries'] if q.get('enabled', True)]
            per_source = 28 if session['plan']['mode'] == 'deep' else 16
            def check():
                if event.is_set():
                    raise AppError('CANCELLED', '检索已取消；已完成结果已保留')
                require(self.library.get_settings().get('online', True), '联网已关闭，检索已停止')
            def fetch(source):
                count, received, failures, successful = 0, 0, [], 0
                for query in queries:
                    check()
                    qid, started = uid(), time.monotonic()
                    with self.library.db(True) as db:
                        db.execute('INSERT INTO search_query_runs(id,run_id,source,query,state) VALUES(?,?,?,?,?)', (qid, run_id, source, query['query'], 'running'))
                        db.execute("UPDATE ai_search_source_runs SET state='running',query=?,message=?,started_at=? WHERE session_id=? AND source=?", (query['query'], '正在查询 ' + query['label'], now(), session_id, source))
                    try:
                        records = self._source_records(source, [query['query']], per_source)
                        check()
                        count += self._store_source(session, source, records)
                        received += len(records)
                        successful += 1
                        with self.library.db(True) as db:
                            db.execute("UPDATE search_query_runs SET state='completed',received=?,truncated=?,elapsed_ms=? WHERE id=?", (len(records), int(len(records) >= per_source), int((time.monotonic()-started)*1000), qid))
                            db.execute('''INSERT INTO search_source_cursors(run_id,query_run_id,source,query,next_offset,page_size,has_more,updated_at)
                                VALUES(?,?,?,?,?,?,?,?)''', (run_id, qid, source, query['query'], len(records), per_source,
                                int(len(records) >= per_source), now()))
                    except Exception as exc:
                        if getattr(exc, 'code', '') == 'CANCELLED':
                            raise
                        failures.append({'query': query['query'], 'message': str(exc)[:400]})
                        with self.library.db(True) as db:
                            db.execute("UPDATE search_query_runs SET state='failed',error=?,elapsed_ms=? WHERE id=?", (str(exc)[:400], int((time.monotonic()-started)*1000), qid))
                with self.library.db(True) as db:
                    state = 'partial' if failures and successful else 'completed' if successful else 'failed'
                    db.execute('UPDATE ai_search_source_runs SET state=?,progress=1,received=?,accepted=?,message=?,error=?,completed_at=? WHERE session_id=? AND source=?',
                               (state, received, count, f'{successful}/{len(queries)} 条检索式完成；最多{per_source}条/式', dumps(failures) if failures else None, now(), session_id, source))
                return count, successful, failures
            progress(.08, '开始执行全部启用检索式')
            with ThreadPoolExecutor(max_workers=min(4, len(session['sources'])), thread_name_prefix='ai-source') as pool:
                futures = {pool.submit(fetch, source): source for source in session['sources']}
                try:
                    for future in as_completed(futures):
                        count, successful, failures = future.result()
                        completed += int(successful > 0)
                        accepted_total += count
                        errors.extend({'source': futures[future], **e} for e in failures)
                        progress(.8, '保存来源结果和逐条件证据')
                        check()
                except Exception:
                    event.set()
                    raise
            if not completed:
                raise AppError('AI_SEARCH_FAILED', '所有查询均失败，请查看来源错误后重试', retryable=True)
            self._freeze(session_id, run_id)
            total = len(self._current(session_id))
            with self.library.db(True) as db:
                state = 'partial' if errors else 'completed'
                db.execute('UPDATE search_runs SET state=?,completed_at=? WHERE id=?', (state, now(), run_id))
                db.execute('UPDATE ai_search_sessions SET state=?,result_count=?,updated_at=? WHERE id=?', (state, total, now(), session_id))
            progress(1, '候选与规则证据已保存；可选择论文进行AI核验')
            return {'sessionId': session_id, 'runId': run_id, 'sourcesCompleted': completed, 'resultCount': total, 'added': accepted_total, 'errors': errors}
        except Exception as exc:
            state = 'cancelled' if getattr(exc, 'code', '') == 'CANCELLED' else 'failed'
            self._freeze(session_id, run_id)
            with self.library.db(True) as db:
                db.execute('UPDATE search_runs SET state=?,completed_at=? WHERE id=?', (state, now(), run_id))
                db.execute("UPDATE search_query_runs SET state=? WHERE run_id=? AND state='running'", (state, run_id))
                db.execute("UPDATE ai_search_source_runs SET state=? WHERE session_id=? AND state IN ('queued','running')", (state, session_id))
                db.execute('UPDATE ai_search_sessions SET state=?,result_count=(SELECT count(*) FROM ai_search_candidates WHERE session_id=?),updated_at=? WHERE id=?', (state, session_id, now(), session_id))
            raise
        finally:
            with self._lock:
                self._running.pop(session_id, None)

    def submit_expand(self, payload):
        session_id = payload.get('sessionId')
        require(isinstance(session_id, str), '缺少检索任务标识')
        require(self.library.get_settings().get('online', True), '联网已关闭，请先在设置中开启')
        budget = bounded_int(payload.get('pageBudget'), 1, 1, 3)
        with self._lock:
            with self.library.db(True) as db:
                row = db.execute('SELECT state FROM ai_search_sessions WHERE id=?', (session_id,)).fetchone()
                require(row and row['state'] in ('completed', 'partial', 'failed', 'cancelled'), '先完成当前检索')
                run = db.execute('SELECT id FROM search_runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (session_id,)).fetchone()
                require(run, '没有可扩展的检索结果')
                available = db.execute('SELECT count(*) FROM search_source_cursors WHERE run_id=? AND has_more=1 AND next_offset<300', (run['id'],)).fetchone()[0]
                require(available, '已达到来源分页上限或无更多记录')
                db.execute("UPDATE ai_search_sessions SET state='queued',updated_at=? WHERE id=?", (now(), session_id))
            try:
                return self.jobs.create('ai-search.expand', {'sessionId': session_id, 'runId': run['id'], 'pageBudget': budget})
            except Exception:
                with self.library.db(True) as db:
                    db.execute('UPDATE ai_search_sessions SET state=? WHERE id=?', (row['state'], session_id))
                raise

    def expand(self, payload, progress):
        session_id, run_id = payload['sessionId'], payload['runId']
        event = threading.Event()
        with self._lock:
            require(session_id not in self._running, '检索任务正在运行')
            self._running[session_id] = event
        completed, accepted, failures = 0, 0, []
        try:
            with self.library.db(True) as db:
                row = db.execute('SELECT * FROM ai_search_sessions WHERE id=?', (session_id,)).fetchone()
                require(row and row['state'] != 'cancelling', '检索已取消')
                current = db.execute('SELECT id FROM search_runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (session_id,)).fetchone()
                require(current and current['id'] == run_id, '检索结果已更新，请重新查看')
                session = self._session_value(row)
                session['runId'] = run_id
                cursors = [dict(r) for r in db.execute('''SELECT * FROM search_source_cursors
                    WHERE run_id=? AND has_more=1 AND next_offset<300 ORDER BY updated_at,source,query_run_id LIMIT ?''',
                    (run_id, payload['pageBudget']))]
                db.execute("UPDATE ai_search_sessions SET state='expanding',updated_at=? WHERE id=?", (now(), session_id))
            last_call = {}
            delays = {'arxiv': 3.0, 'crossref': 1.0, 'pubmed': .4, 'openalex': .3}
            for index, cursor in enumerate(cursors):
                if event.is_set():
                    raise AppError('CANCELLED', '扩展检索已取消')
                require(self.library.get_settings().get('online', True), '联网已关闭，检索已停止')
                source, offset = cursor['source'], cursor['next_offset']
                wait = delays[source] - (time.monotonic() - last_call.get(source, 0))
                if wait > 0 and event.wait(wait):
                    raise AppError('CANCELLED', '扩展检索已取消')
                last_call[source] = time.monotonic()
                limit = min(cursor['page_size'], 300 - offset)
                try:
                    records = self._source_records(source, [cursor['query']], limit, offset)
                    if event.is_set():
                        raise AppError('CANCELLED', '扩展检索已取消')
                    accepted += self._store_source(session, source, records)
                    next_offset = offset + len(records)
                    has_more = len(records) == limit and next_offset < 300
                    with self.library.db(True) as db:
                        db.execute('''UPDATE search_source_cursors SET next_offset=?,has_more=?,error='',updated_at=?
                            WHERE run_id=? AND query_run_id=?''', (next_offset, int(has_more), now(), run_id, cursor['query_run_id']))
                        db.execute('''UPDATE search_query_runs SET received=received+?,truncated=? WHERE id=?''',
                                   (len(records), int(has_more), cursor['query_run_id']))
                    completed += 1
                except Exception as exc:
                    if getattr(exc, 'code', '') == 'CANCELLED':
                        raise
                    failures.append({'source': source, 'query': cursor['query'], 'message': str(exc)[:400]})
                    with self.library.db(True) as db:
                        db.execute('UPDATE search_source_cursors SET error=?,updated_at=? WHERE run_id=? AND query_run_id=?',
                                   (str(exc)[:400], now(), run_id, cursor['query_run_id']))
                progress((index + 1) / max(1, len(cursors)), f'已处理 {index + 1}/{len(cursors)} 个来源分页')
            self._freeze(session_id, run_id)
            total = len(self._current(session_id))
            with self.library.db(True) as db:
                state = 'partial' if failures else 'completed'
                db.execute('UPDATE search_runs SET state=?,completed_at=? WHERE id=?', (state, now(), run_id))
                db.execute('UPDATE ai_search_sessions SET state=?,result_count=?,updated_at=? WHERE id=?',
                           (state, total, now(), session_id))
            return {'sessionId': session_id, 'runId': run_id, 'pagesCompleted': completed, 'accepted': accepted, 'resultCount': total, 'errors': failures}
        except Exception as exc:
            self._freeze(session_id, run_id)
            with self.library.db(True) as db:
                db.execute('UPDATE ai_search_sessions SET state=?,result_count=(SELECT count(*) FROM ai_search_candidates WHERE session_id=?),updated_at=? WHERE id=?',
                           ('cancelled' if getattr(exc, 'code', '') == 'CANCELLED' else 'partial', session_id, now(), session_id))
            raise
        finally:
            with self._lock:
                self._running.pop(session_id, None)

    def submit_citations(self, payload):
        session_id, candidate_id = payload.get('sessionId'), payload.get('candidateId')
        direction = payload.get('direction')
        require(direction in ('citing', 'references'), '引用链方向不正确')
        require(self.library.get_settings().get('online', True), '联网已关闭')
        with self._lock:
            session = self.get(session_id)
            require(session['state'] in ('completed', 'partial'), '请先完成检索')
            candidate = self.evidence(candidate_id)
            require(candidate['runId'] == session['activeRunId'], '请选择当前版本的候选')
            identifier = next((v.get('sourceId', '').rsplit('/', 1)[-1] for v in candidate.get('variants', [])
                               if v.get('source') == 'openalex'), '')
            require(re.fullmatch(r'W\d+', identifier), '这篇文献暂无 OpenAlex 标识，无法扩展引用链')
            with self.library.db(True) as db:
                db.execute("UPDATE ai_search_sessions SET state='queued',updated_at=? WHERE id=?", (now(), session_id))
            try:
                return self.jobs.create('ai-search.citations', {'sessionId': session_id, 'runId': session['activeRunId'],
                                        'candidateId': candidate_id, 'openalexId': identifier, 'direction': direction,
                                        'previousState': session['state']})
            except Exception:
                with self.library.db(True) as db:
                    db.execute('UPDATE ai_search_sessions SET state=? WHERE id=?', (session['state'], session_id))
                raise

    def citations(self, payload, progress):
        session_id, run_id, anchor_id = payload['sessionId'], payload['runId'], payload['candidateId']
        with self._lock:
            require(session_id not in self._running, '检索任务正在运行')
            event = threading.Event()
            self._running[session_id] = event
        try:
            require(self.library.get_settings().get('online', True), '联网已关闭')
            session = self.get(session_id)
            if session['state'] == 'cancelling':
                raise AppError('CANCELLED', '引用链任务已取消')
            require(session['activeRunId'] == run_id, '检索版本已更新')
            with self.library.db(True) as db:
                db.execute("UPDATE ai_search_sessions SET state='linking',updated_at=? WHERE id=?", (now(), session_id))
            selector = 'cites' if payload['direction'] == 'citing' else 'cited_by'
            params = {'filter': selector + ':' + payload['openalexId'], 'per-page': '20',
                      'select': 'id,doi,title,publication_year,authorships,primary_location,cited_by_count,open_access,type,abstract_inverted_index'}
            progress(.1, '正在获取 OpenAlex 引用链第一页')
            data = self._json(SOURCE_ENDPOINTS['openalex'] + '?' + parse.urlencode(params), 'openalex')
            if event.is_set():
                raise AppError('CANCELLED', '引用链任务已取消')
            records = self._openalex_rows(data.get('results') or [])
            session['runId'] = run_id
            accepted = self._store_source(session, 'openalex', records)
            lookup = {v.get('sourceId'): row['id'] for row in self._current(session_id)
                      for v in row.get('variants', []) if v.get('source') == 'openalex'}
            with self.library.db(True) as db:
                for record in records:
                    related_id = lookup.get(record['sourceId'])
                    if related_id and related_id != anchor_id:
                        db.execute('''INSERT OR IGNORE INTO search_citation_edges(run_id,anchor_id,related_id,direction,created_at)
                            VALUES(?,?,?,?,?)''', (run_id, anchor_id, related_id, payload['direction'], now()))
                db.execute('UPDATE ai_search_sessions SET state=?,result_count=(SELECT count(*) FROM ai_search_candidates WHERE session_id=?),updated_at=? WHERE id=?',
                           (payload['previousState'], session_id, now(), session_id))
            self._freeze(session_id, run_id)
            progress(1, '引用链候选已加入当前检索')
            return {'received': len(records), 'accepted': accepted, 'direction': payload['direction'],
                    'hasMore': len(records) == 20, 'limit': 20}
        except Exception:
            with self.library.db(True) as db:
                db.execute('UPDATE ai_search_sessions SET state=?,updated_at=? WHERE id=?',
                           (payload.get('previousState', 'partial'), now(), session_id))
            raise
        finally:
            with self._lock:
                self._running.pop(session_id, None)

    def citation_links(self, payload):
        session = self.get(payload.get('sessionId'))
        run_id = payload.get('runId') or session['activeRunId']
        with self.library.db() as db:
            rows = [dict(r) for r in db.execute('''SELECT related_id AS relatedId,direction,source,created_at AS createdAt
                FROM search_citation_edges WHERE run_id=? AND anchor_id=? ORDER BY direction,created_at LIMIT 200''',
                (run_id, payload.get('candidateId')))]
        candidates = {r['id']: r for r in self._current(session['id'])}
        return [{**row, 'title': candidate.get('title', '历史候选'), 'year': candidate.get('year'),
                 'venue': candidate.get('venue'), 'doi': candidate.get('doi'),
                 'url': candidate.get('url'), 'available': bool(candidate)}
                for row in rows for candidate in [candidates.get(row['relatedId'], {})]]

    def submit_rerank(self, payload):
        session_id = payload.get('sessionId')
        session = self.get(session_id)
        require(session['state'] in ('completed', 'partial') and session['activeRunId'], '请先完成检索')
        return self.jobs.create('ai-search.rerank', {'sessionId': session_id, 'runId': session['activeRunId']})

    def rerank(self, payload, progress):
        session_id, run_id = payload['sessionId'], payload['runId']
        session = self.get(session_id)
        require(session['activeRunId'] == run_id, '检索版本已更新')
        records = self.results({'sessionId': session_id, 'limit': 20, 'sort': 'score'})['items']
        require(records, '当前没有可重排的候选')
        context = {'question': session['brief'], 'criteria': session['plan'].get('criteria', []),
                   'papers': [{'candidateId': r['id'], 'title': r['title'], 'abstract': (r.get('abstract') or '')[:1200]}
                              for r in records]}
        progress(.1, '仅发送前 20 篇的公开题名与摘要')
        response = self.assistant.search_rerank_json(context)
        raw_scores = response.get('scores')
        require(isinstance(raw_scores, list), '语义重排格式不正确')
        by_id = {r['id']: r for r in records}
        accepted = {}
        for row in raw_scores[:40]:
            if not isinstance(row, dict):
                continue
            candidate_id, score = row.get('candidateId'), row.get('score')
            quote = str(row.get('quote') or '').strip()[:500]
            reason = str(row.get('reason') or '').strip()[:500]
            source = by_id.get(candidate_id)
            if not source or type(score) not in (int, float) or not 0 <= score <= 1 or len(quote) < 8:
                continue
            if quote not in source['title'] and quote not in (source.get('abstract') or ''):
                continue
            accepted[candidate_id] = (float(score), quote, reason)
        require(accepted, '模型没有给出可核对的题名或摘要原句；保留原词汇排序')
        require(self.get(session_id)['activeRunId'] == run_id, '检索版本已更新')
        with self.library.db(True) as db:
            db.execute('DELETE FROM search_reranks WHERE run_id=?', (run_id,))
            db.executemany('INSERT INTO search_reranks(run_id,candidate_id,score,quote,reason,created_at) VALUES(?,?,?,?,?,?)',
                           [(run_id, candidate_id, *values, now()) for candidate_id, values in accepted.items()])
        progress(1, '语义建议已验证原句并保存；不改变硬条件判断')
        return {'scored': len(accepted), 'requested': len(records), 'runId': run_id}

    def results(self, payload):
        session_id = payload.get('sessionId')
        session = self.get(session_id)
        run_id = payload.get('runId')
        historical = bool(run_id and run_id != session['activeRunId'])
        run_plan = session['plan']
        if historical:
            with self.library.db() as db:
                row = db.execute('SELECT results_json,plan_json FROM search_runs WHERE id=? AND session_id=?', (run_id, session_id)).fetchone()
                require(row, '结果版本不存在')
                values = json.loads(row['results_json'])
                run_plan = json.loads(row['plan_json'])
        else:
            values = self._current(session_id)
        selected_run = run_id or session['activeRunId']
        with self.library.db() as db:
            reranks = {row['candidate_id']: {'score': row['score'], 'quote': row['quote'], 'reason': row['reason']}
                       for row in db.execute('SELECT candidate_id,score,quote,reason FROM search_reranks WHERE run_id=?', (selected_run,))}
        for row in values:
            row['semantic'] = reranks.get(row['id'])
        limit = bounded_int(payload.get('limit'), 50, 1, 200)
        offset = bounded_int(payload.get('offset'), 0, 0, 1000000)
        q = norm(payload.get('q'))
        for key, field in [('tier', 'tier'), ('screening', 'screening'), ('type', 'type')]:
            if payload.get(key) and payload[key] != 'all':
                values = [r for r in values if r.get(field) == payload[key]]
        if payload.get('source') and payload['source'] != 'all':
            values = [r for r in values if payload['source'] in r['sources']]
        if q:
            values = [r for r in values if q in norm(' '.join(str(r.get(k) or '') for k in ('title','abstract','venue','doi'))) ]
        for key, compare in [('yearFrom', lambda a,b:a>=b), ('yearTo',lambda a,b:a<=b)]:
            if payload.get(key):
                year = bounded_int(payload[key], 0, 1000, 9999)
                values = [r for r in values if r.get('year') and compare(r['year'], year)]
        if payload.get('oaOnly'):
            values = [r for r in values if r.get('oaUrl')]
        sort = payload.get('sort', 'screening')
        field = {'score':'score','year':'year','citations':'citationCount','title':'title'}.get(sort, 'score')
        reverse = payload.get('direction', 'asc' if sort == 'title' else 'desc') != 'asc'
        if sort == 'screening':
            # A keyword match cannot outrank a candidate with supported required criteria.
            # Within an unresolved group, prefer more independently supported
            # requirements before falling back to lexical score.
            rank = {'eligible': 2, 'pending': 1, 'excluded': 0}
            priority = [criterion['id'] for criterion in run_plan.get('criteria', []) if criterion.get('required')]
            def supported_in_order(row):
                checks = {check['id']: check['status'] for check in row.get('checks', [])}
                return tuple(checks.get(identifier) == 'pass' for identifier in priority)
            values.sort(key=lambda r: (
                rank.get(r.get('screening'), 1),
                supported_in_order(r),
                sum(c.get('status') == 'pass' for c in r.get('checks', []) if c.get('required')),
                any(c.get('id') == 'review' and c.get('required') and c.get('status') == 'pass'
                    for c in r.get('checks', [])),
                bool(r.get('abstract')),
                r.get('score') or 0, norm(r['title']),
            ), reverse=True)
        elif sort == 'semantic':
            values.sort(key=lambda r: (r['semantic'] is not None, r['semantic']['score'] if r['semantic'] else r['score'], norm(r['title'])), reverse=True)
        else:
            values.sort(key=lambda r: (norm(r[field]) if field == 'title' else r.get(field) or 0, norm(r['title'])), reverse=reverse)
        total = len(values)
        counts = {status: sum(r.get('screening') == status for r in values) for status in ('eligible','pending','excluded')}
        with self.library.db() as db:
            query_runs = [dict(r) for r in db.execute('SELECT source,query,state,received,truncated,error,elapsed_ms AS elapsedMs FROM search_query_runs WHERE run_id=? ORDER BY rowid', (run_id or session['activeRunId'],))]
            cursors = [dict(r) for r in db.execute('SELECT source,query,next_offset AS nextOffset,has_more AS hasMore,error FROM search_source_cursors WHERE run_id=? ORDER BY source,query', (run_id or session['activeRunId'],))]
        return {'items': values[offset:offset+limit], 'total': total, 'offset': offset, 'hasMore': offset+limit<total, 'counts': counts, 'historical': historical, 'queryRuns': query_runs, 'sourceCursors': cursors, 'semanticCount': len(reranks), 'runPlan': run_plan}

    def evidence(self, candidate_id):
        with self.library.db() as db:
            row = db.execute('SELECT session_id FROM ai_search_candidates WHERE id=?', (candidate_id,)).fetchone()
        require(row, '候选已不在当前版本，请刷新结果')
        candidate = next(r for r in self._current(row[0]) if r['id'] == candidate_id)
        with self.library.db() as db:
            semantic = db.execute('SELECT score,quote,reason FROM search_reranks WHERE run_id=? AND candidate_id=?',
                                  (candidate.get('runId'), candidate_id)).fetchone()
        candidate['semantic'] = dict(semantic) if semantic else None
        return candidate

    def intro(self, payload):
        """Translate public bibliographic fields on first opening, then reuse the saved result."""
        candidate_id = payload.get('candidateId')
        require(isinstance(candidate_id, str), '缺少候选文献标识')
        candidate = self.evidence(candidate_id)
        source = {'title': candidate['title'], 'abstract': candidate.get('abstract') or '',
                  'keywords': candidate.get('keywords') or []}
        source_hash = hashlib.sha256(dumps(source).encode('utf-8')).hexdigest()
        cached = candidate.get('introZh') or {}
        if cached.get('sourceHash') == source_hash:
            return candidate
        require(self.library.get_settings().get('online', True), '联网已关闭，原文仍可查看')
        require(self.assistant and self.assistant.status().get('ready'), '请先在设置中配置 AI 服务，原文仍可查看')
        translated = self.assistant.search_intro(source)
        intro = {'title': _text(translated.get('titleZh'), 1500),
                 'abstract': _text(translated.get('abstractZh'), 20000),
                 'keywords': [_text(value, 160) for value in translated.get('keywordsZh', [])],
                 'sourceHash': source_hash, 'translatedAt': now()}
        require(intro['title'] and (not source['abstract'] or intro['abstract']), 'AI 未返回完整的题名与摘要译文')
        require(len(intro['keywords']) == len(source['keywords']), 'AI 返回的关键词译文数量不一致')
        with self.library.db(True) as db:
            row = db.execute('SELECT data_json FROM search_candidate_details WHERE candidate_id=?', (candidate_id,)).fetchone()
            require(row, '候选已不在当前版本，请刷新结果')
            current = json.loads(row[0])
            current['introZh'] = intro
            db.execute('UPDATE search_candidate_details SET data_json=? WHERE candidate_id=?', (dumps(current), candidate_id))
        return self.evidence(candidate_id)

    def decision(self, payload):
        candidate = self.evidence(payload.get('candidateId'))
        decision = payload.get('decision')
        require(decision in ('include','exclude','pending'), '人工决定无效')
        with self.library.db(True) as db:
            session_id = db.execute('SELECT session_id FROM ai_search_candidates WHERE id=?', (candidate['id'],)).fetchone()[0]
            db.execute('INSERT INTO search_decisions VALUES(?,?,?,?,?) ON CONFLICT(session_id,work_id) DO UPDATE SET decision=excluded.decision,reason=excluded.reason,updated_at=excluded.updated_at',
                       (session_id, candidate['workId'], decision, _text(payload.get('reason'), 1000), now()))
        return self.evidence(candidate['id'])

    def submit_verify(self, payload):
        with self._lock:
            return self._submit_verify(payload)

    def _submit_verify(self, payload):
        require(payload.get('allowRemote') is True, '请明确选择将候选题名与摘要发送到已配置AI服务')
        require(self.library.get_settings().get('online', True), '联网已关闭')
        require(self.assistant and self.assistant.status().get('ready'), '请先配置并连接AI服务')
        ids = list(dict.fromkeys(payload.get('candidateIds') or []))
        require(0 < len(ids) <= 20, '每次选择1–20篇核验')
        with self.library.db(True) as db:
            rows = db.execute('SELECT DISTINCT session_id FROM ai_search_candidates WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids).fetchall()
            require(len(rows) == 1, '候选必须来自同一当前任务')
            sid = rows[0][0]
            row = db.execute('SELECT state FROM ai_search_sessions WHERE id=?', (sid,)).fetchone()
            require(row[0] not in ('queued','running','cancelling','verifying'), '请等待当前任务完成')
            db.execute("UPDATE ai_search_sessions SET state='verifying' WHERE id=?", (sid,))
        try:
            return self.jobs.create('ai-search.verify', {'sessionId': sid, 'candidateIds': ids, 'includeFulltext': payload.get('includeFulltext') is True})
        except Exception:
            with self.library.db(True) as db:
                db.execute("UPDATE ai_search_sessions SET state='failed' WHERE id=?", (sid,))
            raise

    def _pdf_evidence(self, data):
        """Only a user-selected imported item's indexed attachment, with exact page anchors."""
        import hashlib
        if not data.get('importedItemId'):
            return [], '尚未导入文献，未提供PDF材料'
        with self.library.db() as db:
            row = db.execute("SELECT id,text_json FROM attachments WHERE item_id=? AND text_status='indexed' ORDER BY created_at DESC LIMIT 1", (data['importedItemId'],)).fetchone()
        if not row:
            return [], '无已索引PDF，仍仅核验题名与摘要'
        pages = json.loads(row['text_json'] or '[]')
        result, budget = [], 30000
        for number, text in enumerate(pages[:40], 1):
            text = str(text or '')[:min(5000, budget)]
            if not text:
                continue
            result.append({'id': hashlib.sha256((row['id']+str(number)+text).encode()).hexdigest()[:20],
                           'kind': 'fulltext', 'label': f'PDF 第{number}页片段', 'source': 'local-pdf', 'text': text,
                           'attachmentId': row['id'], 'page': number, 'url': '', 'retrievedAt': now()})
            budget -= len(text)
            if budget <= 0:
                break
        return result, f'仅核验PDF前部片段：{len(result)}页，最多30000字符；非全文覆盖'

    def verify(self, payload, progress):
        sid = payload['sessionId']
        with self._lock:
            require(sid not in self._verifying and sid not in self._running, '任务尚在运行或停止，请稍后重试')
            self._verifying.add(sid)
        failures = []
        try:
            with self.library.db(True) as db:
                db.execute("UPDATE ai_search_sessions SET state='verifying' WHERE id=?", (sid,))
            for i, cid in enumerate(payload['candidateIds']):
                require(self.library.get_settings().get('online', True), '联网已关闭')
                progress(i / len(payload['candidateIds']), f'正在核验第 {i + 1}/{len(payload["candidateIds"])} 篇；不会推断未提供的全文')
                try:
                    data = self.evidence(cid)
                    # A later abstract-only request must not silently resend old private PDF excerpts.
                    data['evidence'] = [e for e in data['evidence'] if e['kind'] != 'fulltext']
                    data['coverageNote'] = '仅核验题名与摘要；未提供全文'
                    if payload.get('includeFulltext'):
                        extra, note = self._pdf_evidence(data)
                        data['evidence'].extend(extra)
                        data['coverageNote'] = note
                    raw = self.assistant.search_verify({'criteria': [{k:v for k,v in c.items() if k in ('id','label','required','kind')} for c in data['checks']], 'evidence': data['evidence']})
                    progress((i + .8) / len(payload['candidateIds']), '校验AI返回的原句证据')
                    checks, fields = verified_model(raw, data['checks'], data['evidence'])
                    data.update(checks=checks, fields=fields, screening=disposition(checks, data['evidence']), verification='model-with-validated-quotes')
                    data['tier'] = 'core' if data['screening'] == 'eligible' else 'candidate'
                    data.pop('verificationError', None)
                    with self.library.db(True) as db:
                        db.execute('UPDATE search_candidate_details SET data_json=? WHERE candidate_id=?', (dumps(data), cid))
                except Exception as exc:
                    if getattr(exc, 'code', '') == 'CANCELLED':
                        raise
                    failures.append({'candidateId': cid, 'message': str(exc)[:400]})
                    with self.library.db(True) as db:
                        row = db.execute('SELECT data_json FROM search_candidate_details WHERE candidate_id=?', (cid,)).fetchone()
                        if row:
                            prior = json.loads(row[0])
                            prior['verificationError'] = str(exc)[:400]
                            db.execute('UPDATE search_candidate_details SET data_json=? WHERE candidate_id=?', (dumps(prior), cid))
                progress((i + 1) / len(payload['candidateIds']),
                         f'已处理 {i + 1}/{len(payload["candidateIds"])} 篇；失败 {len(failures)} 篇')
            return {'failed': failures, 'verified': len(payload['candidateIds'])-len(failures)}
        finally:
            runs = self.runs(sid)
            if runs:
                self._freeze(sid, runs[0]['id'])
            with self.library.db(True) as db:
                prior_state = db.execute('SELECT state FROM search_runs WHERE session_id=? ORDER BY rowid DESC LIMIT 1', (sid,)).fetchone()
                db.execute('UPDATE ai_search_sessions SET state=?,updated_at=? WHERE id=?', (prior_state[0] if prior_state else 'completed', now(), sid))
            with self._lock:
                self._verifying.discard(sid)

    def import_candidates(self, payload):
        ids = list(dict.fromkeys(payload.get('candidateIds') or []))
        require(0 < len(ids) <= 100 and all(isinstance(value, str) for value in ids), '请选择1–100条检索结果')
        collection_id = payload.get('collectionId') or None
        imported, existing, missing = [], [], []
        def remember_sources(db, item_id, row):
            for url, origin in ((row.get('oa_url'), 'oa'), (row.get('url'), 'record')):
                url = _text(url, 2000)
                parts = parse.urlsplit(url)
                if parts.scheme not in ('http', 'https') or not parts.hostname:
                    continue
                db.execute('''INSERT INTO fulltext_sources(id,item_id,candidate_id,url,origin,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(item_id,url) DO UPDATE SET
                    candidate_id=COALESCE(fulltext_sources.candidate_id,excluded.candidate_id),
                    origin=CASE WHEN excluded.origin='oa' THEN 'oa' ELSE fulltext_sources.origin END''',
                    (uid(), item_id, row['id'], url, origin, now(), now()))
        with self.library.db(True) as db:
            if collection_id:
                require(db.execute('SELECT 1 FROM collections WHERE id=?', (collection_id,)).fetchone(), '集合不存在')
            rows = db.execute('''SELECT c.id,w.* FROM ai_search_candidates c JOIN ai_search_works w ON w.id=c.work_id
                WHERE c.id IN (''' + ','.join('?' for _ in ids) + ')', ids).fetchall()
            found = {row['id'] for row in rows}
            missing = [value for value in ids if value not in found]
            for raw_row in rows:
                row = dict(raw_row)
                detail = db.execute('SELECT data_json FROM search_candidate_details WHERE candidate_id=?', (row['id'],)).fetchone()
                if detail:
                    snap = json.loads(detail[0])
                    row.update({k: snap[k] for k in ('title','year','venue','abstract','doi','url','type')})
                    row['oa_url'] = snap.get('oaUrl', row['oa_url'])
                    row['authors_json'] = dumps(snap['authors'])
                    row['title_norm'] = norm(snap['title'])
                existing_item = db.execute('SELECT id FROM items WHERE deleted_at IS NULL AND (doi<>\'\' AND doi=? OR (doi=\'\' AND title_norm=? AND year=?)) LIMIT 1',
                                           (row['doi'], row['title_norm'], str(row['year'] or ''))).fetchone()
                if existing_item:
                    existing.append({'candidateId': row['id'], 'itemId': existing_item['id']})
                    db.execute('UPDATE ai_search_candidates SET imported_item_id=?,updated_at=? WHERE id=?', (existing_item['id'], now(), row['id']))
                    if collection_id:
                        db.execute('INSERT OR IGNORE INTO collection_items VALUES(?,?)', (collection_id, existing_item['id']))
                    remember_sources(db, existing_item['id'], row)
                    continue
                issued = {'date-parts': [[row['year']]]} if row['year'] else {}
                item = normalize({'title': row['title'], 'author': json.loads(row['authors_json']), 'issued': issued, 'container-title': row['venue'],
                                  'abstract': row['abstract'], 'DOI': row['doi'], 'PMID': row['pmid'], 'URL': row['url'], 'citationCount': row['citation_count'],
                                  'type': row['type'] or 'article-journal',
                                  'tags': ['AI 检索']})
                item_id = self.library._create(db, item, collection_id, 'ai-search')
                db.execute('UPDATE ai_search_candidates SET imported_item_id=?,updated_at=? WHERE id=?', (item_id, now(), row['id']))
                remember_sources(db, item_id, row)
                imported.append({'candidateId': row['id'], 'itemId': item_id})
        return {'imported': imported, 'existing': existing, 'missing': missing}
