import {useEffect, useLayoutEffect, useMemo, useRef, useState} from 'react';
import {ArrowClockwise, Copy, Translate} from '@phosphor-icons/react';
import {type PDFDocumentProxy} from 'pdfjs-dist';
import {api, type Data} from './api';
import {buildTranslationLayout, findAlignedRange, findSelectedParagraph, type TranslationLayout, type TranslationParagraph} from './translationLayout';

type Props = {itemId: string; attachmentId: string; pdf: PDFDocumentProxy; page: number; pageCount: number; scale: number; rotation: number; selection: Data | null; embedded?: boolean; initialSize?: {width: number; height: number}; notify: (text: string) => void; fail: (error: any) => void};
type Alignment = {paragraphId: string; range?: {start: number; end: number}; pending?: boolean; error?: string};
const LAYOUT_VERSION = 3;
const EMPTY_LAYOUT: TranslationLayout = {lines: [], paragraphs: []};

function FittedParagraph({paragraph, text, scale, alignment}: {paragraph: TranslationParagraph; text: string; scale: number; alignment: Alignment | null}) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || !text) return;
    let fontSize = paragraph.fontSize * (paragraph.lineCount >= 3 ? 1.14 : 1);
    element.style.fontSize = fontSize + 'px';
    element.style.lineHeight = '1.16';
    while (fontSize > Math.max(6.5 * scale, paragraph.fontSize * .58) &&
           (element.scrollHeight > element.clientHeight + 1 || element.scrollWidth > element.clientWidth + 1)) {
      fontSize -= .4 * scale;
      element.style.fontSize = fontSize + 'px';
    }
    // Chinese usually occupies fewer wrapped lines than the English source.
    // Fill the source paragraph's vertical space so corresponding phrases stay
    // near the same height on the two PDF pages.
    const range = document.createRange();
    range.selectNodeContents(element);
    const tops: number[] = [];
    for (const rect of range.getClientRects()) {
      if (rect.width > 1 && !tops.some(top => Math.abs(top - rect.top) < 2)) tops.push(rect.top);
    }
    if (paragraph.lineCount >= 3 && tops.length >= 2) {
      const target = Math.min(2.1, Math.max(1.16,
        (element.clientHeight - fontSize * .5) / (tops.length * fontSize)));
      element.style.lineHeight = String(target);
      let fitted = target;
      while (fitted > 1.16 && element.scrollHeight > element.clientHeight + 1) {
        fitted = Math.max(1.16, fitted - .04);
        element.style.lineHeight = String(fitted);
      }
    }
  }, [paragraph, text, scale, alignment?.range?.start, alignment?.range?.end]);
  const active = alignment?.paragraphId === paragraph.id;
  const range = active ? alignment?.range : undefined;
  return <div ref={ref} data-paragraph-id={paragraph.id} title={text}
    className={'translation-paragraph ' + (paragraph.fontSize >= 15 * scale ? 'title' : '')}
    style={{left: paragraph.left, top: paragraph.top, width: paragraph.width + 2 * scale,
      height: paragraph.height + 2 * scale, fontSize: paragraph.fontSize}}>
      {range ? <>{text.slice(0, range.start)}<mark className="translation-selected">{text.slice(range.start, range.end)}</mark>{text.slice(range.end)}</> : text}
    </div>;
}

