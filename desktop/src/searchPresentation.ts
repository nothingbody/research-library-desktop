export type SearchCheck = {
  label?: string;
  required?: boolean;
  status?: 'pass' | 'fail' | 'unknown';
  references?: Array<{quote?: string; evidenceId?: string}>;
};

export type SearchCandidate = {
  abstract?: string;
  checks?: SearchCheck[];
  evidence?: Array<{id?: string; kind?: string; source?: string}>;
  fields?: {summaryZh?: {text?: string}};
  explanation?: string;
  verification?: string;
};

export function candidatePresentation(candidate: SearchCandidate) {
  const required = (candidate.checks || []).filter(check => check.required);
  const passed = required.filter(check => check.status === 'pass').map(check => check.label || '未命名条件');
  const failed = required.filter(check => check.status === 'fail').map(check => check.label || '未命名条件');
  const unknown = required.filter(check => check.status !== 'pass' && check.status !== 'fail').map(check => check.label || '未命名条件');
  const hasAbstract = Boolean(candidate.abstract || candidate.evidence?.some(item => item.kind === 'abstract'));
  const hasFulltext = Boolean(candidate.evidence?.some(item => item.kind === 'fulltext'));
  const material = hasFulltext ? '已核验 PDF 片段' : hasAbstract ? '题名与摘要' : '仅题名';
  const modelSummary = candidate.verification?.startsWith('model') ? candidate.fields?.summaryZh?.text : undefined;
  let judgement: string;
  if (failed.length) judgement = `不符合必需条件：${failed.join('、')}`;
  else if (unknown.length) judgement = `${passed.length ? `已找到 ${passed.join('、')}；` : ''}待核验 ${unknown.join('、')}`;
  else if (passed.length) judgement = `已找到 ${passed.join('、')} 的题名或摘要依据`;
  else judgement = '未设必需条件；请结合题名和摘要判断';
  if (!hasAbstract && !hasFulltext) judgement += '；缺少摘要';
  const quoted = required.flatMap(check => check.references || []).find(ref => ref.quote && ref.quote.length >= 8);
  return {
    judgement,
    material,
    summary: modelSummary || candidate.explanation || '',
    quote: quoted?.quote || '',
    passed: passed.length,
    failed: failed.length,
    unknown: unknown.length,
  };
}
