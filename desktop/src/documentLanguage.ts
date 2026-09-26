type Bibliography = {title?: string; abstract?: string};
export type DocumentLanguage = 'zh' | 'other' | 'unknown';

function counts(value: string) {
  const han = (value.match(/\p{Script=Han}/gu) || []).length;
  const latin = (value.match(/[A-Za-z]/g) || []).length;
  return {han, latin, share: han / Math.max(1, han + latin)};
}

export function isChineseBibliography(value: Bibliography): boolean {
  const title = counts(value.title || '');
  if (title.han >= 2 && title.share >= .4) return true;
  const abstract = counts(value.abstract || '');
  return abstract.han >= 60 && abstract.share >= .4;
}

export function classifyDocumentLanguage(sample: string, filename: string): DocumentLanguage {
  const content = counts(sample.slice(0, 30000));
  if (content.han + content.latin >= 150) {
    if (content.han >= 70 && content.share >= .35) return 'zh';
    if (content.han < 30 || content.share < .08) return 'other';
  }
  if (isChineseBibliography({title: filename, abstract: sample})) return 'zh';
  return content.han + content.latin >= 150 ? 'other' : 'unknown';
}
