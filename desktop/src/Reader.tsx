import {useCallback, useEffect, useLayoutEffect, useRef, useState, type MouseEvent as ReactMouseEvent, type UIEvent as ReactUIEvent, type WheelEvent as ReactWheelEvent} from 'react';
import {getDocument, GlobalWorkerOptions, TextLayer, type PDFDocumentProxy} from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import 'pdfjs-dist/web/pdf_viewer.css';
import {CaretLeft, CaretRight, MagnifyingGlass, Highlighter, TextUnderline, Selection, Cursor, Minus, Plus, ArrowClockwise, DownloadSimple, Notebook, Trash, List, SquaresFour, ArrowSquareOut, Sparkle, Translate} from '@phosphor-icons/react';
import {api, files, type Data, useErrorText} from './api';
import {Busy, Empty, ErrorBox, Modal} from './ui';
import {ReadingAssistant} from './ReadingAssistant';
import {ContinuousPage} from './ContinuousPage';
import {BilingualContinuousPage} from './BilingualContinuousPage';
import {TranslationPage} from './TranslationPage';
import {normalizePdfLineSelection} from './pdfTextSelection';
import './readerOcr.css';
import './readerSelection.css';
GlobalWorkerOptions.workerSrc = workerUrl;

function Thumbnail({pdf, page, active, click}: {pdf: PDFDocumentProxy; page: number; active: boolean; click: () => void}) {
  const ref = useRef<HTMLCanvasElement>(null), box = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    let cancelled = false, task: any;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(e => e.isIntersecting)) {
        observer.disconnect();
        pdf.getPage(page).then(p => {if (cancelled || !ref.current) return; const v = p.getViewport({scale: 120 / p.getViewport({scale: 1}).width}); const canvas = ref.current; canvas.width = v.width; canvas.height = v.height; task = p.render({canvas, viewport: v}); return task.promise;}).catch(() => {});
      }
    });
    if (box.current) observer.observe(box.current);
    return () => {cancelled = true; observer.disconnect(); task?.cancel();};
  }, [pdf, page]);
  return <button ref={box} className={'thumbnail ' + (active ? 'active' : '')} onClick={click}><canvas ref={ref}/><span>{page}</span></button>;
}

