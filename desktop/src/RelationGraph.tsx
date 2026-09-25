import {useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent as ReactWheelEvent} from 'react';
import {ArrowsOut, DownloadSimple, Minus, Plus} from '@phosphor-icons/react';
import {files, type Data} from './api';
import './relationGraph.css';

type Point = {x: number; y: number};
type GraphNode = Point & {id: string; title: string; year?: string; citationCount?: number | null; profileState?: string; external?: boolean; degree: number};
type Drag = {kind: 'pan' | 'node'; id?: string; x: number; y: number; startX: number; startY: number; moved: boolean};
const WIDTH = 1000;
const HEIGHT = 650;
const colors: Record<string, string> = {
  topic_overlap: '#6ba8ff', method_compare: '#ba9bff', supports: '#6bd9af',
  contradicts: '#ff8f94', extends: '#55d7da', shared_gap: '#f1bb72',
  citation: '#58dcb6', recommended: '#e6b577',
  manual: '#f4ca79',
  shared_references: '#d192ed',
};
const colorFor = (type: string) => colors[type] || '#9db9e8';
const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

function layoutNodes(items: Data[], relations: Data[]): GraphNode[] {
  const count = items.length;
  if (!count) return [];
  const degrees = new Map<string, number>();
  const positions = items.map((item, index) => {
    const angle = -Math.PI / 2 + index * Math.PI * 2 / count;
    const radius = count <= 4 ? 185 : count <= 10 ? 230 : 260;
    return {id: String(item.itemId), title: String(item.title || '未命名文献'), year: item.year, citationCount: item.citationCount, profileState: item.profileState, external: !!item.external,
      x: WIDTH / 2 + Math.cos(angle) * radius, y: HEIGHT / 2 + Math.sin(angle) * radius, vx: 0, vy: 0};
  });
  const byId = new Map(positions.map((node, index) => [node.id, index]));
  const links = relations.flatMap(row => {
    const left = byId.get(row.leftItemId), right = byId.get(row.rightItemId);
    if (left === undefined || right === undefined || left === right) return [];
    degrees.set(row.leftItemId, (degrees.get(row.leftItemId) || 0) + 1);
    degrees.set(row.rightItemId, (degrees.get(row.rightItemId) || 0) + 1);
    return [{left, right}];
  });
  // Settle a small force layout once per analysis set; no animation or network request is needed.
  for (let step = 0; step < 190; step++) {
    const force = positions.map(() => ({x: 0, y: 0}));
    for (let i = 0; i < count; i++) {
      force[i].x += (WIDTH / 2 - positions[i].x) * .009;
      force[i].y += (HEIGHT / 2 - positions[i].y) * .009;
      for (let j = i + 1; j < count; j++) {
        const dx = positions[i].x - positions[j].x;
        const dy = positions[i].y - positions[j].y;
        const distanceSq = Math.max(400, dx * dx + dy * dy);
        const strength = 23000 / distanceSq;
        const distance = Math.sqrt(distanceSq);
        force[i].x += dx / distance * strength;
        force[i].y += dy / distance * strength;
        force[j].x -= dx / distance * strength;
        force[j].y -= dy / distance * strength;
      }
    }
    for (const {left, right} of links) {
      const dx = positions[right].x - positions[left].x;
      const dy = positions[right].y - positions[left].y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const pull = (distance - 195) * .0025;
      force[left].x += dx / distance * pull;
      force[left].y += dy / distance * pull;
      force[right].x -= dx / distance * pull;
      force[right].y -= dy / distance * pull;
    }
    positions.forEach((node, index) => {
      node.vx = (node.vx + force[index].x) * .76;
      node.vy = (node.vy + force[index].y) * .76;
      node.x = clamp(node.x + node.vx, 86, WIDTH - 86);
      node.y = clamp(node.y + node.vy, 86, HEIGHT - 95);
    });
  }
  return positions.map(({vx: _vx, vy: _vy, ...node}) => ({...node, degree: degrees.get(node.id) || 0}));
}

function shortTitle(title: string) {
  return title.length > 25 ? title.slice(0, 24).trimEnd() + '…' : title;
}

