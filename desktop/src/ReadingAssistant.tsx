import {useEffect, useMemo, useState} from 'react';
import {Brain, Lightbulb, ListChecks, Notebook, Sparkle, Translate} from '@phosphor-icons/react';
import {api, type Data} from './api';
import {Busy} from './ui';
import './readingAssistant.css';

const taskButtons: [string, string, any][] = [['translate', '对照翻译', Translate], ['summarize', '总结原文', ListChecks], ['analyze', '分析原文', Brain], ['explain', '讲解原文', Lightbulb]];
const taskLabel: Data = {translate: '对照翻译', summarize: '总结', analyze: '分析', explain: '讲解', question: '原文问答'};
const scopeLabel: Data = {selection: '选中的句子或段落', page: '当前 PDF 页面', paper: '整篇 PDF'};

export function ReadingAssistant({itemId, attachmentId, page, pageCount, selection, notify, fail}: {itemId: string; attachmentId: string; page: number; pageCount: number; selection: Data | null; notify: (text: string) => void; fail: (error: any) => void}) {
  const [loading, setLoading] = useState(true), [runs, setRuns] = useState<Data[]>([]), [scope, setScope] = useState('selection'), [question, setQuestion] = useState('');
  const selected = (selection?.quote || '').trim();
  const activeRun = useMemo(() => runs.find(run => run.state === 'running' || run.state === 'pending') || null, [runs]);
  const latest = useMemo(() => runs.find(run => run.result) || null, [runs]);
  const latestTranslation = useMemo(() => runs.find(run => run.task === 'translate' && run.result) || null, [runs]);
  async function load() {try {setRuns(await api('assistant.runs', {itemId, publicOnly: true}));} catch (error) {fail(error);} finally {setLoading(false);}}
  useEffect(() => {setLoading(true); load();}, [itemId, attachmentId]);
  useEffect(() => {const timer = setInterval(() => api('assistant.runs', {itemId, publicOnly: true}).then(setRuns).catch(() => {}), activeRun ? 1200 : 5000); return () => clearInterval(timer);}, [itemId, activeRun]);
  async function run(task: string) {
    if (scope === 'selection' && !selected) {notify('请先在 PDF 中选中一句或一段原文。'); return;}
    if (task === 'translate' && scope === 'paper') {notify('整篇翻译请按页进行，以便原文和译文准确对照。'); return;}
    try {
      const value = await api('assistant.run', {task, itemId, attachmentId, scope, text: scope === 'selection' ? selected : '', page, question: question.trim()});
      setRuns(old => [{id: value.runId, task, state: 'pending', input: {text: scope === 'selection' ? selected : '', page, scope}, createdAt: new Date().toISOString()}, ...old]);
      notify(task === 'translate' ? '对照翻译已开始，可继续阅读。' : `${scopeLabel[scope]}的 AI 解读已开始，可继续阅读。`);
    } catch (error) {fail(error);}
  }
  if (loading) return <aside className="reader-assistant"><Busy text="正在载入 AI 解读…"/></aside>;
  const sourceText = scope === 'selection' ? selected : scope === 'page' ? `将读取第 ${page} 页的已索引文字` : `将读取整篇 PDF 的已索引文字（共 ${pageCount || '—'} 页）`;
  const translationSource = String(latestTranslation?.input?.text || '').replace(/^\[第 \d+ 页\]\n/, '');
  return <aside className={'reader-assistant ' + (latestTranslation ? 'translation-panel' : '')} aria-label="AI 辅助阅读侧栏"><div className="reader-assistant-scroll ai-reading">
    <header className="ai-reading-header"><Sparkle/><div><strong>AI 原文解读</strong><small>总结、分析、讲解你正在阅读的内容</small></div></header>
    <section className="ai-reading-scope"><strong>解读范围</strong><div>{[['selection','句子 / 段落'],['page','当前页'],['paper','整篇文章']].map(([key, label]) => <button key={key} className={scope === key ? 'active' : ''} onClick={() => setScope(key)}>{label}</button>)}</div><small>对照翻译支持选中原文和当前页；整篇文章请逐页翻译。</small></section>
    <section className="selected-source"><div><strong>{scopeLabel[scope]}</strong><small>{scope === 'selection' ? (selected ? `${selected.length} 个字符 · 第 ${page} 页` : '请在 PDF 中选中要解读的文字') : sourceText}</small></div>{scope === 'selection' && selected && <p>{selected}</p>}</section>
    <div className="assistant-actions">{taskButtons.map(([key, label, Icon]) => <button key={key} disabled={(scope === 'selection' && !selected) || !!activeRun || (key === 'translate' && scope === 'paper')} onMouseDown={e => e.preventDefault()} onClick={() => run(key)}><Icon/>{label}</button>)}</div>
    <label className="reading-field">希望重点关注什么？<textarea aria-label="AI 解读关注点" rows={3} value={question} placeholder="可选，例如：请重点讲解这段方法的假设和适用条件" onChange={e => setQuestion(e.target.value)}/></label>
    <button className="question-button" disabled={(scope === 'selection' && !selected) || !!activeRun || !question.trim()} onMouseDown={e => e.preventDefault()} onClick={() => run('question')}><Brain/>围绕原文回答我的问题</button>
    {activeRun && <div className="assistant-running"><Sparkle className="spin"/>正在{taskLabel[activeRun.task] || '解读'}{scopeLabel[activeRun.input?.scope || scope]}；可以继续阅读。</div>}
    {latestTranslation?.result && <section className="translation-compare"><div className="section-heading"><h3><Translate/>原文对照翻译</h3><small>{scopeLabel[latestTranslation.result.scope || latestTranslation.input?.scope || 'selection']}{latestTranslation.result.page ? ` · 第 ${latestTranslation.result.page} 页` : ''}</small></div><div className="translation-columns"><article><strong>原文</strong><p>{translationSource}</p></article><article><strong>中文翻译</strong><p>{latestTranslation.result.content}</p></article></div><div className="translation-actions"><button onClick={() => navigator.clipboard.writeText(latestTranslation.result.content).then(() => notify('译文已复制')).catch(fail)}>复制译文</button><button onClick={() => api('assistant.apply', {runId: latestTranslation.id, target: 'note'}).then(() => notify('已保存为原文与译文对照笔记')).catch(fail)}><Notebook/>保存对照笔记</button></div></section>}
    {latest?.result && latest.task !== 'translate' && <section className="assistant-result"><div className="section-heading"><h3>{latest.result.label || taskLabel[latest.task]}结果</h3><small>{scopeLabel[latest.result.scope || latest.input?.scope || 'selection']} {latest.result.scope === 'page' ? `· 第 ${latest.result.page || latest.input?.page || '—'} 页` : ''}</small></div><p>{latest.result.content}</p><button onClick={() => api('assistant.apply', {runId: latest.id, target: 'note'}).then(() => notify('已保存为带原文链接的阅读笔记')).catch(fail)}><Notebook/>保存为笔记</button></section>}
    {runs.find(run => run.error)?.error && <p className="assistant-error">{runs.find(run => run.error)?.error?.message || 'AI 解读失败，可重试。'}</p>}
    <p className="assistant-privacy">联网时仅发送本次选择的原文、当前页或整篇已索引 PDF 正文，以及你填写的关注点；访问密钥不会写入文献库。</p>
  </div></aside>;
}
