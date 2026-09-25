import {useState} from 'react';
import {ArrowSquareOut, BookOpen, ChatCircleText, FileText} from '@phosphor-icons/react';
import type {Data} from './api';
import {Empty} from './ui';
import {type Fact, availableSources, feedbackView, journalView, list, main, missing, record, relatedView, sourceFacts, sourceNames, submissionView, yearLabel} from './journalPresentation';
import './journalDetails.css';

type Fail = (error: any) => void;
export function FactList({facts, columns = false}: {facts: Fact[]; columns?: boolean}) {
  return <dl className={'journal-facts' + (columns ? ' two-columns' : '')}>{facts.map((f,i)=><div key={f.label+i} data-fact={f.label}>
    <dt>{f.label}</dt><dd className={f.value===missing?'unavailable':''}>{f.href ? <a href={f.href} target="_blank" rel="noreferrer">{f.value}<ArrowSquareOut size={13}/></a> : f.value}{f.note && <small>{f.note}</small>}</dd>
  </div>)}</dl>;
}
export function JournalOverview({data}: {data:Data}) {
  const view=journalView(data);
  return <div className="journal-overview-grid">
    <section className="panel journal-basics"><div className="section-heading"><h2>基本信息</h2><span className="muted">期刊档案</span></div><FactList facts={view.basic} columns/></section>
    <section className="panel journal-access"><h2>开放获取与收录</h2><FactList facts={view.access}/>{view.oaConflict && <p className="journal-source-note">不同来源的开放获取记录存在差异，以上分别保留各来源状态。</p>}<p className="journal-source-note">开放获取与 DOAJ 收录是两个独立项目。</p></section>
  </div>;
}
const descriptions:Record<string,string>={jcr:'影响因子与学科分区',fqb:'大类、小类分区与 TOP 期刊',xr:'学科分区与收录索引',scimago:'SJR、分区与引文统计',cwts:'标准化引文指标',ccf:'计算机学科推荐目录',gjqk:'按年份保存的期刊预警信息',openalex:'论文与引用的累计统计',doaj:'开放获取目录资料',crossref:'出版与 DOI 登记信息',jufo:'出版渠道评价与资料',wikidata:'期刊名称、出版与收录资料'};
function SourceRecord({source,data}:{source:string;data:Data}) {
  const [year,setYear]=useState('');
  const records=list(data.sources?.[source]?.all_years), years=[...new Set(records.map(r=>String(r.year)).filter(v=>/^\d{4}$/.test(v)))].sort().reverse();
  const current=main(data,source), selected=year ? records.filter(r=>String(r.year)===year) : [current];
  const headingYear=year || current.year;
  return <section className="panel journal-source-panel"><div className="section-heading"><div><h2>{sourceNames[source]} {headingYear && <span className="chip">{yearLabel(headingYear)}</span>}</h2><p className="muted">{descriptions[source]}</p></div>{years.length>0 && <label className="source-year">数据年份<select aria-label="数据源年份" value={year} onChange={e=>setYear(e.target.value)}><option value="">当前记录{current.year?' · '+current.year:''}</option>{years.map(y=><option key={y} value={y}>{y} 年</option>)}</select></label>}</div>
    {selected.map((r,i)=><div className="source-record" key={i}>{selected.length>1 && <h3>学科记录 {i+1}</h3>}<FactList facts={sourceFacts(source,record(r),data)} columns/></div>)}
    <p className="journal-source-note">本页只显示该来源的资料；其他来源的指标保留在各自页面。完整响应可在“原始数据”查看。</p>
  </section>;
}
export function JournalSources({data}:{data:Data}) {
  const sources=availableSources(data), [selected,setSelected]=useState('jcr');
  const current=sources.includes(selected)?selected:sources[0];
  if(!current) return <section className="panel"><Empty title="暂无分区与来源资料">本地尚未保存可展示的来源记录。</Empty></section>;
  return <div className="source-layout journal-source-layout"><nav aria-label="期刊数据来源">{sources.map(s=><button key={s} className={current===s?'active':''} onClick={()=>setSelected(s)}>{sourceNames[s]}</button>)}</nav><SourceRecord key={current} source={current} data={data}/></div>;
}
export function JournalSubmission({data,fail}:{data:Data;fail:Fail}) {
  const view=submissionView(data);
  return <section className="panel journal-submission"><div className="section-heading"><div><h2>投稿指南与模板</h2><p className="muted">查看已保存的投稿要求，或前往期刊提供的页面。</p></div><FileText size={24}/></div><div className="journal-summary-grid compact">{view.summary.map(f=><div key={f.label}><small>{f.label}</small><strong>{f.value}</strong></div>)}</div>
    {view.links.length>0 && <div className="journal-link-actions">{view.links.map(f=><button key={f.label} onClick={()=>window.research.external(f.href!).catch(fail)}>{f.label}<ArrowSquareOut/></button>)}</div>}
    <FactList facts={view.requirements} columns/><p className="journal-source-note">“暂无数据”表示已保存资料没有提供该项要求。模板文件名仅表示存在记录；有可用下载链接时会显示打开按钮。</p>
  </section>;
}
export function JournalFeedback({data,fail}:{data:Data;fail:Fail}) {
  const view=feedbackView(data);
  return <div className="journal-feedback"><section className="panel"><div className="section-heading"><div><h2>作者反馈与经验</h2><p className="muted">评分和处理时间来自作者报告；样本数量及评分可信度分别列出。</p></div><ChatCircleText size={25}/></div><div className="journal-summary-grid">{view.summary.map(f=><div key={f.label}><small>{f.label}</small><strong>{f.value}</strong>{f.note&&<span>{f.note}</span>}</div>)}</div></section>
    {view.comments.length?view.comments.map((c,i)=><article className="panel journal-comment" key={i}><header><div><strong>{c.author}</strong><small>{c.date===missing?'发布日期未提供':c.date} · {c.source===missing?'来源未提供':c.source}</small></div>{c.url&&<button onClick={()=>window.research.external(c.url!).catch(fail)}>查看原文<ArrowSquareOut/></button>}</header><p className="comment-body">{c.content===missing?'该条反馈未提供文字内容。':c.content}</p><div className="tag-list">{c.tags.map((tag,i)=><span className="chip" key={i}>{tag}</span>)}</div><FactList facts={c.facts} columns/></article>):<section className="panel"><Empty title="暂无已保存的反馈正文">汇总评分与反馈正文可能分别提供；此处仅展示本地已有内容。</Empty></section>}
  </div>;
}
export function JournalRelated({value,open,fail}:{value:any;open:(id:number)=>void;fail:Fail}) {
  const view=relatedView(value);
  return <div className="journal-related"><h3>同学科期刊</h3>{view.journals.length?<div className="related-grid">{view.journals.map((j,i)=><article key={i}><BookOpen size={22}/><div><h3>{j.name===missing?'期刊名称未提供':j.name}</h3><p>{j.category===missing?'学科待补充':j.category}</p><small>影响因子 {j.impact}</small></div>{Number.isSafeInteger(j.id)&&j.id>0&&<button onClick={()=>open(j.id)}>查看期刊</button>}</article>)}</div>:<p className="muted">暂无已保存的同学科期刊。</p>}
    <h3>近期论文</h3>{view.papers.length?view.papers.map((p,i)=><article className="related-paper" key={i}><FileText size={20}/><div><h3>{p.title===missing?'论文题名未提供':p.title}</h3><p>{p.year}{p.authors!==missing?' · '+p.authors:''}</p></div>{p.url&&<button onClick={()=>window.research.external(p.url!).catch(fail)}>查看论文<ArrowSquareOut/></button>}</article>):<p className="muted">暂无已保存的近期论文。</p>}
    {view.hubs.length>0&&<><h3>相关专题</h3><div className="journal-link-actions">{view.hubs.map((h,i)=><div key={i}>{h.label}：{h.name}{h.url&&<button onClick={()=>window.research.external(h.url!).catch(fail)}>打开专题<ArrowSquareOut/></button>}</div>)}</div></>}
  </div>;
}