export function Reader({attachmentId, jumpPage, jumpAnnotation, stamp, notify, fail, onNote, onResearchAsk}: {attachmentId: string; jumpPage?: number; jumpAnnotation?: string; stamp?: number; notify: (message: string) => void; fail: (e: any) => void; onNote: () => void; onResearchAsk: (itemId: string, question: string) => void}) {
  const [record, setRecord] = useState<Data | null>(null), [pdf, setPdf] = useState<PDFDocumentProxy | null>(null), [page, setPage] = useState(1), [scale, setScale] = useState(1), [rotation, setRotation] = useState(0);
  const [mode,setMode]=useState<'single'|'continuous'>('continuous');
  const [bilingual, setBilingual] = useState(true);
  const [navCollapsed,setNavCollapsed] = useState(false), [annotationsCollapsed,setAnnotationsCollapsed] = useState(false), [rightPanel, setRightPanel] = useState('annotations');
  const [error, setError] = useState(''), [loading, setLoading] = useState(true), [rendering, setRendering] = useState(false), [annotations, setAnnotations] = useState<Data[]>([]), [outline, setOutline] = useState<any[]>([]), [left, setLeft] = useState('thumb');
  const [tool, setTool] = useState('select'), [color, setColor] = useState('#f6d766'), [viewport, setViewport] = useState<any>(null), [active, setActive] = useState<Data | null>(null), [comment, setComment] = useState('');
  const [selection, setSelection] = useState<Data | null>(null), [area, setArea] = useState<number[] | null>(null), [search, setSearch] = useState(''), [matches, setMatches] = useState<Data[]>([]), [searching, setSearching] = useState(false), [password, setPassword] = useState<Data | null>(null), [passwordText, setPasswordText] = useState('');
  const [annotationDraft, setAnnotationDraft] = useState<Data | null>(null), [annotationComment, setAnnotationComment] = useState(''), [noteDraft, setNoteDraft] = useState<Data | null>(null), [noteBusy, setNoteBusy] = useState(false);
  const [selectionPosition, setSelectionPosition] = useState<{left: number; top: number; visible: boolean} | null>(null);
  const canvas = useRef<HTMLCanvasElement>(null), text = useRef<HTMLDivElement>(null), paper = useRef<HTMLDivElement>(null), readerRoot = useRef<HTMLDivElement>(null), pdfScroll = useRef<HTMLDivElement>(null), start = useRef<number[] | null>(null), searchRun = useRef(0), readingClock = useRef(Date.now());
  const readerBody = useRef<HTMLDivElement>(null), selectionActions = useRef<HTMLDivElement>(null);
  const wheelAmount = useRef(0), wheelDirection = useRef(0), wheelTime = useRef(0), pendingWheelPosition = useRef<'top' | 'bottom' | null>(null);
  const selectionStart = useRef<{x: number; y: number} | null>(null);
  const wheelScale = useRef(scale);
  useEffect(() => {wheelScale.current = scale;}, [scale]);
  const zoomAnchor = useRef<{element: HTMLElement; x: number; y: number; clientX: number; clientY: number; width: number; oldScale: number; scale: number} | null>(null);
  useEffect(() => {
    const container = pdfScroll.current;
    if (!container) return;
    const zoom = (event: WheelEvent) => {
      if (!event.ctrlKey || !pdf || !event.deltaY) return;
      event.preventDefault();
      event.stopPropagation();
      const next = Math.max(.25, Math.min(4, Math.round(wheelScale.current * Math.exp(-event.deltaY * .0014) * 1000) / 1000));
      if (next === wheelScale.current) return;
      wheelScale.current = next;
      zoomAnchor.current = null;
      const target = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>('.continuous-page, .translated-paper, .pdf-paper');
      if (target && container.contains(target)) {
        const bounds = target.getBoundingClientRect();
        zoomAnchor.current = {element: target, x: (event.clientX - bounds.left) / bounds.width,
          y: (event.clientY - bounds.top) / bounds.height, clientX: event.clientX, clientY: event.clientY,
          width: bounds.width, oldScale: scale, scale: next};
      }
      setSelection(null);
      window.getSelection()?.removeAllRanges();
      setScale(next);
    };
    container.addEventListener('wheel', zoom, {passive: false});
    return () => container.removeEventListener('wheel', zoom);
  }, [pdf, scale, loading]);
  useEffect(() => {
    const anchor = zoomAnchor.current, container = pdfScroll.current;
    if (!anchor || !container || anchor.scale !== scale) return;
    const expectedWidth = anchor.width * scale / anchor.oldScale;
    const apply = () => {
      if (!anchor.element.isConnected || zoomAnchor.current !== anchor) return;
      const bounds = anchor.element.getBoundingClientRect();
      if (Math.abs(bounds.width - expectedWidth) > Math.max(2, expectedWidth * .015)) return;
      container.scrollLeft += bounds.left + bounds.width * anchor.x - anchor.clientX;
      container.scrollTop += bounds.top + bounds.height * anchor.y - anchor.clientY;
      zoomAnchor.current = null;
    };
    const observer = new ResizeObserver(apply);
    observer.observe(anchor.element);
    const frame = requestAnimationFrame(apply);
    const timer = window.setTimeout(() => {observer.disconnect(); if (zoomAnchor.current === anchor) zoomAnchor.current = null;}, 1200);
    return () => {observer.disconnect(); cancelAnimationFrame(frame); window.clearTimeout(timer);};
  }, [scale]);
  useLayoutEffect(() => {
    const body = readerBody.current, pane = pdfScroll.current, actions = selectionActions.current;
    const rect = selection?.viewportRects?.[0] as number[] | undefined;
    if (!body || !pane || !actions || !rect || !selection?.quote?.trim()) {setSelectionPosition(null); return;}
    const sourcePage = mode === 'single' ? paper.current : pane.querySelector<HTMLElement>(`.continuous-page[data-page="${selection.page}"]`);
    if (!sourcePage || (mode === 'single' && selection.page !== page)) {setSelectionPosition(null); return;}
    let frame = 0;
    const place = () => {
      const bodyBounds = body.getBoundingClientRect(), paneBounds = pane.getBoundingClientRect(), pageBounds = sourcePage.getBoundingClientRect();
      const anchorX = pageBounds.left + (rect[0] + rect[2]) / 2;
      const anchorTop = pageBounds.top + rect[1], anchorBottom = pageBounds.top + rect[3];
      const width = actions.offsetWidth || 420, height = actions.offsetHeight || 42;
      const availableLeft = paneBounds.left - bodyBounds.left + width / 2 + 8;
      const availableRight = paneBounds.right - bodyBounds.left - width / 2 - 8;
      const left = availableRight < availableLeft ? (paneBounds.left + paneBounds.right) / 2 - bodyBounds.left :
        Math.max(availableLeft, Math.min(availableRight, anchorX - bodyBounds.left));
      let top = anchorTop - bodyBounds.top - height - 9;
      if (top < 8) top = anchorBottom - bodyBounds.top + 9;
      top = Math.max(8, Math.min(bodyBounds.height - height - 8, top));
      const visible = anchorBottom > paneBounds.top + 4 && anchorTop < paneBounds.bottom - 4;
      setSelectionPosition(previous => previous && Math.abs(previous.left - left) < .5 && Math.abs(previous.top - top) < .5 && previous.visible === visible ? previous : {left, top, visible});
    };
    const schedule = () => {cancelAnimationFrame(frame); frame = requestAnimationFrame(place);};
    place();
    pane.addEventListener('scroll', schedule, {passive: true});
    window.addEventListener('resize', schedule);
    const observer = new ResizeObserver(schedule);
    observer.observe(body); observer.observe(sourcePage); observer.observe(actions);
    return () => {cancelAnimationFrame(frame); pane.removeEventListener('scroll', schedule); window.removeEventListener('resize', schedule); observer.disconnect();};
  }, [selection, mode, page, scale, bilingual, navCollapsed]);
  useEffect(() => {
    if (!selection) return;
    const clear = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || document.querySelector('[role="dialog"]')) return;
      setSelection(null);
      window.getSelection()?.removeAllRanges();
    };
    document.addEventListener('keydown', clear);
    return () => document.removeEventListener('keydown', clear);
  }, [selection]);
  useEffect(() => {
    let cancelled = false, task: any;
    setLoading(true); setError('');
    Promise.all([api('attachments.get', {id: attachmentId}), api('annotations.list', {attachmentId})]).then(async ([info, ann]) => {
      if (cancelled) return;
      setRecord(info); setAnnotations(ann);
      setPage(jumpPage || info.position?.page || 1); setScale(info.position?.scale || 1); setRotation(info.position?.rotation || 0);
      if (!info.exists) throw new Error('附件文件缺失，请在文献信息的附件页中重新定位。');
      task = getDocument({url: 'app://local/attachment/' + attachmentId, cMapUrl: 'app://local/pdf-assets/cmaps/', cMapPacked: true, standardFontDataUrl: 'app://local/pdf-assets/standard_fonts/', wasmUrl: 'app://local/pdf-assets/wasm/'});
      task.onPassword = (callback: (value: string) => void, reason: number) => {if (!cancelled) setPassword({callback, reason});};
      const document = await task.promise;
      if (cancelled) return;
      setPdf(document); setOutline(await document.getOutline() || []); setLoading(false);
    }).catch(e => {if (!cancelled) {setError(useErrorText(e)); setLoading(false);}});
    return () => {cancelled = true; searchRun.current++; task?.destroy();};
  }, [attachmentId]);
  const goPage=useCallback((next:number)=>{setPage(next);if(mode==='continuous')requestAnimationFrame(()=>readerRoot.current?.querySelector(`.continuous-page[data-page="${next}"]`)?.scrollIntoView({block:'start'}));},[mode]);
  const handlePdfWheel = useCallback((event: ReactWheelEvent<HTMLDivElement>) => {
    if (event.ctrlKey) return;
    if (mode !== 'single' || !pdf || !event.deltaY) return;
    const container = event.currentTarget;
    const maxScroll = Math.max(0, container.scrollHeight - container.clientHeight);
    const atTop = container.scrollTop <= 1;
    const atBottom = container.scrollTop >= maxScroll - 1;
    if ((event.deltaY > 0 && !atBottom) || (event.deltaY < 0 && !atTop)) return;

    event.preventDefault();
    const direction = Math.sign(event.deltaY);
    const now = performance.now();
    if (direction !== wheelDirection.current || now - wheelTime.current > 240) wheelAmount.current = 0;
    wheelDirection.current = direction;
    wheelTime.current = now;
    wheelAmount.current += event.deltaY;
    if (Math.abs(wheelAmount.current) < 70) return;
    wheelAmount.current = 0;
    const next = page + direction;
    if (next < 1 || next > pdf.numPages) return;
    pendingWheelPosition.current = direction > 0 ? 'top' : 'bottom';
    goPage(next);
  }, [goPage, mode, page, pdf]);
  const handleContinuousScroll = useCallback((event: ReactUIEvent<HTMLDivElement>) => {
    if (mode !== 'continuous') return;
    const container = event.currentTarget;
    const bounds = container.getBoundingClientRect();
    const focus = bounds.top + Math.min(container.clientHeight * .35, 240);
    const pages = container.querySelectorAll<HTMLElement>(bilingual ? '.bilingual-page-row[data-page]' : '.continuous-page[data-page]');
    let nearest = 0, best = Infinity;
    for (const node of pages) {
      const rect = node.getBoundingClientRect();
      if (rect.bottom < bounds.top || rect.top > bounds.bottom) continue;
      const distance = focus < rect.top ? rect.top - focus : focus > rect.bottom ? focus - rect.bottom : 0;
      if (distance < best) {best = distance; nearest = Number(node.dataset.page);}
    }
    if (nearest) setPage(current => current === nearest ? current : nearest);
  }, [mode, bilingual]);
  useEffect(() => {if (jumpPage) goPage(jumpPage);}, [jumpPage, stamp]);
  useEffect(()=>{if(mode==='continuous'&&pdf){const frame=requestAnimationFrame(()=>readerRoot.current?.querySelector(`.continuous-page[data-page="${page}"]`)?.scrollIntoView({block:'start'}));return()=>cancelAnimationFrame(frame);}},[mode,pdf]);
  useEffect(() => {
    const position = pendingWheelPosition.current;
    if (mode !== 'single' || !position) return;
    const frame = requestAnimationFrame(() => {
      const container = pdfScroll.current;
      if (container) container.scrollTop = position === 'bottom' ? container.scrollHeight : 0;
      pendingWheelPosition.current = null;
    });
    return () => cancelAnimationFrame(frame);
  }, [mode, page]);
  useEffect(()=>{if(jumpAnnotation){const a=annotations.find(v=>v.id===jumpAnnotation);if(a){setActive(a);setComment(a.comment||'');setPage(a.pageIndex+1);setAnnotationsCollapsed(false);}}},[jumpAnnotation,stamp,annotations]);
  useEffect(() => {
    if (!pdf || !canvas.current || !text.current) return;
    let cancelled = false, renderTask: any, textLayer: any;
    setRendering(true); setSelection(null); setArea(null);
    const num = Math.max(1, Math.min(pdf.numPages, page));
    if (num !== page) setPage(num);
    pdf.getPage(num).then(async p => {
      if (cancelled || !canvas.current || !text.current) return;
      const vp = p.getViewport({scale, rotation: (p.rotate + rotation) % 360}); setViewport(vp);
      const dpr = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(16000000 / (vp.width * vp.height))), cv = canvas.current;
      cv.width = Math.floor(vp.width * dpr); cv.height = Math.floor(vp.height * dpr); cv.style.width = vp.width + 'px'; cv.style.height = vp.height + 'px';
      renderTask = p.render({canvas: cv, viewport: vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined});
      text.current.replaceChildren();
      text.current.style.setProperty('--scale-factor', String(scale));
      text.current.style.setProperty('--total-scale-factor', String(scale * p.userUnit));
      text.current.style.setProperty('--user-unit', String(p.userUnit));
      text.current.style.setProperty('--scale-round-x', '1px');
      text.current.style.setProperty('--scale-round-y', '1px');
      textLayer = new TextLayer({textContentSource: await p.getTextContent(), container: text.current, viewport: vp});
      await Promise.all([renderTask.promise, textLayer.render()]);
      if (!cancelled) {
        setRendering(false);
        if (search) for (const el of text.current.querySelectorAll('span')) if (el.textContent?.toLowerCase().includes(search.toLowerCase())) el.classList.add('search-hit');
      }
    }).catch(e => {if (!cancelled && e.name !== 'RenderingCancelledException' && e.name !== 'AbortException') {setError(useErrorText(e)); setRendering(false);}});
    return () => {cancelled = true; renderTask?.cancel(); textLayer?.cancel();};
  }, [pdf, page, scale, rotation, search, loading, mode]);
  useEffect(() => {
    if (!pdf) return;
    const timer = setTimeout(() => api('attachments.position', {id: attachmentId, page, scale, rotation}).catch(fail), 350);
    return () => clearTimeout(timer);
  }, [page, scale, rotation, pdf, attachmentId, fail]);
  useEffect(() => {
    if (!record?.itemId || !pdf) return;
    const flush = () => {
      const seconds = Math.min(3600, Math.floor((Date.now() - readingClock.current) / 1000));
      readingClock.current = Date.now();
      if (seconds >= 1) api('reading.activity', {itemId: record.itemId, attachmentId, page, pageCount: pdf.numPages, elapsedSeconds: seconds}).catch(() => {});
    };
    const timer = setInterval(flush, 30000);
    return () => {clearInterval(timer); flush();};
  }, [record?.itemId, attachmentId, page, pdf]);
  async function addAnnotation(type: string, captured = selection, annotationText = '') {
    const evidenceViewport = captured?.viewport || viewport;
    if (!captured || !record || !evidenceViewport) {notify(type === 'area' ? '在页面上拖出一个矩形区域。' : '先在 PDF 中选取文字。'); return;}
    try {
      const sourcePage = captured.page || page;
      const value = await api('annotations.save', {attachmentId, data: {type, version: record.version, pageIndex: sourcePage - 1, pageLabel: String(sourcePage), rects: captured.rects, quote: captured.quote || '', color, pageBox: evidenceViewport.viewBox, rotation, comment: annotationText}});
      setAnnotations(old => [...old, value]); setActive(value); setComment(annotationText); setSelection(null); window.getSelection()?.removeAllRanges(); setArea(null); setAnnotationDraft(null);
      notify(annotationText ? '批注已保存' : type === 'underline' ? '已添加下划线' : type === 'area' ? '区域批注已保存' : '已添加高亮');
    } catch(e) {fail(e);}
  }
  function startSelectedNote(captured = selection) {
    if (!captured?.quote?.trim() || !record?.itemId) {notify('先在 PDF 中选取文字。'); return;}
    const sourcePage = captured.page || page;
    const quote = String(captured.quote).trim().replace(/\r?\n/g, '\n> ');
    setNoteDraft({itemId: record.itemId, title: `阅读笔记 · 第 ${sourcePage} 页`,
      content: `> ${quote}\n\n[第 ${sourcePage} 页 · 返回原文](research://attachment/${attachmentId}?page=${sourcePage})\n\n`});
  }
  async function saveSelectedNote() {
    if (!noteDraft) return;
    setNoteBusy(true);
    try {
      await api('notes.save', {...noteDraft, tags: []});
      setNoteDraft(null); setSelection(null); window.getSelection()?.removeAllRanges();
      notify('阅读笔记已保存，可在“阅读与笔记”中继续编辑');
    } catch(e) {fail(e);} finally {setNoteBusy(false);}
  }
  function handleContinuousSelect(value: Data) {
    setSelection(value); setPage(value.page);
    if (tool === 'area' || tool === 'highlight' || tool === 'underline') addAnnotation(tool, value);
  }
  function captureSelection(event: ReactMouseEvent<HTMLDivElement>) {
    if (tool === 'area' || !paper.current || !viewport) return;
    const selected = window.getSelection();
    if (!selected || selected.isCollapsed || !selected.rangeCount || !text.current) {setSelection(null); return;}
    normalizePdfLineSelection(text.current, selected, selectionStart.current, {x: event.clientX, y: event.clientY});
    selectionStart.current = null;
    if (!text.current.contains(selected.anchorNode) || !text.current.contains(selected.focusNode)) {setSelection(null); return;}
    const bounds = paper.current.getBoundingClientRect();
    const viewportRects: number[][] = [];
    const rectangles = [...selected.getRangeAt(0).getClientRects()].filter(r => r.width > 1 && r.height > 1).map(r => {
      viewportRects.push([r.left - bounds.left, r.top - bounds.top, r.right - bounds.left, r.bottom - bounds.top]);
      const a = viewport.convertToPdfPoint(r.left - bounds.left, r.top - bounds.top), b = viewport.convertToPdfPoint(r.right - bounds.left, r.bottom - bounds.top);
      return [a[0], a[1], b[0], b[1]];
    });
    const captured = {rects: rectangles, viewportRects, quote: selected.toString(), page};
    setSelection(captured);
    if (tool === 'highlight' || tool === 'underline') addAnnotation(tool, captured);
  }
  async function find() {
    if (!pdf || !search.trim()) return;
    const run = ++searchRun.current; setSearching(true); setMatches([]); const found: Data[] = [];
    try {for (let n = 1; n <= pdf.numPages; n++) {
      if (run !== searchRun.current) return;
      const p = await pdf.getPage(n), content = await p.getTextContent();
      const value = content.items.map((x: any) => x.str || '').join(' '), pos = value.toLowerCase().indexOf(search.toLowerCase());
      if (pos >= 0) {found.push({page: n, snippet: value.slice(Math.max(0, pos - 30), pos + search.length + 80)}); setMatches([...found]);}
    }} catch(e) {fail(e);} finally {if (run === searchRun.current) setSearching(false);}
  }
  async function destination(dest: any) {try {if (!pdf) return; const array = typeof dest === 'string' ? await pdf.getDestination(dest) : dest; if (array) goPage(typeof array[0] === 'number' ? array[0] + 1 : (await pdf.getPageIndex(array[0])) + 1);} catch(e) {fail(e);}}
  const outlineNodes = (nodes: any[], depth = 0): any => nodes.map((n, i) => <div key={i}><button className="outline-item" style={{paddingLeft: 12 + depth * 12}} onClick={() => destination(n.dest)}>{n.title}</button>{outlineNodes(n.items || [], depth + 1)}</div>);
  const renderRect = (r: number[]) => {if (!viewport) return {}; const a = viewport.convertToViewportPoint(r[0], r[1]), b = viewport.convertToViewportPoint(r[2], r[3]); return {left: Math.min(a[0], b[0]), top: Math.min(a[1], b[1]), width: Math.abs(a[0] - b[0]), height: Math.abs(a[1] - b[1])};};
  return <div className="reader" ref={readerRoot}><div className="reader-toolbar"><button className="icon" aria-label={navCollapsed ? "展开页面缩略图和目录" : "收起页面缩略图和目录"} aria-expanded={!navCollapsed} title={navCollapsed ? "展开页面缩略图和目录" : "收起页面缩略图和目录"} onClick={()=>setNavCollapsed(v=>!v)}><List/></button><button className="icon" aria-label="上一页" disabled={page <= 1} onClick={() => goPage(page - 1)}><CaretLeft/></button><input aria-label="PDF 页码" type="number" min="1" max={pdf?.numPages || 1} value={page} onChange={e => goPage(Math.max(1, Math.min(pdf?.numPages || 1, Number(e.target.value) || 1)))}/><span>/ {pdf?.numPages || '—'}</span><button className="icon" aria-label="下一页" disabled={!pdf || page >= pdf.numPages} onClick={() => goPage(page + 1)}><CaretRight/></button><i className="separator"/><button className="icon" aria-label="缩小" onClick={() => setScale(s => Math.max(.25, s - .15))}><Minus/></button><button aria-label="重置 PDF 缩放至 100%" title="点击重置为 100%；按住 Ctrl 滚动鼠标滚轮缩放 PDF" onClick={() => setScale(1)}>{Math.round(scale * 100)}%</button><button className="icon" aria-label="放大" onClick={() => setScale(s => Math.min(4, s + .15))}><Plus/></button><button className="icon" aria-label="旋转页面" onClick={() => setRotation(r => (r + 90) % 360)}><ArrowClockwise/></button><i className="separator"/><button className={bilingual ? 'active' : ''} aria-label="切换原文译文对照" title="原文译文对照" onClick={()=>setBilingual(value=>!value)}><Translate/>对照翻译</button><button aria-label="切换连续阅读" onClick={()=>setMode(value=>value==='single'?'continuous':'single')}>{mode==='single'?'连续阅读':'单页阅读'}</button>{[['select', Cursor, '选择文字'], ['highlight', Highlighter, '高亮'], ['underline', TextUnderline, '下划线'], ['area', Selection, '区域批注']].map(([key, Icon, label]: any) => <button className={'icon ' + (tool === key ? 'active' : '')} title={label} aria-label={label} key={key} onMouseDown={e => e.preventDefault()} onClick={() => {setTool(key); if (selection && (key === 'highlight' || key === 'underline')) addAnnotation(key);}}><Icon/></button>)}<input className="color-input" aria-label="批注颜色" type="color" value={color} onChange={e => setColor(e.target.value)}/><span className="spacer"/><button className={'icon ' + (rightPanel === 'assistant' ? 'active' : '')} aria-label="打开辅助阅读" title="辅助阅读" onClick={() => {setBilingual(false); setRightPanel('assistant'); setAnnotationsCollapsed(false);}}><Sparkle/></button><button className="icon" aria-label="收起或展开批注侧栏" onClick={()=>{setBilingual(false); setRightPanel('annotations'); setAnnotationsCollapsed(v=>!v);}}><Notebook/></button><button onClick={() => files('annotatedPDF', {id: attachmentId}).then(r => r && notify('已导出带批注的 PDF 副本')).catch(fail)}><DownloadSimple/>导出批注</button></div>
  {record?.text_status==='no_text'&&<div className="reader-ocr-notice">此 PDF 没有可索引的文字层。可以阅读和做区域批注；OCR 与图表识别尚未启用，全文检索和问答不会假装读到页面文字。</div>}
  {error ? <ErrorBox message={error}/> : loading ? <Busy text="正在打开 PDF…"/> : <div className={"reader-body " + (bilingual ? "bilingual" : "")} ref={readerBody}>{selection?.quote?.trim() && <div className="reader-selection-actions" ref={selectionActions} style={{left: selectionPosition?.left, top: selectionPosition?.top, visibility: selectionPosition?.visible ? "visible" : "hidden"}} role="toolbar" aria-label="所选文字操作"><span>已选 {String(selection.quote).trim().length} 字</span><button aria-label="高亮所选文字" onMouseDown={e => e.preventDefault()} onClick={() => addAnnotation('highlight')}><Highlighter/>高亮</button><button aria-label="下划线标记所选文字" onMouseDown={e => e.preventDefault()} onClick={() => addAnnotation('underline')}><TextUnderline/>下划线</button><button aria-label="批注所选文字" onMouseDown={e => e.preventDefault()} onClick={() => {setAnnotationDraft(selection); setAnnotationComment('');}}><Notebook/>批注</button><button aria-label="将所选文字写入笔记" onMouseDown={e => e.preventDefault()} onClick={() => startSelectedNote()}><Notebook/>写笔记</button><button aria-label="清除文字选择" onMouseDown={e => e.preventDefault()} onClick={() => {setSelection(null); window.getSelection()?.removeAllRanges();}}>×</button></div>}<aside className={"reader-nav "+(navCollapsed?"collapsed":"")}><div className="reader-nav-tabs"><button aria-label="缩略图" className={left === 'thumb' ? 'active' : ''} onClick={() => setLeft('thumb')}><SquaresFour/></button><button aria-label="文档目录" className={left === 'outline' ? 'active' : ''} onClick={() => setLeft('outline')}><List/></button><button aria-label="文内搜索" className={left === 'search' ? 'active' : ''} onClick={() => setLeft('search')}><MagnifyingGlass/></button></div><div className="reader-nav-content">{left === 'thumb' && pdf && Array.from({length: pdf.numPages}, (_, n) => <Thumbnail key={n} pdf={pdf} page={n + 1} active={page === n + 1} click={() => goPage(n + 1)}/>)}{left === 'outline' && (outline.length ? outlineNodes(outline) : <p className="muted">此文档没有目录。</p>)}{left === 'search' && <><input aria-label="文内关键词" value={search} onChange={e => setSearch(e.target.value)} placeholder="查找文内文字" onKeyDown={e => e.key === 'Enter' && find()}/><button className="search-pdf" disabled={searching} onClick={find}>{searching ? '搜索中…' : '搜索全文'}</button><small className="muted">找到 {matches.length} 个匹配页面</small>{matches.map(m => <button key={m.page} className="search-result" onClick={() => goPage(m.page)}><strong>第 {m.page} 页</strong><p>{m.snippet}</p></button>)}</>}</div></aside>
  <div className={'pdf-scroll' + (mode === 'continuous' && bilingual ? ' bilingual-flow' : '')} ref={pdfScroll} onWheel={handlePdfWheel} onScroll={handleContinuousScroll}>
    {mode === 'continuous' && pdf ? bilingual && record ? <>
      {Array.from({length: pdf.numPages}, (_, n) => <BilingualContinuousPage key={n} pdf={pdf} itemId={record.itemId} attachmentId={attachmentId}
        page={n + 1} scale={scale} rotation={rotation} annotations={annotations} activeId={active?.id}
        tool={tool} selection={selection} onSelect={handleContinuousSelect} onStartSelection={() => setSelection(null)} notify={notify} fail={fail}/>)}
    </> : <>
      {Array.from({length: pdf.numPages}, (_, n) => <ContinuousPage key={n} pdf={pdf} page={n + 1} scale={scale}
        rotation={rotation} annotations={annotations} activeId={active?.id} tool={tool} onSelect={handleContinuousSelect} onStartSelection={() => setSelection(null)}/>)}
    </> : <><div className="pdf-paper" ref={paper} style={{width: viewport?.width, height: viewport?.height, cursor: tool === 'area' ? 'crosshair' : 'text'}} onMouseDown={e => {if (tool !== 'area') {selectionStart.current = {x: e.clientX, y: e.clientY}; setSelection(null);}}} onMouseUp={captureSelection}><canvas ref={canvas}/><div className="textLayer" ref={text} style={{pointerEvents: tool === 'area' ? 'none' : 'auto'}}/>
    <div className="annotation-layer">{annotations.filter(a => a.pageIndex === page - 1 && !a.stale).flatMap(a => a.rects.map((r: number[], i: number) => <div key={a.id + i} className={'annotation-mark ' + a.type + (active?.id === a.id ? ' focused' : '')} style={{...renderRect(r), backgroundColor: a.type === 'highlight' ? a.color + '66' : 'transparent', borderColor: a.color}}/>))}</div>
    {tool === 'area' && <div className="area-capture" onPointerDown={e => {const b = e.currentTarget.getBoundingClientRect(); start.current = [e.clientX - b.left, e.clientY - b.top]; e.currentTarget.setPointerCapture(e.pointerId);}} onPointerMove={e => {if (!start.current) return; const b = e.currentTarget.getBoundingClientRect(); setArea([...start.current, e.clientX - b.left, e.clientY - b.top]);}} onPointerUp={e => {if (!start.current || !viewport) return; const b = e.currentTarget.getBoundingClientRect(), end = [e.clientX - b.left, e.clientY - b.top]; if (Math.abs(end[0] - start.current[0]) > 4 && Math.abs(end[1] - start.current[1]) > 4) {const a = viewport.convertToPdfPoint(...start.current), z = viewport.convertToPdfPoint(...end); addAnnotation('area', {rects: [[...a, ...z]], quote: ''});} start.current = null; setArea(null);}}>{area && <div className="area-preview" style={{left: Math.min(area[0], area[2]), top: Math.min(area[1], area[3]), width: Math.abs(area[2] - area[0]), height: Math.abs(area[3] - area[1])}}/>}</div>}
    {rendering && <div className="render-indicator">正在渲染…</div>}</div><div className="paper-caption">第 {page} 页 · {record?.name}</div></>}</div>
  {bilingual && mode === 'continuous' ? null : bilingual && record && pdf ? <TranslationPage itemId={record.itemId} attachmentId={attachmentId} pdf={pdf} page={page} pageCount={pdf.numPages} scale={scale} rotation={rotation} selection={selection} notify={notify} fail={fail}/> : rightPanel === 'assistant' && record && pdf ? <ReadingAssistant itemId={record.itemId} attachmentId={attachmentId} page={page} pageCount={pdf.numPages} selection={selection} notify={notify} fail={fail}/> : <aside className={"reader-annotations "+(annotationsCollapsed?"collapsed":"")}><div className="section-heading"><h3>批注与摘录 <span>{annotations.length}</span></h3><button className="icon" aria-label="打开笔记" onClick={onNote}><Notebook/></button></div><p className="muted">选取文字后点击高亮或下划线，区域工具可框选图表。</p>{annotations.length ? annotations.map(a => <button key={a.id} className={'annotation-card ' + (active?.id === a.id ? 'active' : '')} style={{borderLeftColor: a.color}} onClick={() => {setActive(a); setComment(a.comment || ''); goPage(a.pageIndex + 1);}}><small>第 {a.pageIndex + 1} 页 · {a.type === 'area' ? '区域' : a.type === 'underline' ? '下划线' : '高亮'}</small><p>{a.quote || '图表与区域批注'}</p>{a.comment && <em>{a.comment}</em>}</button>) : <div className="annotation-empty"><Highlighter size={28}/><p>还没有批注<br/>读到关键处，留下你的思考。</p></div>}{active && <div className="annotation-edit"><label>批注想法<textarea aria-label="批注评论" value={comment} onChange={e => setComment(e.target.value)} rows={4}/></label><div className="mini-actions"><button onClick={async () => {try {const value = await api('annotations.save', {attachmentId, data: {...active, comment}}); setAnnotations(old => old.map(a => a.id === value.id ? value : a)); setActive(value); notify('批注已保存');} catch(e) {fail(e);}}}>保存</button><button onClick={() => api('annotations.excerpt', {id: active.id}).then(() => notify('已生成包含原文链接的阅读笔记')).catch(fail)}><Notebook size={15}/>摘录到笔记</button><button disabled={!record?.itemId || !(active.quote || comment).trim()} onClick={() => record?.itemId && onResearchAsk(record.itemId, '请结合这条批注解释：' + (active.quote || comment))}><Sparkle size={15}/>基于批注提问</button><button className="icon danger" aria-label="删除批注" onClick={() => api('annotations.delete', {id: active.id, revision: active.revision}).then(() => {setAnnotations(old => old.filter(entry => entry.id !== active.id)); setActive(null);}).catch(fail)}><Trash size={16}/></button></div></div>}</aside>}</div>}
  {annotationDraft && <Modal title="批注所选文字" close={() => setAnnotationDraft(null)}><p className="reader-selection-quote">{annotationDraft.quote}</p><label>批注内容<textarea aria-label="批注内容" value={annotationComment} onChange={e => setAnnotationComment(e.target.value)} rows={5} placeholder="写下理解、疑问或后续思路…"/></label><div className="dialog-actions"><button onClick={() => setAnnotationDraft(null)}>取消</button><button className="primary" onClick={() => addAnnotation('highlight', annotationDraft, annotationComment)}>保存批注</button></div></Modal>}
  {noteDraft && <Modal title="从选中文字创建笔记" close={() => setNoteDraft(null)} wide><label>笔记标题<input aria-label="笔记标题" value={noteDraft.title} onChange={e => setNoteDraft({...noteDraft, title: e.target.value})}/></label><label>笔记内容<textarea className="reader-selection-note" aria-label="笔记内容" value={noteDraft.content} onChange={e => setNoteDraft({...noteDraft, content: e.target.value})} rows={12}/></label><small className="muted">摘录保留返回原 PDF 页面的链接。</small><div className="dialog-actions"><button onClick={() => setNoteDraft(null)}>取消</button><button className="primary" disabled={noteBusy || !noteDraft.title?.trim() || !noteDraft.content?.trim()} onClick={saveSelectedNote}>{noteBusy ? '保存中…' : '保存笔记'}</button></div></Modal>}
  {password && <Modal title={password.reason === 2 ? '密码不正确，请重试' : '此 PDF 需要密码'} close={() => {setPassword(null); setError('PDF 需要密码，请关闭标签后重新打开。'); setLoading(false);}}><label>密码<input type="password" value={passwordText} onChange={e => setPasswordText(e.target.value)}/></label><div className="dialog-actions"><button className="primary" onClick={() => {password.callback(passwordText); setPassword(null); setPasswordText('');}}>打开 PDF</button></div></Modal>}
  </div>;
}
