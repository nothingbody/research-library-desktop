import {useEffect, useRef, useState} from 'react';
import {type PDFDocumentProxy} from 'pdfjs-dist';
import {type Data} from './api';
import {ContinuousPage} from './ContinuousPage';
import {TranslationPage} from './TranslationPage';

export function BilingualContinuousPage({pdf, itemId, attachmentId, page, scale, rotation, annotations, activeId,
  tool, selection, onSelect, onStartSelection, notify, fail}: {
  pdf: PDFDocumentProxy; itemId: string; attachmentId: string; page: number; scale: number; rotation: number;
  annotations: Data[]; activeId?: string; tool: string; selection: Data | null;
  onSelect: (value: Data) => void; onStartSelection?: () => void; notify: (text: string) => void; fail: (error: any) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [near, setNear] = useState(false);
  const [size, setSize] = useState({width: 595 * scale, height: 842 * scale});
  useEffect(() => {
    let cancelled = false;
    pdf.getPage(page).then(pdfPage => {
      if (cancelled) return;
      const viewport = pdfPage.getViewport({scale, rotation: (pdfPage.rotate + rotation) % 360});
      setSize({width: viewport.width, height: viewport.height});
    }).catch(fail);
    return () => {cancelled = true;};
  }, [pdf, page, scale, rotation]);
  useEffect(() => {
    const node = box.current;
    if (!node) return;
    const observer = new IntersectionObserver(entries => setNear(entries.some(entry => entry.isIntersecting)),
      {root: node.closest('.pdf-scroll'), rootMargin: '800px 0px'});
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return <div ref={box} className="bilingual-page-row" data-page={page}
    style={{gridTemplateColumns: `${size.width}px ${size.width}px`}}>
    <ContinuousPage pdf={pdf} page={page} scale={scale} rotation={rotation} size={size}
      annotations={annotations} activeId={activeId} tool={tool} onSelect={onSelect} onStartSelection={onStartSelection}/>
    <div className="bilingual-translation-slot" style={{width: size.width, height: size.height}}>
      {near && <TranslationPage itemId={itemId} attachmentId={attachmentId} pdf={pdf} page={page}
        pageCount={pdf.numPages} scale={scale} rotation={rotation} selection={selection}
        embedded initialSize={size} notify={notify} fail={fail}/>}
    </div>
  </div>;
}
