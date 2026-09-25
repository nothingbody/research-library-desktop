"""Conservative, source-bound screening. Missing evidence is never a negative finding."""
import hashlib
import re
from .common import require

CONCEPTS = [
    ('distributed', '多车间 / 分布式生产', ['多车间', '多工厂', 'distributed', 'multi-workshop', 'multi-factory'],
     ['distributed', 'multi-workshop', 'multi workshop', 'multiple factories', 'multi-factory', 'multi factory', '多车间', '多工厂', '分布式'],
     ['single factory', 'single-factory', 'one factory only', 'single workshop', '单工厂', '单车间']),
    ('fjsp', '柔性作业车间', ['flexible job', 'fjsp', '柔性作业'],
     ['flexible job shop', 'flexible job-shop', 'fjsp', '柔性作业车间'], []),
    ('transport', 'AGV / 运输资源', ['agv', 'automated guided vehicle', '自动导引车', '运输资源', '生产与运输', 'production-transportation', 'transportation resource'],
     ['agv', 'automated guided vehicle', 'automated guided vehicles', 'transportation resource', 'transportation resources',
      'production-transportation', 'machine and agv', '自动导引车', '运输资源', '生产与运输'], []),
    ('multiobjective', '多目标优化', ['multi objective', 'multi-objective', '多目标'],
     ['multi-objective', 'multi objective', 'bi-objective', 'many-objective', '多目标', '双目标'], ['single objective only', '仅单目标']),
    ('surrogate', '代理评价模型', ['surrogate', '代理模型'],
     ['surrogate model', 'surrogate-assisted', 'surrogate assisted', 'metamodel', 'kriging', '代理模型'], []),
    ('review', '综述类论文', ['survey', 'review', '综述'],
     ['systematic review', 'literature review', 'a review', 'a survey', 'review of', 'survey of',
      'bibliometric review', 'state of the art', 'reviews the literature', '综述'], []),
]

def infer(brief):
    lower = brief.casefold()
    return [{'id': key, 'label': label, 'required': True, 'kind': 'include', 'terms': terms, 'negativeTerms': negatives}
            for key, label, triggers, terms, negatives in CONCEPTS if any(t in lower for t in triggers)]

def validate(criteria):
    require(isinstance(criteria, list) and len(criteria) <= 16, '核验条件最多16项')
    result, ids = [], set()
    for i, c in enumerate(criteria):
        require(isinstance(c, dict), '核验条件格式不正确')
        label = str(c.get('label') or '').strip()[:400]
        require(label, '请填写条件说明')
        identifier = str(c.get('id') or f'criterion-{i}')[:100]
        require(identifier not in ids, '条件标识重复')
        ids.add(identifier)
        def terms(key):
            values = c.get(key, [])
            require(isinstance(values, list) and len(values) <= 30, '同义词最多30项')
            return list(dict.fromkeys(str(v).strip().casefold()[:160] for v in values if str(v).strip()))
        result.append({'id': identifier, 'label': label, 'required': bool(c.get('required', True)),
                       'kind': 'exclude' if c.get('kind') == 'exclude' else 'include',
                       'terms': terms('terms'), 'negativeTerms': terms('negativeTerms')})
    return result

def fragments(variants):
    result = []
    for variant in sorted(variants, key=lambda x: x['source']):
        for field in ('title', 'abstract'):
            text = variant.get(field) or ''
            if not text:
                continue
            # Full abstract retained. Individual verifications must point into this source.
            identity = hashlib.sha256((variant['source'] + field + text).encode()).hexdigest()[:20]
            result.append({'id': identity, 'kind': field, 'label': '题名' if field == 'title' else '摘要',
                           'text': text, 'source': variant['source'], 'url': variant.get('url', ''), 'retrievedAt': variant.get('retrievedAt')})
    return result

def contains(text, term):
    if re.fullmatch(r'[a-z0-9 -]+', term):
        return bool(re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text.casefold()))
    return term.casefold() in text.casefold()

def screen(criteria, evidence):
    checks = []
    for criterion in criteria:
        positive, negative = [], []
        for fragment in evidence:
            for sentence in re.split(r'(?<=[.!?。；;])\s*', fragment['text']):
                if any(contains(sentence, term) for term in criterion['negativeTerms']):
                    negative.append({'evidenceId': fragment['id'], 'quote': sentence})
                elif any(contains(sentence, term) for term in criterion['terms']):
                    # A mention under explicit negation is not affirmative topic evidence.
                    if re.search(r'\b(not|without|exclud\w*|unlike)\b|不涉及|未考虑|没有', sentence, re.I):
                        continue
                    positive.append({'evidenceId': fragment['id'], 'quote': sentence})
        status, refs = 'unknown', []
        if positive and negative:
            reason = '来源中存在相反表述，需人工核查'
            refs = (positive + negative)[:4]
        elif negative:
            status, refs, reason = 'fail', negative[:3], '发现明确相反表述'
        elif positive:
            status, refs, reason = 'pass', positive[:3], '找到主题词原句；规则核验，不代表全文结论'
        else:
            reason = '题名或摘要未提供充分依据，不能推断全文不涉及'
        if criterion['kind'] == 'exclude':
            status = {'pass': 'fail', 'fail': 'unknown', 'unknown': 'unknown'}[status]
            reason = '命中排除主题' if status == 'fail' else '尚不能确认满足排除条件'
        checks.append({**criterion, 'status': status, 'reason': reason, 'references': refs, 'engine': 'rules-v2'})
    return checks

def disposition(checks, evidence=None):
    required = [c for c in checks if c['required']]
    if any(c['status'] == 'fail' for c in required):
        return 'excluded'
    if evidence is not None and not any(e['kind'] in ('abstract', 'fulltext') for e in evidence):
        return 'pending'
    if not required or any(c['status'] == 'unknown' for c in required):
        return 'pending'
    return 'eligible'

def verified_model(value, criteria, evidence):
    """Reject orphan IDs and invented quotes; unsupported judgements become unknown."""
    require(isinstance(value, dict), 'AI核验返回结构无效')
    by_id = {e['id']: e for e in evidence}
    def refs(raw):
        valid = []
        for r in raw if isinstance(raw, list) else []:
            if not isinstance(r, dict):
                continue
            e = by_id.get(r.get('evidenceId'))
            quote = str(r.get('quote') or '').strip()
            if e and len(quote) >= 8 and quote in e['text']:
                valid.append({'evidenceId': e['id'], 'quote': quote})
        return valid[:4]
    answers = {c.get('id'): c for c in value.get('checks', []) if isinstance(c, dict)}
    checks = []
    for c in criteria:
        a = answers.get(c['id'], {})
        references = refs(a.get('references'))
        status = a.get('status') if a.get('status') in ('pass', 'fail', 'unknown') else 'unknown'
        if not references:
            status = 'unknown'
        checks.append({**c, 'status': status, 'reason': str(a.get('reason') or '模型未提供可核验证据')[:1000],
                       'references': references, 'engine': 'configured-model'})
    fields = {}
    for key in ('summaryZh', 'task', 'method', 'objectives', 'constraints'):
        raw = value.get(key) or {}
        if isinstance(raw, dict):
            references = refs(raw.get('references'))
            if references and raw.get('text'):
                fields[key] = {'text': str(raw['text'])[:2500], 'references': references}
    return checks, fields