export function TranslationPage({itemId, attachmentId, pdf, page, pageCount, scale, rotation, selection, embedded = false, initialSize, notify, fail}: Props) {
  selection = selection?.pages ? selection.pages.find((value: Data) => value.page === page) || null : selection;
  const canvas = useRef<HTMLCanvasElement>(null), scroll = useRef<HTMLElement>(null), forceNext = useRef(false);
  const alignmentCache = useRef(new Map<string, string>());
  const [layoutState, setLayoutState] = useState<{key: string; value: TranslationLayout}>({key: '', value: EMPTY_LAYOUT});
  const [size, setSize] = useState(initialSize || {width: 700, height: 900});
  const [translation, setTranslation] = useState<Data | null>(null), [checking, setChecking] = useState(true);
  const [refreshEpoch, setRefreshEpoch] = useState(0), [rendering, setRendering] = useState(true), [message, setMessage] = useState('');
  const [alignment, setAlignment] = useState<Alignment | null>(null);
  const renderKey = `${attachmentId}:${page}:${scale}:${rotation}`;
  const layout = layoutState.key === renderKey ? layoutState.value : EMPTY_LAYOUT;
  const translations = translation?.translations || {};
  const pending = translation?.state === 'pending';
  const skipped = translation?.state === 'skipped';
  const ready = translation?.state === 'ready' && layout.paragraphs.length > 0;
  const selectedParagraph = useMemo(() => selection?.page === page ?
    findSelectedParagraph(layout.paragraphs, selection.viewportRects || [], String(selection.quote || '')) : null,
  [layout, page, selection]);
  useEffect(() => {
    let cancelled = false, task: any;
    setRendering(true); setLayoutState({key: '', value: EMPTY_LAYOUT}); setTranslation(null); setMessage('');
    pdf.getPage(page).then(async pdfPage => {
      if (cancelled || !canvas.current) return;
      const viewport = pdfPage.getViewport({scale, rotation: (pdfPage.rotate + rotation) % 360});
      setSize({width: viewport.width, height: viewport.height});
      const dpr = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(16000000 / (viewport.width * viewport.height)));
      const target = canvas.current;
      target.width = Math.floor(viewport.width * dpr);
      target.height = Math.floor(viewport.height * dpr);
      target.style.width = viewport.width + 'px';
      target.style.height = viewport.height + 'px';
      task = pdfPage.render({canvas: target, viewport, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined});
      const content = await pdfPage.getTextContent();
      await task.promise;
      if (!cancelled) {setLayoutState({key: renderKey, value: buildTranslationLayout(content, viewport, scale)}); setRendering(false);}
    }).catch(error => {if (!cancelled) {setRendering(false); setMessage('无法渲染本页译文底稿。'); fail(error);}});
    return () => {cancelled = true; task?.cancel();};
  }, [pdf, page, scale, rotation]);
  useEffect(() => {
    if (!layout.paragraphs.length) {setChecking(false); return;}
    let cancelled = false, timer: number | undefined;
    setChecking(true); setTranslation(null);
    const force = forceNext.current;
    forceNext.current = false;
    const load = async (refresh = false) => {
      try {
        const value = await api('assistant.translation.page', {itemId, attachmentId, page, layoutVersion: LAYOUT_VERSION,
          segments: layout.paragraphs.map(({id, text}) => ({id, text})), force: refresh});
        if (cancelled) return;
        setTranslation(value); setChecking(false);
        if (value.state === 'pending') timer = window.setTimeout(() => load(), 1400);
      } catch (error) {if (!cancelled) {setChecking(false); setMessage('无法读取或保存本页译文。'); fail(error);}}
    };
    load(force);
    return () => {cancelled = true; if (timer) window.clearTimeout(timer);};
  }, [attachmentId, layout, itemId, page, refreshEpoch]);
  const selectedTranslation = selectedParagraph ? String(translations[selectedParagraph.id] || '') : '';
  useEffect(() => {
    let cancelled = false, timer: number | undefined;
    const paragraph = selectedParagraph;
    const quote = String(selection?.quote || '').trim();
    if (!paragraph || !quote) {setAlignment(null); return;}
    setAlignment({paragraphId: paragraph.id, pending: true});
    if (!ready || !translation?.sourceHash || !selectedTranslation) return;
    const source = paragraph.text;
    const sourceNormalized = source.replace(/\s+/g, ' ').trim().toLowerCase();
    const quoteNormalized = quote.replace(/\s+/g, ' ').trim().toLowerCase();
    if (sourceNormalized === quoteNormalized) {
      setAlignment({paragraphId: paragraph.id, range: {start: 0, end: selectedTranslation.length}});
      return;
    }
    const firstRect = (selection?.viewportRects || [])[0] as number[] | undefined;
    const positionRatio = firstRect ? Math.max(0, Math.min(1, paragraph.lineCount <= 1 ?
      ((firstRect[0] + firstRect[2]) / 2 - paragraph.left) / Math.max(1, paragraph.width) :
      ((firstRect[1] + firstRect[3]) / 2 - paragraph.top) / Math.max(1, paragraph.height))) : 0;
    const applyTarget = (target: string) => {
      if (cancelled) return;
      const range = findAlignedRange(source, quote, selectedTranslation, target, positionRatio);
      setAlignment(range ? {paragraphId: paragraph.id, range} : {paragraphId: paragraph.id, error: '未能准确定位对应译文'});
    };
    if (quote.length >= 2 && selectedTranslation.includes(quote)) {applyTarget(quote); return;}
    const input = JSON.stringify({sourceParagraph: source, translatedParagraph: selectedTranslation, selectedText: quote});
    const cacheKey = translation.sourceHash + ':' + input;
    if (alignmentCache.current.has(cacheKey)) {applyTarget(alignmentCache.current.get(cacheKey)!); return;}
    const check = async () => {
      try {
        const history: Data[] = await api('assistant.runs', {itemId, task: 'align_translation', limit: 100});
        if (cancelled) return;
        const run = history.find(entry => entry.task === 'align_translation' && entry.attachmentId === attachmentId &&
          Number(entry.input?.page) === page && entry.input?.text === input);
        if (!run) {
          await api('assistant.run', {task: 'align_translation', itemId, attachmentId, scope: 'selection', page, text: input});
          if (!cancelled) timer = window.setTimeout(check, 1000);
        } else if (run.state === 'completed') {
          const target = String(run.result?.alignedTarget || '');
          alignmentCache.current.set(cacheKey, target);
          applyTarget(target);
        } else if (run.state === 'failed' || run.state === 'cancelled') {
          if (!cancelled) setAlignment({paragraphId: paragraph.id, error: '译文定位暂不可用'});
        } else if (!cancelled) timer = window.setTimeout(check, 1000);
      } catch {
        if (!cancelled) setAlignment({paragraphId: paragraph.id, error: '译文定位暂不可用'});
      }
    };
    timer = window.setTimeout(check, 180);
    return () => {cancelled = true; if (timer) window.clearTimeout(timer);};
  }, [itemId, attachmentId, page, selection, selectedParagraph, selectedTranslation, translation?.sourceHash, ready]);
  useEffect(() => {
    if (!selectedParagraph) return;
    const frame = requestAnimationFrame(() => {
      const section = scroll.current;
      const pane = embedded ? section?.closest<HTMLElement>('.pdf-scroll') : section;
      const paragraph = section?.querySelector<HTMLElement>(`[data-paragraph-id="${selectedParagraph.id}"]`);
      const target = paragraph?.querySelector('mark') || paragraph;
      if (!pane || !target) return;
      const paneRect = pane.getBoundingClientRect(), targetRect = target.getBoundingClientRect();
      if (targetRect.top < paneRect.top + 24 || targetRect.bottom > paneRect.bottom - 24) {
        pane.scrollTo({top: pane.scrollTop + targetRect.top - paneRect.top - pane.clientHeight * .35, behavior: 'smooth'});
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [selectedParagraph, alignment?.range?.start, embedded]);
  function retry() {setMessage(''); forceNext.current = true; setRefreshEpoch(value => value + 1);}
  const issue = translation?.error || message;
  return <section ref={scroll} className={'translated-scroll' + (embedded ? ' embedded' : '')} aria-label="中文翻译 PDF 页面">
    <article className="translated-paper" style={{width: size.width, height: size.height}}>
      <canvas ref={canvas}/>
      <div className="translation-mask-layer" aria-hidden="true">
        {!skipped && layout.lines.map((line, index) => <div key={index} className="translation-mask" style={{
          left: line.left - 2 * scale, top: line.top - scale, width: line.right - line.left + 4 * scale,
          height: line.bottom - line.top + 2 * scale,
        }}/>)}
      </div>
      <div className="translation-text-layer">
        {layout.paragraphs.map(paragraph => <FittedParagraph key={paragraph.id} paragraph={paragraph}
          text={String(translations[paragraph.id] || '')} scale={scale} alignment={alignment}/>)}
      </div>
      {ready && selection?.page === page && !selectedParagraph && <div className="translation-source-selection" aria-hidden="true">
        {(selection.viewportRects || []).map((rect: number[], index: number) => <div key={index} style={{
          left: rect[0], top: rect[1], width: Math.max(0, rect[2] - rect[0]), height: Math.max(0, rect[3] - rect[1]),
        }}/>) }
      </div>}
      {(rendering || checking || pending || !ready) && <div className="translation-status"><Translate className={rendering || pending ? 'spin' : ''}/>
        <strong>{rendering || checking ? '正在读取本机译文…' : pending ? '正在翻译当前页…' : skipped ? '中文页面无需翻译' : layout.paragraphs.length ? '当前页还没有译文' : '此页没有可识别的文字'}</strong>
        <p>{issue || translation?.reason || (layout.paragraphs.length ? '译文完成后将自动保存到本机文献库。' : '此页没有可识别的文字。')}</p>
        {!rendering && !checking && !pending && !skipped && layout.paragraphs.length > 0 && <button onClick={retry}><ArrowClockwise/>重新尝试</button>}
      </div>}
      {ready && <footer><small>中文译文 · 第 {page} / {pageCount} 页 · 已存本机{alignment?.pending ? ' · 正在定位选中文本…' : alignment?.error ? ' · ' + alignment.error : ''}</small>
        <button onClick={() => navigator.clipboard.writeText(layout.paragraphs.map(paragraph => translations[paragraph.id]).filter(Boolean).join('\n\n')).then(() => notify('本页译文已复制')).catch(fail)}><Copy/>复制译文</button>
      </footer>}
    </article>
  </section>;
}
