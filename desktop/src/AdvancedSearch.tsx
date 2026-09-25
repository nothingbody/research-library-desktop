import {useState} from 'react';
import {api, type Data, useErrorText} from './api';
import {Modal} from './ui';
import './advancedSearch.css';

type Leaf = {field:string; operator:string; value:string};
type Group = {op:'all'|'any'; conditions:Node[]};
type Node = Leaf|Group;
const fields = [['title','题名'],['author','作者'],['doi','DOI'],['journal','期刊 / 来源'],['year','年份'],['abstract','摘要'],['tag','标签'],['note','笔记'],['annotation','批注原文 / 评论'],['pdf','PDF 全文']];
const operators = [['contains','包含'],['equals','等于'],['isEmpty','为空 / 不存在'],['isNotEmpty','非空 / 存在']];
const newLeaf = ():Leaf => ({field:'title',operator:'contains',value:''});
const isGroup = (node:Node):node is Group => 'conditions' in node;

function RuleGroup({value,onChange,onRemove,depth=0}:{value:Group;onChange:(next:Group)=>void;onRemove?:()=>void;depth?:number}) {
  const update = (index:number,node:Node) => onChange({...value,conditions:value.conditions.map((old,i)=>i===index?node:old)});
  const remove = (index:number) => onChange({...value,conditions:value.conditions.filter((_,i)=>i!==index)});
  return <section className={'advanced-group depth-'+depth}>
    <div className="advanced-group-head"><span>满足</span><select aria-label="条件组匹配方式" value={value.op} onChange={e=>onChange({...value,op:e.target.value as 'all'|'any'})}><option value="all">全部条件</option><option value="any">任一条件</option></select>{onRemove&&<button type="button" onClick={onRemove}>移除组</button>}</div>
    {value.conditions.map((node,index)=>isGroup(node)?
      <RuleGroup key={index} value={node} onChange={next=>update(index,next)} onRemove={()=>remove(index)} depth={depth+1}/>:
      <div className="advanced-condition" key={index}><select aria-label="检索字段" value={node.field} onChange={e=>update(index,{...node,field:e.target.value})}>{fields.map(([key,label])=><option key={key} value={key}>{label}</option>)}</select><select aria-label="匹配关系" value={node.operator} onChange={e=>update(index,{...node,operator:e.target.value})}>{operators.map(([key,label])=><option key={key} value={key}>{label}</option>)}</select><input aria-label="条件内容" value={node.value} disabled={node.operator==='isEmpty'||node.operator==='isNotEmpty'} maxLength={200} placeholder="输入检索词" onChange={e=>update(index,{...node,value:e.target.value})}/><button type="button" aria-label="移除条件" onClick={()=>remove(index)}>移除</button></div>)}
    <div className="advanced-add"><button type="button" onClick={()=>onChange({...value,conditions:[...value.conditions,newLeaf()]})}>＋ 添加条件</button>{depth<3&&<button type="button" onClick={()=>onChange({...value,conditions:[...value.conditions,{op:'any',conditions:[newLeaf()]}]})}>＋ 添加条件组</button>}</div>
  </section>;
}

export function AdvancedSearch({collections,initial,initialIds,close,apply,notify}:{collections:Data[];initial?:Group;initialIds?:string[];close:()=>void;apply:(rule:Group,ids:string[])=>void;notify:(message:string)=>void}) {
  const [rule,setRule]=useState<Group>(initial||{op:'all',conditions:[newLeaf()]});
  const [ids,setIds]=useState<string[]>(initialIds||[]);
  const [name,setName]=useState(''),[busy,setBusy]=useState(false),[error,setError]=useState('');
  function validate() {
    const check=(node:Node):boolean=>isGroup(node)?node.conditions.length>0&&node.conditions.every(check):
      ['isEmpty','isNotEmpty'].includes(node.operator)||!!node.value.trim();
    if (!check(rule)) {setError('请填写每个条件的检索词，或选择“为空 / 不存在”。');return false;}
    setError('');return true;
  }
  async function save() {
    if (!validate()||!name.trim()) return;
    setBusy(true);try {await api('smartCollections.save',{name:name.trim(),rule:{advanced:rule,collectionIds:ids,view:'all'}});notify('已保存为智能集合，可在“研究整理”中查看动态结果');close();}
    catch(e){setError(useErrorText(e));}finally{setBusy(false);}
  }
  return <Modal title="高级库内检索" close={close} wide><div className="advanced-search"><p className="muted">在题录、笔记、批注和已索引 PDF 中组合条件。多个集合取并集；不选则检索全库。</p>
    <details className="advanced-scope"><summary>集合范围：{ids.length?`已选 ${ids.length} 个`:'全库'}</summary><div>{collections.map(collection=><label key={collection.id}><input type="checkbox" checked={ids.includes(collection.id)} onChange={e=>setIds(old=>e.target.checked?[...old,collection.id]:old.filter(id=>id!==collection.id))}/>{collection.name}</label>)}</div></details>
    <RuleGroup value={rule} onChange={setRule}/>{error&&<p className="advanced-error">{error}</p>}
    <div className="advanced-footer"><input aria-label="保存为智能集合名称" value={name} maxLength={100} placeholder="智能集合名称（可选）" onChange={e=>setName(e.target.value)}/><button type="button" disabled={!name.trim()||busy} onClick={save}>保存规则</button><span className="spacer"/><button type="button" onClick={close}>取消</button><button type="button" className="primary" onClick={()=>{if(validate())apply(rule,ids);}}>应用筛选</button></div>
  </div></Modal>;
}
