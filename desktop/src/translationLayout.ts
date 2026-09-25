export type TranslationLine = {
  text: string;
  left: number;
  right: number;
  top: number;
  bottom: number;
  baseline: number;
  fontSize: number;
};

export type TranslationParagraph = {
  id: string;
  text: string;
  left: number;
  top: number;
  width: number;
  height: number;
  fontSize: number;
  lineCount: number;
};

export type TranslationLayout = {
  lines: TranslationLine[];
  paragraphs: TranslationParagraph[];
};

type TextItem = {str?: string; transform?: number[]; width?: number; height?: number};
type TextContent = {items?: readonly unknown[]};
type Viewport = {convertToViewportPoint: (x: number, y: number) => number[]};
type Glyph = {text: string; left: number; right: number; baseline: number; fontSize: number};

function sameRow(a: Glyph, b: Glyph, scale: number) {
  return Math.abs(a.baseline - b.baseline) <= Math.max(2.7 * scale, Math.max(a.fontSize, b.fontSize) * .32);
}

function joinText(parts: Glyph[]) {
  let result = '';
  let previous: Glyph | undefined;
  for (const part of parts) {
    const gap = previous ? part.left - previous.right : 0;
    if (result && gap > Math.max(1.5, part.fontSize * .17) && !/\s$/.test(result) && !/^\s/.test(part.text)) result += ' ';
    result += part.text;
    previous = part;
  }
  return result.replace(/\s+/g, ' ').trim();
}

function paragraphText(lines: TranslationLine[]) {
  return lines.reduce((text, line) => {
    if (!text) return line.text;
    if (/[A-Za-z]-$/.test(text)) return text.slice(0, -1) + line.text;
    return text + ' ' + line.text;
  }, '');
}

export function buildTranslationLayout(content: TextContent, viewport: Viewport, scale: number): TranslationLayout {
  const glyphs: Glyph[] = [];
  for (const entry of content.items || []) {
    const item = entry as TextItem;
    const value = item.str || '';
    if (!value.trim() || !item.transform || item.transform.length < 6) continue;
    const [left, baseline] = viewport.convertToViewportPoint(item.transform[4], item.transform[5]);
    const fontSize = Math.max(2 * scale, Math.abs(item.height || item.transform[3] || 9) * scale);
    const width = Math.max(fontSize * .2, Math.abs(item.width || 0) * scale);
    glyphs.push({text: value, left, right: left + width, baseline, fontSize});
  }
  glyphs.sort((a, b) => a.baseline - b.baseline || a.left - b.left);

  const rows: Glyph[][] = [];
  for (const glyph of glyphs) {
    const row = rows.at(-1);
    if (row && sameRow(row[0], glyph, scale)) row.push(glyph);
    else rows.push([glyph]);
  }
  const lines: TranslationLine[] = [];
  for (const row of rows) {
    row.sort((a, b) => a.left - b.left);
    let parts: Glyph[] = [];
    const flush = () => {
      if (!parts.length) return;
      const text = joinText(parts), fontSize = Math.max(...parts.map(part => part.fontSize));
      const baseline = Math.max(...parts.map(part => part.baseline));
      if (text) lines.push({
        text, left: Math.min(...parts.map(part => part.left)),
        right: Math.max(...parts.map(part => part.right)),
        top: Math.max(0, baseline - fontSize * 1.08),
        bottom: baseline + fontSize * .28, baseline, fontSize,
      });
      parts = [];
    };
    for (const glyph of row) {
      const previous = parts.at(-1);
      const gap = previous ? glyph.left - previous.right : 0;
      const inlineContinuation = previous && gap <= Math.max(previous.fontSize, glyph.fontSize) * 3 &&
        (/^(?:abstract|keywords?|摘要|关键词)\s*[:：]$/i.test(joinText(parts)) || /^(?:and|et|&|,|，)\b/i.test(glyph.text));
      if (previous && gap > Math.max(8 * scale, Math.max(previous.fontSize, glyph.fontSize) * 1.25) && !inlineContinuation) flush();
      parts.push(glyph);
    }
    flush();
  }
  lines.sort((a, b) => a.top - b.top || a.left - b.left);

  const complexBaselines = [...new Set(lines.filter(line =>
    lines.filter(peer => Math.abs(peer.baseline - line.baseline) <= 3 * scale).length >= 3
  ).map(line => line.baseline))].sort((a, b) => a - b);
  const protectedBands: {start: number; end: number}[] = [];
  for (const baseline of complexBaselines) {
    const band = protectedBands.at(-1);
    if (band && baseline - band.end <= 24 * scale) band.end = baseline;
    else protectedBands.push({start: baseline, end: baseline});
  }
  for (const band of protectedBands) {band.start -= 14 * scale; band.end += 14 * scale;}
  const readableLines = lines.filter(line => {
    if (protectedBands.some(band => line.bottom >= band.start && line.top <= band.end)) return false;
    const letters = (line.text.match(/[A-Za-z\p{Script=Han}]/gu) || []).length;
    return letters >= 3 || !/[=<>∑{}]/.test(line.text);
  });
  const groups: TranslationLine[][] = [];
  for (const line of readableLines) {
    let best = -1, bestGap = Infinity;
    for (let index = 0; index < groups.length; index++) {
      const group = groups[index], last = group.at(-1)!;
      const gap = line.baseline - last.baseline;
      if (gap <= 0 || gap > Math.max(line.fontSize, last.fontSize) * 1.85) continue;
      if (Math.max(line.fontSize, last.fontSize) / Math.min(line.fontSize, last.fontSize) > 1.35) continue;
      const left = Math.min(...group.map(row => row.left));
      const right = Math.max(...group.map(row => row.right));
      const overlap = Math.max(0, Math.min(line.right, right) - Math.max(line.left, left));
      if (overlap < Math.min(line.right - line.left, right - left) * .45 && Math.abs(line.left - left) > line.fontSize * 2.4) continue;
      if (group.length > 1 && line.left > left + line.fontSize * 1.55) continue;
      if (/^[1-9]\d?\s+[A-Z]/.test(line.text) && /^[1-9]\d?\s+[A-Z]/.test(group[0].text)) continue;
      if (/^\d+(?:\.\d+)*\.\s+[A-Z]/.test(group.at(-1)!.text)) continue;
      if (gap < bestGap) {best = index; bestGap = gap;}
    }
    if (best < 0) groups.push([line]);
    else groups[best].push(line);
  }
  const safeGroups = groups.filter(group => group.some(line => /[A-Za-z\p{Script=Han}]/u.test(line.text)));
  const candidates = safeGroups.map(group => {
    const left = Math.min(...group.map(line => line.left));
    const right = Math.max(...group.map(line => line.right));
    const top = Math.min(...group.map(line => line.top));
    const bottom = Math.max(...group.map(line => line.bottom));
    return {
      id: '', text: paragraphText(group), left, top,
      width: right - left, height: bottom - top,
      fontSize: Math.max(...group.map(line => line.fontSize)),
      lineCount: group.length,
    };
  });
  const preserve = new Set<number>();
  for (let first = 0; first < candidates.length; first++) for (let second = first + 1; second < candidates.length; second++) {
    const a = candidates[first], b = candidates[second];
    const horizontal = Math.max(0, Math.min(a.left + a.width, b.left + b.width) - Math.max(a.left, b.left));
    const vertical = Math.max(0, Math.min(a.top + a.height, b.top + b.height) - Math.max(a.top, b.top));
    const smallerArea = Math.min(a.width * a.height, b.width * b.height);
    if (vertical > 6 * scale && horizontal * vertical > smallerArea * .2) {
      preserve.add(first); preserve.add(second);
    }
  }
  const paragraphs = candidates.filter((_, index) => !preserve.has(index));
  paragraphs.forEach((paragraph, index) => {paragraph.id = String(index + 1);});
  return {lines: safeGroups.filter((_, index) => !preserve.has(index)).flat(), paragraphs};
}

