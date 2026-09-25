import {useEffect, useMemo, useState} from 'react';
import {ArrowClockwise, ArrowSquareOut, DownloadSimple, MagnifyingGlass, Prohibit, SpinnerGap} from '@phosphor-icons/react';
import {api, type Data, useErrorText} from './api';
import './relationDiscovery.css';

const directionLabel: Record<string, string> = {references: '参考文献', citing: '后续引用', related: '主题推荐'};
const pdfLink = (url: string) => /\.pdf(?:$|[?#])|\/pdf\//i.test(url);

export function RelationDiscovery({session, rows, selectedId, onRows, onSelect, onSessionChanged, notify, fail}: {
  session: Data; rows: Data[]; selectedId?: string | null; onRows: (rows: Data[]) => void;
  onSelect: (row: Data) => void; onSessionChanged: () => void;
  notify: (message: string) => void; fail: (error: any) => void;
}) {
  const [anchor, setAnchor] = useState(String(session.items[0]?.itemId || ''));
  const [direction, setDirection] = useState('references');
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobText, setJobText] = useState('');
  const [busyId, setBusyId] = useState('');
  const [view, setView] = useState('all');
  const [reason, setReason] = useState('');
  const [page, setPage] = useState<Data>({nextOffset: 0, hasMore: true});
  const [error, setError] = useState('');
  useEffect(() => {setAnchor(String(session.items[0]?.itemId || '')); setJobId(null); setView('all');}, [session.id]);
  useEffect(() => {setReason(rows.find(row => row.id === selectedId)?.decisionReason || '');}, [selectedId]);
  useEffect(() => {if (anchor) api('relations.discoveryProgress', {sessionId: session.id, itemId: anchor, direction}).then(setPage).catch(fail);}, [session.id, anchor, direction, rows]);
  useEffect(() => {
    if (!jobId) return;
    let alive = true;
    const refresh = async () => {
      try {
        const jobs: Data[] = await api('jobs.list');
        const job = jobs.find(value => value.id === jobId);
        if (!alive || !job) return;
        setJobText(job.message || '正在检索…');
        if (['completed', 'failed', 'cancelled'].includes(job.state)) {
          setJobId(null);
          if (job.state === 'completed') {
            onRows(await api('relations.discoveries', {sessionId: session.id}));
            setPage(await api('relations.discoveryProgress', {sessionId: session.id, itemId: anchor, direction}));
            notify(`已保存 ${job.result?.received || 0} 篇来源记录`);
          } else setError(job.error?.message || '发现任务未完成');
        }
      } catch (exception) {if (alive) {setJobId(null); setError(useErrorText(exception));}}
    };
    refresh();
    const timer = window.setInterval(refresh, 1200);
    return () => {alive = false; window.clearInterval(timer);};
  }, [jobId, session.id]);
  const visible = useMemo(() => rows.filter(row => view === 'all' || (view === 'later' ? row.status === 'later' : row.direction === view)), [rows, view]);
  const counts = useMemo(() => ({citation: rows.filter(row => row.direction !== 'related').length,
    pending: rows.filter(row => row.status === 'pending' && !row.importedItemId).length}), [rows]);

  async function discover() {
    setError('');
    try {
      const result = await api('relations.discover', {sessionId: session.id, itemId: anchor, direction, limit: 30});
      setJobId(result.jobId); setJobText('正在核对种子论文…');
    } catch (exception) {setError(useErrorText(exception));}
  }
  async function decide(row: Data, status: string) {
    setBusyId(row.id); setError('');
    try {
      const result = await api('relations.discoveryDecide', {sessionId: session.id, itemId: row.anchorItemId,
        workId: row.workId, direction: row.direction, status, reason: selectedId === row.id ? reason : ''});
      onRows(await api('relations.discoveries', {sessionId: session.id}));
      if (status === 'saved') {
        if (row.oaUrl && pdfLink(row.oaUrl)) {
          try {await api('attachments.download', {itemId: result.itemId, url: row.oaUrl});
            notify('已加入文献库和分析集，公开 PDF 下载已排队');}
          catch (exception) {notify(`已保存文献；PDF 下载未启动：${useErrorText(exception)}`);}
        } else notify('已加入文献库和分析集；可在全文获取中继续查找 PDF');
        onSessionChanged();
      } else notify(status === 'ignored' ? '已忽略该发现' : status === 'later' ? '已加入稍后阅读' : '已恢复为待处理');
    } catch (exception) {fail(exception);} finally {setBusyId('');}
  }

  return <section className="relation-discovery" aria-label="种子论文发现">
    <div className="relation-discovery-heading"><div><span>04 · DISCOVER</span><h3>沿种子论文继续发现</h3><p>真实引文来自 OpenAlex 的参考文献匹配；主题推荐是算法推荐，不表示存在引用。</p></div><b>{counts.citation} 条引文 · {counts.pending} 篇待处理</b></div>
    <div className="relation-discovery-controls"><select aria-label="选择种子论文" disabled={!!jobId} value={anchor} onChange={event => setAnchor(event.target.value)}>{session.items.map((item: Data) => <option key={item.itemId} value={item.itemId}>{item.title}</option>)}</select><select aria-label="发现方向" disabled={!!jobId} value={direction} onChange={event => setDirection(event.target.value)}>{Object.entries(directionLabel).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><button disabled={!!jobId || !anchor || !page.hasMore} onClick={discover}>{jobId ? <SpinnerGap className="spin"/> : <MagnifyingGlass/>}{jobId ? '正在发现' : !page.hasMore ? '已到最后一批' : page.nextOffset ? '继续发现 30 篇' : '联网发现 30 篇'}</button><button aria-label="刷新发现结果" onClick={() => api('relations.discoveries', {sessionId: session.id}).then(onRows).catch(fail)}><ArrowClockwise/></button></div>
    {jobId && <p className="relation-discovery-job">{jobText}</p>}{error && <p className="relation-discovery-error">{error}</p>}
    <div className="relation-discovery-tabs">{[['all','全部'],['references','参考文献'],['citing','后续引用'],['related','主题推荐'],['later','稍后阅读']].map(([key,label]) => <button key={key} className={view === key ? 'active' : ''} onClick={() => setView(key)}>{label}</button>)}</div>
    {visible.length ? <div className="relation-discovery-list">{visible.map(row => <article key={row.id} className={(selectedId === row.id ? 'selected ' : '') + (row.status === 'ignored' ? 'ignored' : '')} onClick={() => {onSelect(row); setReason(row.decisionReason || '');}}><div className="relation-discovery-row-head"><span className={'relation-discovery-kind ' + row.direction}>{directionLabel[row.direction]}</span><small>{row.year || '年份未知'} · 引用 {row.citationCount || 0} · {row.source}</small></div><h4>{row.title}</h4><p>{row.authors?.slice(0, 3).join('、') || '作者待补充'}{row.venue ? ` · ${row.venue}` : ''}</p><p className="relation-discovery-abstract">{row.abstract || '来源未提供摘要。'} </p>{selectedId === row.id && <input className="relation-discovery-reason" aria-label="记录筛选理由" maxLength={1000} value={reason} placeholder="可选：记录收集或排除理由" onClick={event => event.stopPropagation()} onChange={event => setReason(event.target.value)}/>}<div className="relation-discovery-actions"><span>{row.importedItemId ? '文献库已有' : row.status === 'ignored' ? '已忽略' : row.status === 'later' ? '稍后阅读' : '待收集'}</span><button disabled={busyId === row.id || row.status === 'saved'} onClick={event => {event.stopPropagation(); decide(row, 'saved');}}><DownloadSimple/>{row.status === 'saved' ? '已保存' : row.importedItemId ? '加入分析集' : '保存到文献库'}</button>{row.status !== 'saved' && row.status !== 'later' && <button disabled={busyId === row.id} onClick={event => {event.stopPropagation(); decide(row, 'later');}}>稍后阅读</button>}{row.status === 'ignored' ? <button onClick={event => {event.stopPropagation(); decide(row, 'pending');}}>恢复</button> : <button disabled={busyId === row.id} onClick={event => {event.stopPropagation(); decide(row, 'ignored');}}><Prohibit/>忽略</button>}<button onClick={event => {event.stopPropagation(); window.research.external(row.sourceUrl);}}><ArrowSquareOut/>来源</button></div></article>)}</div> : <p className="relation-discovery-empty">选择一篇种子论文和发现方向，获取可核对的引文或主题推荐。</p>}
  </section>;
}