function chronology(nodes: GraphNode[]) {
  const knownYears = nodes.map(node => Number(node.year)).filter(year => Number.isInteger(year) && year >= 1500 && year <= 2100);
  const minYear = Math.min(...knownYears), maxYear = Math.max(...knownYears);
  const citations = nodes.map(node => Number(node.citationCount)).filter(count => Number.isFinite(count) && count >= 0);
  const maxCitations = citations.length ? Math.max(1, ...citations) : 1;
  const plotted = nodes.map((node, index) => {
    const year = Number(node.year), count = Number(node.citationCount);
    const validYear = Number.isInteger(year) && year >= 1500 && year <= 2100;
    const validCitations = node.citationCount !== null && node.citationCount !== undefined && Number.isFinite(count) && count >= 0;
    const x = validYear ? 185 + (maxYear === minYear ? .5 : (year - minYear) / (maxYear - minYear)) * 700 : 90;
    const y = validCitations ? 505 - Math.log1p(count) / Math.log1p(maxCitations) * 340 : 555;
    return {...node, x: x + ((index % 5) - 2) * 4, y: y + ((index % 3) - 1) * 4};
  });
  return {nodes: plotted, minYear, maxYear, maxCitations, hasYears: knownYears.length > 0, hasCitations: citations.length > 0};
}

