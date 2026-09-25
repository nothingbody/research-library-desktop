import {authorText, type Data} from './api';

type Props = {
  items: Data[];
  selected: string[];
  disabled: boolean;
  onSelect: (id: string) => void;
  onOpen: (item: Data) => void;
  onSave: (id: string) => void;
};

export function AiSearchList({items, selected, disabled, onSelect, onOpen, onSave}: Props) {
  return <div className="discovery-list" role="region" aria-label="文献检索结果">
    <table>
      <thead><tr><th><span className="sr-only">选择</span></th><th>论文</th><th>发表位置</th><th>被引</th><th>年份</th><th>操作</th></tr></thead>
      <tbody>{items.map(item => <tr key={item.id}>
        <td><input type="checkbox" aria-label={'选择 ' + item.title} checked={selected.includes(item.id)} disabled={disabled} onChange={() => onSelect(item.id)}/></td>
        <td className="discovery-paper"><button onClick={() => onOpen(item)}>{item.title}</button><small>{authorText(item) || '作者待补充'} · {(item.sources || []).join(' / ')}</small></td>
        <td>{item.venue || '来源未提供'}{item.journalMatch?.matched && <small>JCR {item.journalMatch.jcrQuartile || '—'} · IF {item.journalMatch.impactFactor ?? '—'} · {item.journalMatch.jcrYear || '年份未知'}</small>}</td>
        <td>{item.citationCount ?? '—'}</td>
        <td>{item.year || '—'}</td>
        <td className="discovery-actions"><button onClick={() => onOpen(item)}>论文简介</button><button disabled={disabled} onClick={() => onSave(item.id)}>保存</button></td>
      </tr>)}</tbody>
    </table>
  </div>;
}
