import {useEffect, useRef, useState, type ReactNode} from 'react';
import {X, CircleNotch, MagnifyingGlass, WarningCircle, CaretLeft, CaretRight} from '@phosphor-icons/react';
import type {Data} from './api';

export function Modal({title, children, close, wide = false}: {title: string; children: ReactNode; close: () => void; wide?: boolean}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    (ref.current?.querySelector<HTMLElement>('[autofocus]') ||
      ref.current?.querySelector<HTMLElement>('input:not([type="hidden"]),textarea,select') ||
      ref.current?.querySelector<HTMLElement>('button'))?.focus();
    const keyboard = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
      if (e.key === 'Tab') {
        const elements = Array.from(ref.current?.querySelectorAll<HTMLElement>('button,input,select,textarea,a[href]') || []).filter(el => !(el as HTMLButtonElement).disabled);
        const first = elements[0], last = elements.at(-1);
        if (e.shiftKey && document.activeElement === first) {last?.focus(); e.preventDefault();}
        if (!e.shiftKey && document.activeElement === last) {first?.focus(); e.preventDefault();}
      }
    };
    document.addEventListener('keydown', keyboard);
    return () => {document.removeEventListener('keydown', keyboard); previous?.focus();};
  }, [close]);
  return <div className="modal-shade"><div ref={ref} className={'modal ' + (wide ? 'wide' : '')} role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2><button className="icon" aria-label="关闭对话框" onClick={close}><X/></button></header><div className="modal-body">{children}</div></div></div>;
}
export function Empty({icon, title, children, action}: {icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode}) {
  return <div className="empty"><div className="empty-icon">{icon || <MagnifyingGlass size={34}/>}</div><h2>{title}</h2><p>{children}</p>{action}</div>;
}
export const Busy = ({text = '正在载入…'}: {text?: string}) => <div className="busy"><CircleNotch className="spin" size={22}/>{text}</div>;
export const ErrorBox = ({message, retry}: {message: string; retry?: () => void}) => <div className="error-box"><WarningCircle size={20}/><span>{message}</span>{retry && <button onClick={retry}>重试</button>}</div>;
export function Search({value, onChange, placeholder = '搜索文献、作者、DOI 或全文…'}: {value: string; onChange: (value: string) => void; placeholder?: string}) {
  return <label className="search"><MagnifyingGlass size={19}/><input data-search value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder}/>{value && <button className="icon" aria-label="清除搜索" onClick={() => onChange('')}><X size={16}/></button>}<kbd>Ctrl F</kbd></label>;
}
export function Pager({page, total, size, setPage}: {page: number; total: number; size: number; setPage: (page: number) => void}) {
  const pages = Math.max(1, Math.ceil(total / size));
  const [jump, setJump] = useState('');
  return <div className="pager"><span>共 {total.toLocaleString()} 条</span><div><button className="icon" disabled={page <= 1} aria-label="上一页" onClick={() => setPage(page - 1)}><CaretLeft/></button><span>{page} / {pages}</span><button className="icon" disabled={page >= pages} aria-label="下一页" onClick={() => setPage(page + 1)}><CaretRight/></button><input aria-label="跳转页码" type="number" min="1" max={pages} value={jump} onChange={e => setJump(e.target.value)} onKeyDown={e => {if (e.key === 'Enter') {setPage(Math.max(1, Math.min(pages, Number(jump) || 1))); setJump('');}}}/><span>页</span></div></div>;
}
export function Fields({value, labels}: {value: Data; labels?: Data}) {
  return <dl className="fields">{Object.entries(value).filter(([, v]) => v !== null && v !== undefined && v !== '').map(([k, v]) => <div key={k}><dt>{labels?.[k] || k}</dt><dd>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</dd></div>)}</dl>;
}
export function JsonView({value}: {value: any}) {return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;}
