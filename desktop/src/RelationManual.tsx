import {useEffect, useState} from 'react';
import {LinkSimple, Plus, Trash} from '@phosphor-icons/react';
import {api, type Data, useErrorText} from './api';
import './relationManual.css';

export function RelationManual({session, rows, selectedId, onRows, onSelect, notify, fail}: {
  session: Data; rows: Data[]; selectedId?: string | null; onRows: (rows: Data[]) => void;
  onSelect: (row: Data) => void; notify: (message: string) => void; fail: (error: any) => void;
}) {
  const [left, setLeft] = useState(''), [right, setRight] = useState('');
  const [label, setLabel] = useState('人工关联'), [note, setNote] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  useEffect(() => {setLeft(session.items[0]?.itemId || ''); setRight(session.items[1]?.itemId || '');}, [session.id]);
  async function add() {
    setBusy(true); setError('');
    try {onRows(await api('relations.manualAdd', {sessionId: session.id, leftItemId: left, rightItemId: right, label, note}));
      setNote(''); notify('人工关联已保存');}
    catch (exception) {setError(useErrorText(exception));} finally {setBusy(false);}
  }
  async function remove(row: Data) {
    try {onRows(await api('relations.manualRemove', {id: row.id})); notify('人工关联已移除');}
    catch (exception) {fail(exception);}
  }
  return <section className="relation-manual"><div className="relation-manual-heading"><LinkSimple size={17}/><div><h3>人工关联</h3><p>仅记录你的判断；不会标成数据库引文或自动证据。</p></div><strong>{rows.length} 条</strong></div>
    <div className="relation-manual-form"><select aria-label="人工关联左侧文献" value={left} onChange={event => setLeft(event.target.value)}>{session.items.map((item: Data) => <option key={item.itemId} value={item.itemId}>{item.title}</option>)}</select><select aria-label="人工关联右侧文献" value={right} onChange={event => setRight(event.target.value)}>{session.items.map((item: Data) => <option key={item.itemId} value={item.itemId}>{item.title}</option>)}</select><input aria-label="人工关联名称" value={label} maxLength={80} onChange={event => setLabel(event.target.value)} placeholder="例如：同一实验数据集"/><input aria-label="人工关联说明" value={note} maxLength={2000} onChange={event => setNote(event.target.value)} placeholder="可选：记录判断依据"/><button disabled={busy || !left || !right || left === right} onClick={add}><Plus size={16}/>建立关联</button></div>
    {error && <p className="relation-manual-error">{error}</p>}
    {!!rows.length && <div className="relation-manual-list">{rows.map(row => <article key={row.id} className={selectedId === row.id ? 'selected' : ''} onClick={() => onSelect(row)}><div><strong>{row.label}</strong><p>{row.leftTitle} ↔ {row.rightTitle}</p>{row.note && <small>{row.note}</small>}</div><button aria-label={`移除人工关联 ${row.label}`} onClick={event => {event.stopPropagation(); remove(row);}}><Trash size={15}/></button></article>)}</div>}
  </section>;
}
