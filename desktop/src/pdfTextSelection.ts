type Point = {x: number; y: number};
type Caret = {node: Text; offset: number; lineHeight: number; lineCenter: number};

function rowSpread(rectangles: DOMRect[]) {
  const centers = rectangles.filter(rect => rect.width > 1 && rect.height > 1)
    .map(rect => rect.top + rect.height / 2);
  return centers.length < 2 ? 0 : Math.max(...centers) - Math.min(...centers);
}

function nearestCaret(layer: HTMLElement, point: Point): Caret | null {
  const candidates = [...layer.querySelectorAll<HTMLSpanElement>('span')].flatMap(span => {
    const node = span.firstChild;
    if (!(node instanceof Text) || !node.length || span.getAttribute('role') === 'img') return [];
    const rect = span.getBoundingClientRect();
    if (rect.width <= 1 || rect.height <= 1) return [];
    const tolerance = Math.max(3, rect.height * .18);
    if (point.y < rect.top - tolerance || point.y > rect.bottom + tolerance) return [];
    const horizontal = Math.max(rect.left - point.x, point.x - rect.right, 0);
    const vertical = Math.abs(point.y - (rect.top + rect.bottom) / 2);
    // A paragraph indent can put the previous line directly under the
    // pointer's x coordinate. The line under the pointer must win first.
    return [{span, node, rect, score: vertical * 100 + horizontal}];
  }).sort((a, b) => a.score - b.score);
  const candidate = candidates[0];
  if (!candidate) return null;
  const {span, node, rect} = candidate;
  const lineCenter = rect.top + rect.height / 2;
  if (point.x <= rect.left) return {node, offset: 0, lineHeight: rect.height, lineCenter};
  if (point.x >= rect.right) return {node, offset: node.length, lineHeight: rect.height, lineCenter};
  const caret = document.caretRangeFromPoint(point.x, Math.min(rect.bottom - 1, Math.max(rect.top + 1, point.y)));
  if (caret && span.contains(caret.startContainer) && caret.startContainer instanceof Text)
    return {node: caret.startContainer, offset: caret.startOffset, lineHeight: rect.height, lineCenter};
  const range = document.createRange();
  let low = 0, high = node.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    range.setStart(node, 0); range.setEnd(node, middle + 1);
    if (range.getBoundingClientRect().right < point.x) low = middle + 1;
    else high = middle;
  }
  return {node, offset: low, lineHeight: rect.height, lineCenter};
}

// A small vertical wobble during a horizontal drag can make Chromium select
// the neighbouring PDF line (or everything between two text-stream nodes).
// Snap only short vertical movements back to the row where the drag began.
export function normalizePdfLineSelection(layer: HTMLElement, selection: Selection, start: Point | null, end: Point) {
  if (!start || !selection.rangeCount) return false;
  const startCaret = nearestCaret(layer, start);
  if (!startCaret) return false;
  const verticalDistance = Math.abs(start.y - end.y);
  if (Math.abs(start.x - end.x) < Math.max(24, verticalDistance * 3) ||
      verticalDistance > Math.max(12, startCaret.lineHeight * 1.15)) return false;
  const pointedEnd = nearestCaret(layer, end);
  if (pointedEnd && Math.abs(pointedEnd.lineCenter - startCaret.lineCenter) > startCaret.lineHeight * .65 &&
      Math.abs(start.y - startCaret.lineCenter) < startCaret.lineHeight * .28 &&
      Math.abs(end.y - pointedEnd.lineCenter) < pointedEnd.lineHeight * .28) return false;
  const endCaret = nearestCaret(layer, {x: end.x, y: startCaret.lineCenter});
  if (!startCaret || !endCaret) return false;
  const lineHeight = Math.max(startCaret.lineHeight, endCaret.lineHeight);
  const corrected = document.createRange();
  corrected.setStart(startCaret.node, startCaret.offset);
  corrected.setEnd(endCaret.node, endCaret.offset);
  if (corrected.collapsed && (startCaret.node !== endCaret.node || startCaret.offset !== endCaret.offset)) {
    corrected.setStart(endCaret.node, endCaret.offset);
    corrected.setEnd(startCaret.node, startCaret.offset);
  }
  if (corrected.collapsed || rowSpread([...corrected.getClientRects()]) > Math.max(10, lineHeight * .85)) return false;
  if (!selection.isCollapsed && selection.toString() === corrected.toString() &&
      rowSpread([...selection.getRangeAt(0).getClientRects()]) <= Math.max(10, lineHeight * .85)) return false;
  selection.removeAllRanges(); selection.addRange(corrected);
  return true;
}
