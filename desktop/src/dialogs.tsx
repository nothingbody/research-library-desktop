import {useEffect, useState} from 'react';
import {ArrowSquareOut, Check, Clipboard, DownloadSimple, FilePdf, WarningCircle} from '@phosphor-icons/react';
import {api, files, type Data, authorText, yearText} from './api';
import {Modal, Busy, Fields} from './ui';
type Common = {close: () => void; done: () => void; fail: (error: any) => void};

export function ItemEditor({item, collectionId, close, done, fail}: Common & {item?: Data; collectionId?: string | null}) {
  const [data, setData] = useState<Data>({...item, title: item?.title || '', authorInput: (item?.author || []).map((a: Data) => a.literal ? '{' + a.literal + '}' : [a.family, a.given].filter(Boolean).join(', ')).join('; '), yearInput: item ? yearText(item) : '', tagsInput: (item?.tags || []).join(', ')});
  const [busy, setBusy] = useState(false), [advanced, setAdvanced] = useState(false), [raw, setRaw] = useState('');
  const patch = () => {
    const value: Data = {};
    for (const k of ['title', 'type', 'container-title', 'DOI', 'ISSN', 'ISBN', 'volume', 'issue', 'page', 'publisher', 'URL', 'abstract', 'citationKey']) if (data[k] !== undefined) value[k] = data[k];
    value.author = data.authorInput.split(/[;；]/).map((x: string) => x.trim()).filter(Boolean).map((x: string) => {
      if (x.startsWith('{') && x.endsWith('}')) return {literal: x.slice(1, -1)};
      if (x.includes(',')) {const [family, ...given] = x.split(','); return {family: family.trim(), given: given.join(',').trim()};}
      const parts = x.split(' '); return {family: parts.pop(), given: parts.join(' ')};
    });
    value.issued = data.yearInput ? {'date-parts': [[Number(data.yearInput)]]} : {};
    value.tags = data.tagsInput.split(/[,，]/).map((x: string) => x.trim()).filter(Boolean);
    return value;
  };
  async function save() {setBusy(true); try {const value = advanced ? JSON.parse(raw) : patch(); if (item) await api('items.update', {id: item.id, revision: item.revision, patch: value}); else await api('items.create', {data: value, collectionId}); done();} catch (e) {fail(e);} finally {setBusy(false);}}
  return <Modal title={item ? '编辑文献信息' : '手动添加文献'} close={close} wide><div className="dialog-subtitle">题录保存在本地，可随时补全或修正。{item && <code>{item.citationKey}</code>}</div><div className="tab-switch"><button className={!advanced ? 'active' : ''} onClick={() => setAdvanced(false)}>常用字段</button><button className={advanced ? 'active' : ''} onClick={() => {setRaw(JSON.stringify(patch(), null, 2)); setAdvanced(true);}}>CSL 字段</button></div>{advanced ? <textarea className="code-editor" aria-label="CSL JSON 字段" value={raw} onChange={e => setRaw(e.target.value)}/> : <div className="form-grid"><label className="full">标题<input value={data.title} onChange={e => setData({...data, title: e.target.value})}/></label><label>文献类型<select value={data.type || 'article-journal'} onChange={e => setData({...data, type: e.target.value})}><option value="article-journal">期刊论文</option><option value="paper-conference">会议论文</option><option value="book">图书</option><option value="chapter">书籍章节</option><option value="thesis">学位论文</option><option value="report">报告</option><option value="manuscript">预印本</option><option value="webpage">网页</option><option value="document">其他文档</option></select></label><label>出版年份<input type="number" min="1" max="9999" value={data.yearInput} onChange={e => setData({...data, yearInput: e.target.value})}/></label><label className="full">作者 <small>分号分隔；支持“姓, 名”；机构用花括号包围</small><input value={data.authorInput} onChange={e => setData({...data, authorInput: e.target.value})}/></label>{[['container-title', '期刊 / 来源'], ['DOI', 'DOI'], ['ISSN', 'ISSN'], ['ISBN', 'ISBN'], ['volume', '卷'], ['issue', '期'], ['page', '页码'], ['publisher', '出版商'], ['URL', '网址'], ['citationKey', '引用键（留空自动生成）']].map(([key, label]) => <label key={key}>{label}<input value={data[key] || ''} onChange={e => {const next = {...data, [key]: e.target.value}; if (key === 'citationKey' && !e.target.value && !item) delete next.citationKey; setData(next);}}/></label>)}<label className="full">标签<input placeholder="使用逗号分隔" value={data.tagsInput} onChange={e => setData({...data, tagsInput: e.target.value})}/></label><label className="full">摘要<textarea rows={5} value={data.abstract || ''} onChange={e => setData({...data, abstract: e.target.value})}/></label></div>}<div className="dialog-actions"><button onClick={close}>取消</button><button className="primary" disabled={busy || (!advanced && !data.title.trim())} onClick={save}>{busy ? '保存中…' : '保存文献'}</button></div></Modal>;
}

