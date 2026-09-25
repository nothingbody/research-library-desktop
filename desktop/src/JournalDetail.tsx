import {useEffect, useState} from 'react';
import {ArrowLeft, ArrowSquareOut, ArrowsClockwise, BookOpen, Compass} from '@phosphor-icons/react';
import {api, type Data, useErrorText} from './api';
import {Busy, Empty, ErrorBox, JsonView} from './ui';
import {JournalOverview, JournalSources, JournalSubmission, JournalFeedback, JournalRelated} from './JournalSections';
import {dateLabel, journalView, list, numberText, sourceNames} from './journalPresentation';

function Trend({name, series}: {name:string;series:Data[]}) {
  if (!series.length) return <div className="trend"><h3>{name}</h3><p className="muted">暂无历史数据。</p></div>;
  const max=Math.max(...series.map(d=>d.value),1), min=Math.min(0,...series.map(d=>d.value));
  const x=(i:number)=>38+(i/Math.max(1,series.length-1))*360, y=(v:number)=>126-((v-min)/(max-min))*99;
  return <div className="trend"><h3>{name}<small>{sourceNames[series.at(-1)?.source] || '来源未提供'}</small></h3><svg viewBox="0 0 430 165" role="img" aria-label={name+'历年变化'}>{[0,.5,1].map(n=><g key={n}><line x1="38" y1={126-n*99} x2="399" y2={126-n*99} stroke="#e8edf4"/><text x="0" y={130-n*99} fontSize="10" fill="#8490a2">{Math.round(min+(max-min)*n)}</text></g>)}<polyline points={series.map((d,i)=>`${x(i)},${y(d.value)}`).join(' ')} fill="none" stroke="#4168db" strokeWidth="2.2"/>{series.map((d,i)=><g key={i}><circle cx={x(i)} cy={y(d.value)} r="3" fill="#4168db"><title>{d.year}: {d.value}</title></circle>{(i===0||i===series.length-1||series.length<6)&&<text x={x(i)} y="151" textAnchor="middle" fontSize="10" fill="#8490a2">{d.year}</text>}</g>)}</svg><details><summary>查看年份与数值</summary><table className="simple-table"><thead><tr><th>年份</th><th>数值</th><th>来源</th></tr></thead><tbody>{series.map((d,i)=><tr key={i}><td>{d.year}</td><td>{numberText(d.value)}</td><td>{sourceNames[d.source] || '来源未提供'}</td></tr>)}</tbody></table></details></div>;
}
export function Detail({id,back,onSelect,fail,notify}:{id:number;back:()=>void;onSelect:(id:number)=>void;fail:(e:any)=>void;notify:(message:string)=>void}) {
  const [data,setData]=useState<Data|null>(null), [tab,setTab]=useState('overview'), [error,setError]=useState(''), [related,setRelated]=useState<Data|null>(null), [version,setVersion]=useState(0);
  useEffect(()=>setTab('overview'),[id]);
  useEffect(()=>{let cancelled=false;setData(null);setError('');setRelated(null);
    api('journals.get',{id}).then(r=>{if(!cancelled)setData(r);}).catch(e=>{if(!cancelled)setError(useErrorText(e));});
    api('journals.related',{id}).then(r=>{if(!cancelled)setRelated(r);}).catch(e=>{if(!cancelled)fail(e);});
    return ()=>{cancelled=true;};
  },[id,version,fail]);
  const ensure=(kind:string)=>api('journals.ensure',{id,kind}).then(()=>notify('已加入采集队列；完成后点击“重读本地数据”')).catch(fail);
  if(error)return <div className="module-page"><button onClick={back}><ArrowLeft/>返回期刊</button><ErrorBox message={error} retry={()=>setVersion(v=>v+1)}/></div>;
  if(!data)return <Busy/>;
  const view=journalView(data),u=data.unified || {};
  const status=({complete:'已保存',ok:'已保存',pending:'详情待获取',running:'正在更新',failed:'详情更新失败',blocked:'详情暂不可获取'} as Data)[data.detailStatus] || '详情待补充';
  return <div className="module-page journal-detail">
    <div className="detail-breadcrumb"><button className="link-button" onClick={back}><ArrowLeft size={17}/>期刊选投</button><span>/ 期刊档案</span><div className="spacer"/><button onClick={()=>setVersion(v=>v+1)}><ArrowsClockwise/>重读本地数据</button><button onClick={()=>ensure('detail')}>联网更新</button></div>
    <div className="journal-title"><span className="journal-emblem"><BookOpen size={34}/></span><div><span className="eyebrow">JOURNAL PROFILE</span><h1>{data.name}</h1><p>{view.publisher} · {view.country}</p><div className="tag-list">{u.print_issn&&<span className="chip">纸本 ISSN {u.print_issn}</span>}{u.electronic_issn&&<span className="chip">电子 ISSN {u.electronic_issn}</span>}</div></div><div className="spacer"/>{data.homepage&&<button onClick={()=>window.research.external(data.homepage).catch(fail)}>期刊官网<ArrowSquareOut/></button>}</div>
    <div className="journal-metrics">{view.headlines.map(f=><div key={f.label}><small>{f.label}</small><strong>{f.value}</strong><span>{f.note}</span></div>)}</div>
    <div className="source-notice">本地详情：{status} · 最近获取：{dateLabel(data.fetchedAt)} · 评价指标按各自来源和年份展示</div>
    <div className="detail-tabs" role="tablist" aria-label="期刊详情">{[['overview','概览与趋势'],['sources','分区与数据源'],['submission','投稿指南'],['comments','作者反馈'],['related','相关期刊与论文'],['raw','原始数据']].map(([key,label])=><button role="tab" aria-selected={tab===key} key={key} className={tab===key?'active':''} onClick={()=>setTab(key)}>{label}</button>)}</div>
    <div role="tabpanel" className="journal-tab-content" data-tab={tab}>
      {tab==='overview'&&<><JournalOverview data={data}/><h2>历史指标趋势</h2><div className="trends-grid">{[['impact','影响因子趋势'],['sjr','SJR 趋势'],['snip','SNIP 趋势'],['works','年度发文量'],['cited','年度引用量']].map(([key,name])=><Trend key={key} name={name} series={list(data.metrics?.[key])}/>)}</div></>}
      {tab==='sources'&&<JournalSources data={data}/>}
      {tab==='submission'&&<JournalSubmission data={data} fail={fail}/>}
      {tab==='comments'&&<JournalFeedback data={data} fail={fail}/>}
      {tab==='related'&&<section className="panel"><div className="section-heading"><h2>相关期刊与论文</h2><button onClick={()=>ensure('related')}>联网获取</button></div>{related?.status==='cached'?<JournalRelated value={related.data} open={onSelect} fail={fail}/>:<Empty icon={<Compass size={32}/>} title="相关内容尚未缓存">联网获取后可在本机保留查询结果。</Empty>}</section>}
      {tab==='raw'&&<section className="panel"><div className="section-heading"><div><h2>原始数据</h2><p className="muted">用于核对数据来源；包含完整字段、内部编号及匹配记录。</p></div><button onClick={()=>window.research.clipboard(JSON.stringify(data.raw,null,2)).then(()=>notify('原始数据已复制')).catch(fail)}>复制 JSON</button></div><JsonView value={data.raw}/></section>}
    </div>
  </div>;
}
