import {useCallback, useEffect, useMemo, useState} from 'react';
import {ArrowsClockwise, CheckCircle, Copy, MagnifyingGlass} from '@phosphor-icons/react';
import {api, type Data, dateText, useErrorText} from './api';
import {Empty, ErrorBox} from './ui';

const fields = [['title', '题名'], ['authors', '作者'], ['year', '年份'], ['venue', '期刊 / 来源'], ['doi', 'DOI'], ['abstract', '摘要'], ['oaUrl', '开放获取链接']];
const blankSide = () => ({title: '', authors: '', year: '', venue: '', doi: '', abstract: '', oaUrl: '', url: '', citationCount: 0});
const blank = () => ({query: '多车间、多目标作业调度', source: '阙问 Paper', sourceUrl: 'https://qiewenpaper.com/app/search', external: blankSide(), local: blankSide(), conclusion: ''});
const statusText: Data = {match: '一致', different: '不一致', missingLocal: '本地缺失', missingExternal: '外部缺失', empty: '两侧为空'};

export function Comparison({tick, notify}: {tick: number; notify: (message: string) => void}) {
  const [records, setRecords] = useState<Data[]>([]), [value, setValue] = useState<Data>(blank), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const load = useCallback(() => api('comparison.list').then(setRecords).catch(e => setError(useErrorText(e))), []);
  useEffect(() => {load();}, [load, tick]);
  const preview = useMemo(() => fields.map(([key, label]) => {
    const external = String(value.external[key] || ''), local = String(value.local[key] || '');
    return {key, label, external, local, status: external && local ? external.trim().toLowerCase() === local.trim().toLowerCase() ? 'match' : 'different' : external ? 'missingLocal' : local ? 'missingExternal' : 'empty'};
  }), [value]);
  const identity = useMemo(() => {const a=value.external,b=value.local; const clean=(s:string)=>s.trim().toLowerCase().replace(/^https?:\/\/(dx\.)?doi\.org\//,''); if(a.doi&&b.doi)return clean(a.doi)===clean(b.doi)?'same':'different'; return a.title&&b.title&&a.title.trim().toLowerCase()===b.title.trim().toLowerCase()&&a.year&&a.year===b.year?'possible':'unknown';},[value]);
  const update = (side: 'external' | 'local', key: string, content: string) => setValue((old: Data) => ({...old, [side]: {...old[side], [key]: content}}));
  async function save() {
    setBusy(true); setError('');
    try {const record = await api('comparison.save', value); setRecords(old => [record, ...old.filter(item => item.id !== record.id)]); setValue(blank()); notify('检索对比已保存到本地');}
    catch (exception) {setError(useErrorText(exception));} finally {setBusy(false);}
  }
  return <div className="module-page comparison-page"><div className="page-heading"><div className="eyebrow">EVIDENCE CHECK</div><h1>外部检索对比</h1><p>核对同一篇论文的题录字段。不同论文之间的字段差异不能衡量检索质量。</p></div>
    {error && <ErrorBox message={error} retry={load}/>}<section className="comparison-layout"><div className="comparison-form"><section className="panel comparison-setup"><div className="section-heading"><div><h2><MagnifyingGlass/>建立一次对比</h2><p>记录来源、检索主题和两侧可核验的题录字段。</p></div><button onClick={()=>setValue(blank())}>新建对比</button><button onClick={load}><ArrowsClockwise/>刷新记录</button></div><div className="comparison-top-fields"><label>检索主题<input aria-label="对比检索主题" value={value.query} onChange={event => setValue((old: Data) => ({...old, query: event.target.value}))}/></label><label>外部来源<input aria-label="外部检索来源" value={value.source} onChange={event => setValue((old: Data) => ({...old, source: event.target.value}))}/></label><label>来源页面<input aria-label="外部来源页面" value={value.sourceUrl} onChange={event => setValue((old: Data) => ({...old, sourceUrl: event.target.value}))}/></label></div></section>
      <section className="comparison-sides"><article className="panel comparison-side"><header><span>01 · EXTERNAL</span><h2>外部检索结果</h2><small>例如阙问 Paper 的论文卡片或详情页。</small></header>{fields.map(([key, label]) => <label key={key}>{label}{key === 'abstract' ? <textarea aria-label={'外部' + label} rows={4} value={value.external[key]} onChange={event => update('external', key, event.target.value)}/> : <input aria-label={'外部' + label} value={value.external[key]} onChange={event => update('external', key, event.target.value)}/>}</label>)}</article>
        <article className="panel comparison-side"><header><span>02 · LOCAL</span><h2>本地学术源结果</h2><small>粘贴 AI 文献检索候选或导入后的题录。</small></header>{fields.map(([key, label]) => <label key={key}>{label}{key === 'abstract' ? <textarea aria-label={'本地' + label} rows={4} value={value.local[key]} onChange={event => update('local', key, event.target.value)}/> : <input aria-label={'本地' + label} value={value.local[key]} onChange={event => update('local', key, event.target.value)}/>}</label>)}</article></section>
      <section className="panel comparison-preview"><div><h2>字段核对</h2><p>{identity==='same'?'两侧 DOI 一致，可核对字段来源。':identity==='different'?'两侧 DOI 不同：属于不同记录，字段差异不是元数据错误。':identity==='possible'?'题名与年份一致，仍需核对作者和 DOI。':'尚未确认是同一篇论文；此表不用于评价检索质量。'}</p><p>比较检索效果需要相同问题、模式和范围，并人工标注 Top-K 结果；本页仅保存题录观察。</p></div><div className="comparison-field-grid">{preview.map(item => <article className={item.status} key={item.key}><strong>{item.label}</strong><span>{statusText[item.status]}</span><small>{item.external || '—'} <b>↔</b> {item.local || '—'}</small></article>)}</div><label>结论<textarea aria-label="对比结论" rows={3} placeholder="记录身份核验、字段来源及待确认问题；不同论文的差异不能用于排名检索工具。" value={value.conclusion} onChange={event => setValue((old: Data) => ({...old, conclusion: event.target.value}))}/></label><button className="primary" disabled={busy || !value.query.trim()} onClick={save}>{busy ? '保存中…' : <><CheckCircle weight="fill"/>保存本次对比</>}</button></section></div>
      <aside className="comparison-history panel"><div className="section-heading"><div><h2><Copy/>已保存对比</h2><p>仅保存在当前文献库。</p></div></div>{records.length ? <div>{records.map(record => <article key={record.id}><span>{record.source}</span><h3>{record.title}</h3><p>{record.query}</p><div>{Object.entries(record.summary || {}).filter(([, count]) => count).map(([status, count]) => <i className={status} key={status}>{statusText[status]} {String(count)}</i>)}</div><small>{dateText(record.updatedAt)}</small><button onClick={()=>{setValue(record);notify('已载入完整记录和结论');}}>查看 / 编辑</button></article>)}</div> : <Empty icon={<Copy size={31}/>} title="尚无对比记录">完成一次检索后，把两侧题录粘贴到左侧并保存。</Empty>}</aside></section>
  </div>;
}