export function findSelectedParagraph(paragraphs: TranslationParagraph[], rectangles: number[][], quote: string) {
  const ranked = paragraphs.map(paragraph => {
    const right = paragraph.left + paragraph.width, bottom = paragraph.top + paragraph.height;
    const overlap = rectangles.reduce((score, rect) => {
      if (rect.length < 4) return score;
      return score + Math.max(0, Math.min(right, rect[2]) - Math.max(paragraph.left, rect[0])) *
        Math.max(0, Math.min(bottom, rect[3]) - Math.max(paragraph.top, rect[1]));
    }, 0);
    return {paragraph, overlap};
  }).sort((a, b) => b.overlap - a.overlap);
  if (ranked[0]?.overlap > 0) return ranked[0].paragraph;
  const needle = quote.replace(/\s+/g, ' ').trim().toLowerCase();
  return needle ? paragraphs.find(paragraph => paragraph.text.toLowerCase().includes(needle)) || null : null;
}

export function findAlignedRange(source: string, quote: string, translation: string, target: string, positionRatio?: number) {
  if (!target || !translation.includes(target)) return null;
  if (quote.trim().length < source.trim().length * .7 &&
      target.length > Math.min(translation.length * .7, Math.max(18, quote.trim().length * 1.6 + 8))) return null;
  const normalizedSource = source.replace(/\s+/g, ' ').toLowerCase();
  const normalizedQuote = quote.replace(/\s+/g, ' ').trim().toLowerCase();
  let sourceOffset = -1, sourceDistance = Infinity, next = 0;
  while (normalizedQuote && (next = normalizedSource.indexOf(normalizedQuote, next)) >= 0) {
    const distance = Math.abs(next / Math.max(1, normalizedSource.length) - (positionRatio ?? 0));
    if (sourceOffset < 0 || (positionRatio !== undefined && distance < sourceDistance)) {
      sourceOffset = next; sourceDistance = distance;
    }
    next += Math.max(1, normalizedQuote.length);
  }
  const ratio = sourceOffset >= 0 ? sourceOffset / Math.max(1, normalizedSource.length) : (positionRatio ?? 0);
  let best = -1, distance = Infinity, offset = 0;
  while ((offset = translation.indexOf(target, offset)) >= 0) {
    const delta = Math.abs(offset / Math.max(1, translation.length) - ratio);
    if (delta < distance) {best = offset; distance = delta;}
    offset += Math.max(1, target.length);
  }
  return best < 0 ? null : {start: best, end: best + target.length};
}
