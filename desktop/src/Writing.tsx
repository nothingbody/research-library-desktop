import {useCallback, useEffect, useState} from 'react';
import {ArrowsClockwise, Books, CheckCircle, Copy, WarningCircle} from '@phosphor-icons/react';
import {api, files, type Data, dateText, useErrorText} from './api';
import {Busy, Empty, ErrorBox} from './ui';

export function Writing({tick, notify}: {tick: number; notify: (message: string) => void}) {
  const [status, setStatus] = useState<Data | null>(null), [sessions, setSessions] = useState<Data[]>([]), [error, setError] = useState(''), [loading, setLoading] = useState(true);
  const refresh = useCallback(() => {setLoading(true); setError(''); Promise.all([api('writing.status'), api('writing.sessions')]).then(([bridge, values]) => {setStatus(bridge); setSessions(values);}).catch(e => setError(useErrorText(e))).finally(() => setLoading(false));}, []);
  useEffect(refresh, [refresh, tick]);
  const rotate = async () => {try {setStatus(await api('writing.newPairing')); notify('已生成新的六位配对码');} catch (e) {setError(useErrorText(e));}};
  const reveal = async () => {try {await files('writingAddin'); notify('已打开 Word 加载项清单所在位置');} catch (e) {setError(useErrorText(e));}};
  if (loading && !status) return <Busy text="正在检查写作插件…"/>;
  return <div className="module-page writing-page"><div className="page-heading"><div className="eyebrow">WRITE WITH YOUR LIBRARY</div><h1>写作引用</h1><p>在 Word 中搜索本地文献，插入可更新的正文引文与参考文献。</p></div>
    {error && <ErrorBox message={error} retry={refresh}/>}
    <section className="panel writing-connection"><div><h2><Books/>本机写作桥接</h2><p>只绑定本机回环地址；文献、PDF 和访问密钥不会发送到网络。</p></div><div className={status?.running ? 'writer-live' : 'writer-live error'}>{status?.running ? <CheckCircle/> : <WarningCircle/>}{status?.running ? `已运行 · 端口 ${status.port}` : status?.error || '未运行'}</div></section>
    <section className="writing-grid"><article className="panel host-card"><div className="host-card-heading"><div><small>MICROSOFT WORD</small><h2>Word 任务窗格</h2></div>{status?.hosts?.word?.installed ? <span className="success">已检测到</span> : <span className="muted-chip">未检测到</span>}</div><p>{status?.hosts?.word?.installed ? '已找到当前机器上的 Word。上传加载项清单后，可在任务窗格中使用配对码连接本地文献库。' : '未找到 Word 安装位置；仍可准备加载项清单。'}</p><button onClick={reveal}><Copy/>打开加载项清单</button></article></section>
    <section className="panel pairing-panel"><div><h2>连接任务窗格</h2><p>先保持文献工作台运行，再在 Word 任务窗格中输入六位配对码。重新生成后，旧任务窗格需要重新连接。</p></div><div className="pairing-code"><strong>{status?.running ? status?.pairingCode || '——' : '——'}</strong><button className="icon" aria-label="生成新配对码" onClick={rotate} disabled={!status?.running}><ArrowsClockwise/></button></div></section>
    <section className="panel writing-guide"><h2>Word 使用方式</h2><ol><li>在 Word 的“我的加载项”中上传 <code>word-manifest.xml</code>。</li><li>打开“文献工作台写作引用”任务窗格，输入上方配对码。</li><li>检索文献，设置页码、前后缀和作者显示，再插入动态引文。</li><li>在文末插入参考文献；题录或样式变化后点击“刷新全文”。</li></ol></section>
    <section className="panel writing-sessions"><div className="section-heading"><div><h2>最近写作文档</h2><p>本地索引用于诊断与恢复；引文状态也保存于文档内部。</p></div><button onClick={refresh}><ArrowsClockwise/>刷新</button></div>{sessions.length ? <div className="writing-session-list">{sessions.map(session => <article key={session.id}><div><strong>{session.documentName || '未命名文档'}</strong><small>{session.host === 'word' ? 'Word' : '历史宿主记录'} · {session.citationCount} 处引文 · {session.styleId}</small></div><time>{dateText(session.updatedAt)}</time></article>)}</div> : <Empty icon={<Books size={32}/>} title="尚未连接写作文档">连接任务窗格并插入第一处动态引文后，会在这里显示本地会话记录。</Empty>}</section>
  </div>;
}