export function ImportPreview({preview, collections, collectionId, close, done, fail}: Common & {preview: Data; collections: Data[]; collectionId?: string | null}) {
  const [keys, setKeys] = useState<string[]>(preview.entries.map((e: Data) => e.key)), [collection, setCollection] = useState(collectionId || ''), [mode, setMode] = useState('managed'), [keep, setKeep] = useState(false), [busy, setBusy] = useState(false);
  useEffect(() => {api('settings.get').then(s => setMode(s.attachmentMode || 'managed')).catch(fail);}, [fail]);
  async function commit() {setBusy(true); try {await api('imports.commit', {batchId: preview.batchId, keys, collectionId: collection || null, mode, keepDuplicates: keep}); done();} catch(e) {fail(e);} finally {setBusy(false);}}
  return <Modal title="导入预览" close={close} wide><p className="muted">解析到 {preview.entries.length} 条题录。重复项默认归入已有文献；PDF 会建立全文索引。</p>{preview.errors.map((e: Data, i: number) => <div className="error-box" key={i}><WarningCircle/>{e.fileName}：{e.message}</div>)}<div className="import-list">{preview.entries.map((e: Data) => <label key={e.key} className="import-entry"><input type="checkbox" checked={keys.includes(e.key)} onChange={event => setKeys(old => event.target.checked ? [...old, e.key] : old.filter(k => k !== e.key))}/><FilePdf size={23}/><div><strong>{e.data.title}</strong><small>{e.fileName} · {authorText(e.data) || '作者待补充'} {yearText(e.data)}</small>{e.warning && <small className="amber">{e.warning}</small>}{e.duplicate && <small className="blue">重复候选：{e.duplicate.title}</small>}</div></label>)}</div><div className="form-grid"><label>加入集合<select value={collection} onChange={e => setCollection(e.target.value)}><option value="">仅加入我的文献</option>{collections.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label><label>附件保存方式<select value={mode} onChange={e => setMode(e.target.value)}><option value="managed">复制到文献库（推荐）</option><option value="linked">链接原文件</option></select></label><label className="check-label full"><input type="checkbox" checked={keep} onChange={e => setKeep(e.target.checked)}/>为重复候选也创建独立条目</label></div><div className="dialog-actions"><button onClick={close}>取消</button><button className="primary" disabled={!keys.length || busy} onClick={commit}>导入所选 {keys.length} 项</button></div></Modal>;
}

const metadataLabels: Data = {title: '标题', author: '作者', issued: '出版日期', 'container-title': '期刊', DOI: 'DOI', ISSN: 'ISSN', ISBN: 'ISBN', volume: '卷', issue: '期', page: '页码', publisher: '出版商', abstract: '摘要', URL: '网址', type: '文献类型'};
function display(value: any) {return typeof value === 'object' ? JSON.stringify(value) : String(value || '—');}
export function MetadataDialog({item, collectionId, close, done, fail}: Common & {item?: Data; collectionId?: string | null}) {
  const [identifier, setIdentifier] = useState(item?.DOI || ''), [result, setResult] = useState<Data | null>(null), [selected, setSelected] = useState<string[]>([]), [busy, setBusy] = useState(false);
  async function search() {setBusy(true); try {const r = await api('metadata.lookup', {identifier}); setResult(r); setSelected(Object.keys(metadataLabels).filter(k => r.data[k] && (!item || !item[k])));} catch(e) {fail(e);} finally {setBusy(false);}}
  async function save() {setBusy(true); try {const data = item ? Object.fromEntries(selected.map(k => [k, result!.data[k]])) : result!.data; if (item) await api('metadata.apply', {id: item.id, revision: item.revision, patch: data, source: result!.source}); else await api('items.create', {data, collectionId}); done();} catch(e) {fail(e);} finally {setBusy(false);}}
  return <Modal title={item ? '补全文献信息' : '通过 DOI / PMID 添加'} close={close} wide><p className="muted">从 Crossref 或 PubMed 获取公开题录，保存前可核对字段。</p><div className="inline-form"><input aria-label="DOI 或 PMID" placeholder="输入 DOI、doi.org 链接，或 PMID:12345678" value={identifier} onChange={e => setIdentifier(e.target.value)} onKeyDown={e => e.key === 'Enter' && search()}/><button className="primary" disabled={busy || !identifier.trim()} onClick={search}>获取元数据</button></div>{busy && !result && <Busy text="正在查询公开元数据…"/>}{result && <><div className="source-line">来源：{result.source}<button className="link-button" onClick={() => window.research.external(result.sourceUrl)}>查看来源<ArrowSquareOut size={14}/></button></div>{item ? <div className="metadata-diff"><div className="diff-head"><span>字段</span><span>当前内容</span><span>获取内容</span></div>{Object.entries(metadataLabels).filter(([k]) => result.data[k]).map(([key, label]) => <label key={key}><span><input type="checkbox" checked={selected.includes(key)} onChange={e => setSelected(old => e.target.checked ? [...old, key] : old.filter(x => x !== key))}/>{label}</span><span>{display(item[key])}</span><span>{display(result.data[key])}</span></label>)}</div> : <Fields value={Object.fromEntries(Object.keys(metadataLabels).map(k => [k, result.data[k]]))} labels={metadataLabels}/>}<div className="notice">{item ? '只更新勾选字段。已有内容默认保留，勾选后才覆盖。' : '请核对作者、出版日期与题名后再加入文献库。'}</div></>}<div className="dialog-actions"><button onClick={close}>取消</button><button className="primary" disabled={!result || busy || (!!item && !selected.length)} onClick={save}><Check/> {item ? '应用所选字段' : '加入文献库'}</button></div></Modal>;
}

export function CitationDialog({ids, close, notify, fail}: {ids: string[]; close: () => void; notify: (message: string) => void; fail: (e: any) => void}) {
  const [styles, setStyles] = useState<Data[]>([]), [language, setLanguage] = useState('zh-CN');
  const [style, setStyle] = useState('gb-t-7714-2015'), [text, setText] = useState(''), [format, setFormat] = useState('bibtex');
  const [browse, setBrowse] = useState(false), [styleQuery, setStyleQuery] = useState(''), [matches, setMatches] = useState<Data[]>([]);
  const [styleError, setStyleError] = useState(''), [searching, setSearching] = useState(false), [installing, setInstalling] = useState('');
  useEffect(() => {api('citation.styles').then(setStyles).catch(fail);}, [fail]);
  useEffect(() => {let cancelled = false; api('citation.format', {ids, style, lang: language}).then(result => {if (!cancelled) setText(result.text);}).catch(fail); return () => {cancelled = true;};}, [ids, style, language, fail]);
  useEffect(() => {
    if (!browse || styleQuery.trim().length < 2) {setMatches([]); setStyleError(''); setSearching(false); return;}
    let cancelled = false;
    const timer = setTimeout(() => {
      setSearching(true); setStyleError('');
      api('citation.searchStyles', {query: styleQuery}).then(result => {if (!cancelled) setMatches(result);})
        .catch(error => {if (!cancelled) setStyleError(String(error?.message || error));})
        .finally(() => {if (!cancelled) setSearching(false);});
    }, 250);
    return () => {cancelled = true; clearTimeout(timer);};
  }, [browse, styleQuery]);
  async function installStyle(match: Data) {
    if (match.installedId) {setStyle(match.installedId); setBrowse(false); return;}
    setInstalling(match.path); setStyleError('');
    try {
      const added = await api('citation.installStyle', {path: match.path});
      setStyles(await api('citation.styles')); setStyle(added.id); setBrowse(false);
      notify(`已安装引文样式：${added.name}`);
    } catch (error: any) {setStyleError(String(error?.message || error));}
    finally {setInstalling('');}
  }
  return <Modal title={`引用与导出 · ${ids.length} 篇文献`} close={close} wide>
    <div className="inline-form"><label>引文样式</label><select value={style} onChange={event => setStyle(event.target.value)}>{styles.map(entry => <option key={entry.id} value={entry.id}>{entry.name}</option>)}</select><select aria-label="引用语言" value={language} onChange={event => setLanguage(event.target.value)}><option value="zh-CN">中文</option><option value="en-US">English</option></select><button onClick={() => setBrowse(value => !value)}>在线查找样式</button><button onClick={() => files('citationStyle').then(result => {if (result) {api('citation.styles').then(setStyles); setStyle(result.id);}}).catch(fail)}>导入 CSL 文件</button><button onClick={() => window.research.clipboard(text).then(() => notify('引用已复制')).catch(fail)}><Clipboard/>复制引用</button></div>
    {browse && <div className="citation-style-browser"><label>搜索官方 CSL 样式库<input aria-label="搜索引文样式" value={styleQuery} onChange={event => setStyleQuery(event.target.value)} placeholder="输入样式或期刊英文名，如 nature、chicago" autoFocus/></label><p>样式来自 <button className="inline-link" onClick={() => window.research.external('https://citationstyles.org/')}>Citation Style Language</button> 项目，安装后保存在本机。期刊变体会使用其官方母样式。</p>{searching && <small>正在搜索…</small>}{styleError && <div className="error-box">{styleError}</div>}<div className="citation-style-results">{matches.map(match => <button key={match.path} disabled={!!installing} onClick={() => installStyle(match)}><strong>{match.name}</strong><small>{match.dependent ? '期刊变体' : '独立样式'} · {match.installedId ? '已安装' : installing === match.path ? '安装中…' : '点击安装'}</small></button>)}{styleQuery.trim().length >= 2 && !matches.length && !searching && !styleError && <small>未找到匹配样式，请换用期刊英文名或缩短关键词。</small>}</div></div>}
    <pre className="citation-preview">{text || '正在生成引用…'}</pre><p className="muted">导出的题录可交给 Zotero、JabRef 或 Word 的文献工具继续使用。</p><div className="dialog-actions"><select aria-label="导出格式" value={format} onChange={event => setFormat(event.target.value)}><option value="bibtex">BibTeX</option><option value="biblatex">BibLaTeX</option><option value="ris">RIS</option><option value="json">CSL JSON</option></select><button className="primary" onClick={() => files('export', {ids, format}).then(result => result && notify('已导出至 ' + result.path)).catch(fail)}><DownloadSimple/>导出题录</button></div>
  </Modal>;
}

export function CollectionDialog({value, collections, close, done, fail}: Common & {value: Data; collections: Data[]}) {
  const [name, setName] = useState(value.name || ''), [parent, setParent] = useState(value.parentId || ''), [confirm, setConfirm] = useState(false), [busy, setBusy] = useState(false);
  async function save(remove = false) {setBusy(true); try {if (remove) await api('collections.edit', {action: 'delete', id: value.id}); else if (value.id) {await api('collections.edit', {action: 'move', id: value.id, parentId: parent || null}); await api('collections.edit', {action: 'rename', id: value.id, name});} else await api('collections.edit', {action: 'create', name, parentId: parent || null}); done();} catch(e) {fail(e);} finally {setBusy(false);}}
  return <Modal title={value.id ? '管理集合' : '新建集合'} close={close}><div className="form-grid"><label className="full">集合名称<input value={name} onChange={e => setName(e.target.value)}/></label><label className="full">上级集合<select value={parent} onChange={e => setParent(e.target.value)}><option value="">我的集合</option>{collections.filter(c => c.id !== value.id).map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label></div>{confirm && <p className="notice">删除集合后，文献仍保留在“我的文献”；子集合移到上一级。</p>}<div className="dialog-actions">{value.id && <button className="danger" disabled={busy} onClick={() => confirm ? save(true) : setConfirm(true)}>{confirm ? '确认删除集合' : '删除集合'}</button>}<div className="spacer"/><button onClick={close}>取消</button><button className="primary" disabled={!name.trim() || busy} onClick={() => save()}>保存</button></div></Modal>;
}
