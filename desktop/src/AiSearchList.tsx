import {authorText, type Data} from './api';

type Props = {
  items: Data[];
  selected: string[];
  disabled: boolean;
  onSelect: (id: string) => void;
  onSelectAll: (checked: boolean) => void;
  onOpen: (item: Data) => void;
  onSave: (id: string) => void;
};

const sourceNames: Record<string, string> = {openalex: 'OpenAlex', crossref: 'Crossref', pubmed: 'PubMed', arxiv: 'arXiv'};

export function AiSearchList({items, selected, disabled, onSelect, onSelectAll, onOpen, onSave}: Props) {
  const allSelected = items.length > 0 && items.every(item => selected.includes(item.id));
  return <div className="discovery-list" role="region" aria-label="文献检索结果">
    <table>
      <thead><tr><th><input type="checkbox" aria-label="选择本页全部文献" checked={allSelected} disabled={disabled || !items.length} onChange={event => onSelectAll(event.target.checked)}/></th><th>论文</th><th>发表位置</th><th>被引</th><th>年份</th><th>操作</th></tr></thead>
      <tbody>{items.map(item => <tr key={item.id}>
        <td><input type="checkbox" aria-label={'选择 ' + item.title} checked={selected.includes(item.id)} disabled={disabled} onChange={() => onSelect(item.id)}/></td>
        <td className="discovery-paper">
          <button className="discovery-paper-title" title={item.title} onClick={() => onOpen(item)}>{item.title}</button>
          <small className="discovery-authors" title={authorText(item) || ''}>{authorText(item) || '作者待补充'}</small>
          <div className="discovery-record-flags"><span>{(item.sources || []).map((source: string) => sourceNames[source] || source).join(' / ') || '来源待核对'}</span><span>{item.abstract ? '有摘要' : '仅题名'}</span>{item.oaUrl && <span title="候选链接，尚未验证为 PDF">开放链接待检查</span>}</div>
        </td>
        <td className="discovery-venue">{item.venue || '来源未提供'}{item.journalMatch?.matched && <small>JCR {item.journalMatch.jcrQuartile || '—'} · IF {item.journalMatch.impactFactor ?? '—'} · {item.journalMatch.jcrYear || '年份未知'}</small>}</td>
        <td className="discovery-number">{item.citationCount ?? '—'}</td>
        <td className="discovery-number">{item.year || '—'}</td>
        <td className="discovery-actions"><button onClick={() => onOpen(item)}>查看简介</button><button disabled={disabled} onClick={() => onSave(item.id)}>保存文献</button></td>
      </tr>)}</tbody>
    </table>
  </div>;
}
