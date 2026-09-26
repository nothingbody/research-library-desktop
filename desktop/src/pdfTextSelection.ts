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

// Chromium maps a pointer that leaves the page (e.g. overshooting the right
// margin) to the start of the text layer, so the selection flips backwards and
// covers the lines above the anchor. It also only fires the page's own mouseup
// when released on the page. Track the drag at document level instead: keep
// the focus on the nearest caret while the pointer is outside the page, and
// always hand the release back to the caller.
export function trackPdfSelectionDrag(layer: HTMLElement, page: HTMLElement, onRelease: (event: MouseEvent) => void) {
  let frame = 0;
  const controller = new AbortController();
  const {signal} = controller;
  document.addEventListener('mousemove', event => {
    const bounds = page.getBoundingClientRect();
    if (event.clientX >= bounds.left && event.clientX <= bounds.right && event.clientY >= bounds.top && event.clientY <= bounds.bottom) return;
    // Over another page's text layer (continuous mode): a cross-page selection is intended.
    if (document.elementFromPoint(event.clientX, event.clientY)?.closest('.textLayer')) return;
    cancelAnimationFrame(frame);
    // Run after Chromium has applied its own drag-selection update.
    frame = requestAnimationFrame(() => {
      const selection = window.getSelection();
      if (!selection?.rangeCount || !layer.contains(selection.anchorNode)) return;
      const point = {x: Math.min(bounds.right - 1, Math.max(bounds.left + 1, event.clientX)),
        y: Math.min(bounds.bottom - 1, Math.max(bounds.top + 1, event.clientY))};
      const caret = nearestCaret(layer, point);
      if (caret) selection.extend(caret.node, caret.offset);
    });
  }, {signal});
  window.addEventListener('mouseup', event => {
    cancelAnimationFrame(frame);
    controller.abort();
    onRelease(event);
  }, {signal, once: true});
  window.addEventListener('blur', () => {cancelAnimationFrame(frame); controller.abort();}, {signal, once: true});
}
// Mirrors pdf.js TextLayerBuilder's selection guard (web/text_layer_builder.js).
// The core TextLayer class does not add it; without it, dragging across the
// gaps between absolutely positioned spans lets Chromium snap the selection
// focus to a neighbouring line.
const guardedLayers = new Map<HTMLElement, HTMLDivElement>();
let guardListeners: AbortController | null = null;

function resetLayer(layer: HTMLElement, end: HTMLDivElement) {
  layer.append(end);
  layer.classList.remove('selecting');
}

function resetAll() {
  guardedLayers.forEach((end, layer) => resetLayer(layer, end));
}

function enableGuardListeners() {
  if (guardListeners) return;
  guardListeners = new AbortController();
  const {signal} = guardListeners;
  let pointerDown = false;
  document.addEventListener('pointerdown', () => {pointerDown = true;}, {signal});
  document.addEventListener('pointerup', () => {pointerDown = false; resetAll();}, {signal});
  window.addEventListener('blur', () => {pointerDown = false; resetAll();}, {signal});
  document.addEventListener('keyup', () => {if (!pointerDown) resetAll();}, {signal});
  document.addEventListener('selectionchange', () => {
    const selection = document.getSelection();
    if (!selection?.rangeCount) {resetAll(); return;}
    const range = selection.getRangeAt(0);
    guardedLayers.forEach((end, layer) => {
      if (range.intersectsNode(layer)) layer.classList.add('selecting');
      else resetLayer(layer, end);
    });
  }, {signal});
}

// Call after TextLayer.render() resolves; call the returned function on cleanup.
export function attachSelectionGuard(layer: HTMLElement): () => void {
  const end = document.createElement('div');
  end.className = 'endOfContent';
  layer.append(end);
  const onMouseDown = () => layer.classList.add('selecting');
  layer.addEventListener('mousedown', onMouseDown);
  guardedLayers.set(layer, end);
  enableGuardListeners();
  return () => {
    layer.removeEventListener('mousedown', onMouseDown);
    layer.classList.remove('selecting');
    end.remove();
    guardedLayers.delete(layer);
    if (!guardedLayers.size) {guardListeners?.abort(); guardListeners = null;}
  };
}

type Box = {left: number; top: number; right: number; bottom: number};

// One box per visual line. Range.getClientRects() returns the span box and
// the text box for fully selected spans; CJK punctuation and Latin runs overlap.
export function mergeSelectionRects(rects: Iterable<DOMRectReadOnly>): Box[] {
  const boxes = [...rects].filter(r => r.width > 1 && r.height > 1)
    .map(r => ({left: r.left, top: r.top, right: r.right, bottom: r.bottom}))
    .sort((a, b) => a.left - b.left);
  const lines: {boxes: Box[]; top: number; bottom: number; right: number}[] = [];
  for (const box of boxes) {
    const mid = (box.top + box.bottom) / 2, height = box.bottom - box.top;
    const line = lines.find(l => {
      const lineMid = (l.top + l.bottom) / 2;
      const sameRow = (mid > l.top && mid < l.bottom) || (lineMid > box.top && lineMid < box.bottom);
      return sameRow && box.left - l.right < Math.max(height, l.bottom - l.top) * 1.5;
    });
    if (line) {line.boxes.push(box); line.right = Math.max(line.right, box.right);}
    else lines.push({boxes: [box], top: box.top, bottom: box.bottom, right: box.right});
  }
  return lines.map(line => {
    // Superscripts and number runs may be taller; use the widest body-text run.
    const main = line.boxes.reduce((a, b) => (b.right - b.left > a.right - a.left ? b : a));
    return {left: Math.min(...line.boxes.map(b => b.left)), right: Math.max(...line.boxes.map(b => b.right)), top: main.top, bottom: main.bottom};
  }).sort((a, b) => a.top - b.top || a.left - b.left);
}