export function RelationGraph({items, relations, discoveries = [], manualRelations = [], selectedRelationId, selectedDiscoveryId, selectedManualId, selectedSharedId, onSelectRelation, onSelectDiscovery, onSelectManual, onSelectShared, onExported, onError}: {
  items: Data[]; relations: Data[]; discoveries?: Data[]; manualRelations?: Data[];
  selectedRelationId?: string; selectedDiscoveryId?: string | null; selectedManualId?: string | null; selectedSharedId?: string | null;
  onSelectRelation: (relation: Data) => void; onSelectDiscovery?: (discovery: Data) => void; onSelectManual?: (relation: Data) => void; onSelectShared?: (relation: Data) => void;
  onExported: () => void; onError: (error: unknown) => void;
}) {
  const [discoveryLimit, setDiscoveryLimit] = useState(20);
  const {graphItems, graphRelations} = useMemo(() => {
    const known = new Set(items.map(item => item.itemId));
    const external = new Map<string, Data>();
    const cited: Data[] = discoveries.filter(row => row.status !== 'ignored').slice(0, discoveryLimit).map(row => {
      const target = row.importedItemId && known.has(row.importedItemId) ? row.importedItemId : `ext:${row.workId}`;
      if (String(target).startsWith('ext:')) external.set(target, {itemId: target, title: row.title, year: row.year, citationCount: row.citationCount, external: true});
      return {...row, id: row.id, leftItemId: row.direction === 'citing' ? target : row.anchorItemId,
        rightItemId: row.direction === 'citing' ? row.anchorItemId : target,
        kind: row.direction === 'related' ? 'algorithmic-recommendation' : 'verified-citation-record',
        type: row.direction === 'related' ? 'recommended' : 'citation',
        typeLabel: row.direction === 'related' ? '主题推荐' : row.direction === 'citing' ? '后续引用' : '参考文献', discovery: row};
    });
    const manual: Data[] = manualRelations.map(row => ({...row, type: 'manual', typeLabel: row.label, manual: row}));
    const sharedPairs = new Map<string, {left: string; right: string; titles: string[]}>();
    const byWork = new Map<string, Data[]>();
    for (const row of discoveries.filter(row => row.direction === 'references' && row.status !== 'ignored')) {
      byWork.set(row.workId, [...(byWork.get(row.workId) || []), row]);
    }
    for (const references of byWork.values()) {
      const anchors = [...new Map(references.map(row => [row.anchorItemId, row])).values()];
      for (let i = 0; i < anchors.length; i++) for (let j = i + 1; j < anchors.length; j++) {
        const [left, right] = [anchors[i].anchorItemId, anchors[j].anchorItemId].sort();
        const key = `${left}|${right}`;
        const pair: {left: string; right: string; titles: string[]} = sharedPairs.get(key) || {left, right, titles: []};
        pair.titles.push(anchors[i].title);
        sharedPairs.set(key, pair);
      }
    }
    const shared: Data[] = [...sharedPairs.values()].filter(pair => pair.titles.length >= 2).map(pair => ({
      id: `shared:${pair.left}:${pair.right}`, leftItemId: pair.left, rightItemId: pair.right,
      kind: 'shared-references', type: 'shared_references', typeLabel: `共同参考文献 ${pair.titles.length} 篇`,
      sharedCount: pair.titles.length, sharedTitles: pair.titles,
    }));
    return {graphItems: [...items, ...external.values()], graphRelations: [...relations, ...cited, ...manual, ...shared] as Data[]};
  }, [items, relations, discoveries, manualRelations, discoveryLimit]);
  const initialNodes = useMemo(() => layoutNodes(graphItems, graphRelations), [graphItems, graphRelations]);
  const [graphLayout, setGraphLayout] = useState<'network' | 'chronology'>('network');
  const chronologyData = useMemo(() => chronology(initialNodes), [initialNodes]);
  const yearTicks = chronologyData.hasYears ? (chronologyData.maxYear - chronologyData.minYear <= 6
    ? Array.from({length: chronologyData.maxYear - chronologyData.minYear + 1}, (_, index) => chronologyData.minYear + index)
    : [...new Set([0, .25, .5, .75, 1].map(fraction => Math.round(chronologyData.minYear + fraction * (chronologyData.maxYear - chronologyData.minYear))))]) : [];
  const [offsets, setOffsets] = useState<Record<string, Point>>({});
  const [focusId, setFocusId] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState<Point>({x: 0, y: 0});
  const svgRef = useRef<SVGSVGElement | null>(null);
  const drag = useRef<Drag | null>(null);
  const key = graphItems.map(item => item.itemId).join('|');
  useEffect(() => {setOffsets({}); setFocusId(null); setZoom(1); setPan({x: 0, y: 0});}, [key]);
  const nodes = useMemo(() => (graphLayout === 'chronology' ? chronologyData.nodes : initialNodes.map(node => ({...node, x: node.x + (offsets[node.id]?.x || 0), y: node.y + (offsets[node.id]?.y || 0)}))), [graphLayout, chronologyData, initialNodes, offsets]);
  const byId = useMemo(() => new Map(nodes.map(node => [node.id, node])), [nodes]);
  const selected = graphRelations.find(row => row.id === (selectedRelationId || selectedDiscoveryId || selectedManualId || selectedSharedId));
  const activeId = focusId || selected?.leftItemId || null;
  const nearby = focusId ? graphRelations.filter(row => row.leftItemId === focusId || row.rightItemId === focusId) : [];
  const pairOrder = new Map<string, number>();

  function pointFromEvent(event: ReactPointerEvent<SVGSVGElement>) {
    const bounds = svgRef.current!.getBoundingClientRect();
    return {x: (event.clientX - bounds.left) * WIDTH / bounds.width, y: (event.clientY - bounds.top) * HEIGHT / bounds.height};
  }
  function onPointerDown(event: ReactPointerEvent<SVGSVGElement>) {
    if (event.button !== 0 || (event.target as Element).closest('[data-graph-edge]')) return;
    const id = (event.target as Element).closest('[data-graph-node]')?.getAttribute('data-graph-node') || undefined;
    const point = pointFromEvent(event);
    drag.current = {kind: id && graphLayout === 'network' ? 'node' : 'pan', id, x: point.x, y: point.y, startX: point.x, startY: point.y, moved: false};
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  function onPointerMove(event: ReactPointerEvent<SVGSVGElement>) {
    if (!drag.current) return;
    const point = pointFromEvent(event);
    const dx = point.x - drag.current.x, dy = point.y - drag.current.y;
    if (Math.hypot(point.x - drag.current.startX, point.y - drag.current.startY) > 4) drag.current.moved = true;
    if (drag.current.kind === 'node' && drag.current.id) {
      const id = drag.current.id;
      setOffsets(current => ({...current, [id]: {x: (current[id]?.x || 0) + dx / zoom, y: (current[id]?.y || 0) + dy / zoom}}));
    } else setPan(current => ({x: current.x + dx, y: current.y + dy}));
    drag.current.x = point.x; drag.current.y = point.y;
  }
  function onPointerUp(event: ReactPointerEvent<SVGSVGElement>) {
    if (drag.current?.id && !drag.current.moved) setFocusId(drag.current.id);
    drag.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }
  function onWheel(event: ReactWheelEvent<SVGSVGElement>) {
    event.preventDefault();
    const point = pointFromEvent(event as unknown as ReactPointerEvent<SVGSVGElement>);
    const next = clamp(zoom * (event.deltaY < 0 ? 1.12 : .89), .55, 2.6);
    const ratio = next / zoom;
    setPan(current => ({x: point.x - (point.x - current.x) * ratio, y: point.y - (point.y - current.y) * ratio}));
    setZoom(next);
  }
  function reset() {setZoom(1); setPan({x: 0, y: 0}); setOffsets({}); setFocusId(null);}
  function changeZoom(factor: number) {setZoom(current => clamp(current * factor, .55, 2.6));}
  async function exportImage() {
    const source = svgRef.current;
    if (!source) return;
    const copy = source.cloneNode(true) as SVGSVGElement;
    copy.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    copy.setAttribute('width', String(WIDTH)); copy.setAttribute('height', String(HEIGHT));
    const background = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    background.setAttribute('width', String(WIDTH)); background.setAttribute('height', String(HEIGHT)); background.setAttribute('fill', '#10213b');
    copy.insertBefore(background, copy.firstChild);
    const style = document.createElementNS('http://www.w3.org/2000/svg', 'style');
    style.textContent = '.relation-graph-node-index{fill:#ecf5ff;font:700 16px Arial,sans-serif}.relation-graph-node-label{fill:#e5efff;font:600 13px Arial,sans-serif;paint-order:stroke;stroke:#102341;stroke-width:3px;stroke-linejoin:round}.relation-graph-node-meta{fill:#9ab3d4;font:11px Arial,sans-serif;paint-order:stroke;stroke:#102341;stroke-width:3px}.relation-graph-edge.faded,.relation-graph-node.faded{opacity:.16}.relation-graph-node.active{opacity:1}.relation-axis{fill:#b5cae5;font:12px Arial,sans-serif}.relation-axis-line{stroke:#7e9bc3;stroke-width:1;opacity:.55}';
    copy.insertBefore(style, copy.firstChild);
    const saved = await files('graphImageExport', {text: new XMLSerializer().serializeToString(copy)});
    if (saved) onExported();
  }

  return <div className="relation-graph">
    <div className="relation-graph-toolbar"><div><strong>{graphLayout === 'chronology' ? '年份 × 被引分布' : '文献关系网络'}</strong><span>{items.length} 篇本地论文 · {graphItems.length - items.length} 篇待收集 · {relations.length} 条证据候选 · {graphRelations.length - relations.length} 条来源/人工连接</span></div><div className="relation-graph-zoom"><select aria-label="图谱布局" value={graphLayout} onChange={event => {setGraphLayout(event.target.value as 'network' | 'chronology'); reset();}}><option value="network">关系网络</option><option value="chronology">年份 × 被引</option></select><select aria-label="图上发现数量" value={discoveryLimit} onChange={event => setDiscoveryLimit(Number(event.target.value))}><option value={20}>显示 20 篇</option><option value={40}>显示 40 篇</option><option value={80}>显示 80 篇</option></select><button aria-label="导出图谱 SVG" title="导出图谱 SVG" onClick={() => {exportImage().catch(onError);}}><DownloadSimple/></button><button aria-label="缩小关系网络" onClick={() => changeZoom(.8)}><Minus/></button><button aria-label="重置关系网络" onClick={reset}><ArrowsOut/></button><button aria-label="放大关系网络" onClick={() => changeZoom(1.25)}><Plus/></button></div></div>
    <svg ref={svgRef} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="group" aria-label={`${graphItems.length} 篇论文与 ${graphRelations.length} 条连接组成的可交互网络图`} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerUp} onWheel={onWheel}>
      <defs><pattern id="relation-grid" width="28" height="28" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#7290c2" opacity=".19"/></pattern><filter id="relation-glow"><feGaussianBlur stdDeviation="5"/></filter><marker id="citation-arrow" markerWidth="9" markerHeight="9" refX="8" refY="4.5" orient="auto" markerUnits="userSpaceOnUse"><path d="M 0 0 L 9 4.5 L 0 9 z" fill="#58dcb6"/></marker></defs>
      <rect width={WIDTH} height={HEIGHT} fill="url(#relation-grid)"/>
      <g transform={`translate(${pan.x} ${pan.y}) scale(${zoom})`}>
        {graphLayout === 'chronology' && <g className="relation-axes" aria-label="年份和被引数坐标轴">
          <path className="relation-axis-line" d="M 150 155 L 150 515 L 905 515" fill="none"/>
          {[0, .25, .5, .75, 1].map(fraction => <g key={`y-${fraction}`}><path className="relation-axis-line" d={`M 150 ${505 - fraction * 340} L 905 ${505 - fraction * 340}`} strokeDasharray="3 8"/><text className="relation-axis" x="142" y={509 - fraction * 340} textAnchor="end">{Math.round(Math.expm1(Math.log1p(chronologyData.maxCitations) * fraction))}</text></g>)}
          {yearTicks.map(year => <text key={`x-${year}`} className="relation-axis" x={185 + (chronologyData.maxYear === chronologyData.minYear ? .5 : (year - chronologyData.minYear) / (chronologyData.maxYear - chronologyData.minYear)) * 700} y="535" textAnchor="middle">{year}</text>)}
          <text className="relation-axis" x="35" y="335" transform="rotate(-90 35 335)" textAnchor="middle">被引次数（对数刻度）</text>
        </g>}
        {graphRelations.map(row => {
          const left = byId.get(row.leftItemId), right = byId.get(row.rightItemId);
          if (!left || !right || left.id === right.id) return null;
          const pair = [left.id, right.id].sort().join('|');
          const order = pairOrder.get(pair) || 0; pairOrder.set(pair, order + 1);
          const dx = right.x - left.x, dy = right.y - left.y, distance = Math.max(1, Math.hypot(dx, dy));
          const curve = order ? (Math.ceil(order / 2) * (order % 2 ? 1 : -1) * 22) : 0;
          const midpoint = {x: (left.x + right.x) / 2 - dy / distance * curve, y: (left.y + right.y) / 2 + dx / distance * curve};
          const startLength = Math.max(1, Math.hypot(midpoint.x - left.x, midpoint.y - left.y));
          const endLength = Math.max(1, Math.hypot(right.x - midpoint.x, right.y - midpoint.y));
          const startRadius = Math.min(30, 20 + Math.sqrt(left.degree) * 3) + 3;
          const endRadius = Math.min(30, 20 + Math.sqrt(right.degree) * 3) + 4;
          const start = {x: left.x + (midpoint.x - left.x) / startLength * startRadius,
            y: left.y + (midpoint.y - left.y) / startLength * startRadius};
          const end = {x: right.x - (right.x - midpoint.x) / endLength * endRadius,
            y: right.y - (right.y - midpoint.y) / endLength * endRadius};
          const path = `M ${start.x} ${start.y} Q ${midpoint.x} ${midpoint.y} ${end.x} ${end.y}`;
          const active = selectedRelationId === row.id || selectedDiscoveryId === row.id || selectedManualId === row.id || selectedSharedId === row.id;
          const faded = activeId && left.id !== activeId && right.id !== activeId && !active;
          const color = colorFor(row.type);
          const selectEdge = () => {setFocusId(null); if (row.discovery) onSelectDiscovery?.(row.discovery); else if (row.manual) onSelectManual?.(row.manual); else if (row.kind === 'shared-references') onSelectShared?.(row); else onSelectRelation(row);};
          return <g key={row.id} className={'relation-graph-edge' + (faded ? ' faded' : '') + (active ? ' active' : '')} data-graph-edge={row.id} onClick={selectEdge} role="button" tabIndex={0} aria-label={`${left.title} 与 ${right.title}：${row.typeLabel}，${row.discovery ? '查看来源记录' : row.manual ? '查看人工说明' : '查看双侧证据'}`} onKeyDown={event => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault(); selectEdge();}}}>
            <path d={path} fill="none" stroke={color} strokeWidth={active ? 9 : 6} opacity={active ? .4 : .12} filter="url(#relation-glow)"/>
            <path d={path} fill="none" stroke={color} strokeWidth={active ? 3.5 : 2} strokeDasharray={row.kind === 'algorithmic-recommendation' ? '3 7' : row.kind === 'shared-references' ? '4 5' : row.kind === 'evidence-inference' || row.status === 'rejected' ? '8 6' : undefined} markerEnd={row.kind === 'verified-citation-record' ? 'url(#citation-arrow)' : undefined} opacity={row.status === 'rejected' ? .42 : active ? 1 : .78}/>
            <path d={path} fill="none" stroke="transparent" strokeWidth="18"/>
            <title>{row.discovery ? `${row.typeLabel} · OpenAlex 来源记录 · ${row.discovery.sourceUrl}` : row.manual ? `${row.typeLabel} · 人工关联 · ${row.note || ''}` : row.kind === 'shared-references' ? `${row.typeLabel} · 当前已发现的 OpenAlex 参考文献交集，可能不完整` : `${row.typeLabel} · 本地证据推断 · ${Math.round((row.confidence || 0) * 100)}% · ${row.rationale || ''}`}</title>
          </g>;
        })}
        {nodes.map((node, index) => {
          const radius = Math.min(30, 20 + Math.sqrt(node.degree) * 3);
          const active = activeId === node.id;
          const faded = activeId && !active && !graphRelations.some(row => (row.leftItemId === activeId && row.rightItemId === node.id) || (row.rightItemId === activeId && row.leftItemId === node.id));
          return <g key={node.id} data-graph-node={node.id} className={'relation-graph-node' + (active ? ' active' : '') + (faded ? ' faded' : '')} transform={`translate(${node.x} ${node.y})`} role="button" tabIndex={0} aria-label={`${node.title}，${node.degree} 条关系`} onKeyDown={event => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault(); setFocusId(node.id);}}}>
            <circle r={radius + 8} fill={active ? '#83b9ff' : '#77a6ed'} opacity={active ? .19 : .08}/>
            <circle r={radius} fill={active ? '#3b76cd' : node.external ? '#694e37' : '#223e69'} stroke={active ? '#a7d5ff' : node.external ? '#e6b577' : node.degree ? '#84addd' : '#69809f'} strokeWidth={active ? 3 : 1.5}/>
            <text textAnchor="middle" dy="4" className="relation-graph-node-index">{String(index + 1).padStart(2, '0')}</text>
            <text textAnchor="middle" y={radius + 22} className="relation-graph-node-label">{shortTitle(node.title)}</text>
            <text textAnchor="middle" y={radius + 37} className="relation-graph-node-meta">{node.year || '年份未知'} · {node.external ? '待收集' : `${node.degree} 条关系`}</text>
            <title>{node.title}</title>
          </g>;
        })}
      </g>
    </svg>
    <div className="relation-graph-legend"><span><i style={{background: colors.citation}}/>实线箭头：来源记录中的引用</span><span><i style={{background: colors.shared_references}}/>短虚线：共同参考文献</span><span><i style={{background: colors.recommended}}/>点线：算法主题推荐</span><span><i style={{background: colors.topic_overlap}}/>长虚线：本地证据推断</span><span><i style={{background: colors.manual}}/>实线：人工关联</span></div>
    {focusId && <div className="relation-graph-focus"><button aria-label="关闭文献信息" onClick={() => setFocusId(null)}>×</button><small>已选论文 · {nearby.length} 条连接</small><strong>{byId.get(focusId)?.title}</strong>{nearby.length ? <div>{nearby.map(row => <button key={row.id} onClick={() => {setFocusId(null); if (row.discovery) onSelectDiscovery?.(row.discovery); else if (row.manual) onSelectManual?.(row.manual); else if (row.kind === 'shared-references') onSelectShared?.(row); else onSelectRelation(row);}}><i style={{background: colorFor(row.type)}}/>{row.typeLabel} · {byId.get(row.leftItemId === focusId ? row.rightItemId : row.leftItemId)?.title}</button>)}</div> : <p>当前筛选条件下，这篇论文没有关系线。可调整上方筛选条件。</p>}</div>}
    <div className="relation-graph-hint">{graphLayout === 'chronology' ? '横轴：发表年份 · 纵轴：被引数（对数） · 左侧/底部表示数据缺失 · 点击连线核对来源' : '拖动画布平移 · 滚轮缩放 · 拖动节点调整位置 · 点击连线查看双侧证据'}</div>
  </div>;
}
