import {useEffect, useRef, useState, type MouseEvent as ReactMouseEvent} from 'react';
import {TextLayer, type PDFDocumentProxy} from 'pdfjs-dist';
import {type Data} from './api';
import {attachSelectionGuard, mergeSelectionRects, normalizePdfLineSelection, trackPdfSelectionDrag} from './pdfTextSelection';
import './continuousPage.css';

const pageViewports = new WeakMap<HTMLElement, any>();

export function ContinuousPage({pdf,page,scale,rotation,annotations,activeId,tool,size,onSelect,onStartSelection}: {
  pdf:PDFDocumentProxy;page:number;scale:number;rotation:number;annotations:Data[];activeId?:string;
  tool:string;size?:{width:number;height:number};onSelect:(value:Data)=>void;onStartSelection?:()=>void;
}) {
  const box=useRef<HTMLDivElement>(null),canvas=useRef<HTMLCanvasElement>(null),text=useRef<HTMLDivElement>(null);
  const areaStart=useRef<number[]|null>(null);
  const selectionStart=useRef<{x:number;y:number}|null>(null);
  const captureRef=useRef<(event:{clientX:number;clientY:number})=>void>(()=>{});
  const [visible,setVisible]=useState(false),[viewport,setViewport]=useState<any>(null);
  const [area,setArea]=useState<number[]|null>(null);
  useEffect(()=>{const node=box.current;if(!node)return;const root=node.closest('.pdf-scroll');const observer=new IntersectionObserver(entries=>setVisible(entries.some(entry=>entry.isIntersecting)),{root,rootMargin:'700px 0px'});observer.observe(node);return()=>observer.disconnect();},[]);
  useEffect(()=>{if(!visible||!canvas.current||!text.current)return;let cancelled=false,task:any,layer:any,detachGuard:(()=>void)|undefined;const pageBox=box.current;pdf.getPage(page).then(async p=>{if(cancelled||!canvas.current||!text.current)return;const vp=p.getViewport({scale,rotation:(p.rotate+rotation)%360});setViewport(vp);if(pageBox)pageViewports.set(pageBox,vp);const cv=canvas.current,dpr=Math.min(window.devicePixelRatio||1,2,Math.sqrt(16000000/(vp.width*vp.height)));cv.width=Math.floor(vp.width*dpr);cv.height=Math.floor(vp.height*dpr);cv.style.width=vp.width+'px';cv.style.height=vp.height+'px';const surface=text.current;surface.replaceChildren();surface.style.setProperty('--scale-factor',String(scale));surface.style.setProperty('--total-scale-factor',String(scale*p.userUnit));surface.style.setProperty('--user-unit',String(p.userUnit));surface.style.setProperty('--scale-round-x','1px');surface.style.setProperty('--scale-round-y','1px');task=p.render({canvas:cv,viewport:vp,transform:dpr!==1?[dpr,0,0,dpr,0,0]:undefined});layer=new TextLayer({textContentSource:await p.getTextContent(),container:surface,viewport:vp});await Promise.all([task.promise,layer.render()]);if(!cancelled)detachGuard=attachSelectionGuard(surface);}).catch(()=>{});return()=>{cancelled=true;task?.cancel();layer?.cancel();detachGuard?.();if(pageBox)pageViewports.delete(pageBox);};},[pdf,page,scale,rotation,visible]);
  function capture(event:{clientX:number;clientY:number}) {
    if(tool==='area')return;
    const selection=window.getSelection(), start=selectionStart.current;
    selectionStart.current=null;
    if(!selection||selection.isCollapsed||!selection.rangeCount||!viewport||!text.current)return;
    normalizePdfLineSelection(text.current,selection,start,{x:event.clientX,y:event.clientY});
    const root=box.current?.closest('.pdf-scroll');
    if(!root||!text.current.contains(selection.anchorNode)||!root.contains(selection.focusNode))return;
    const source=selection.getRangeAt(0), pages:Data[]=[];
    for(const node of root.querySelectorAll<HTMLElement>('.continuous-page')) {
      const surface=node.querySelector<HTMLElement>('.textLayer'), pageViewport=pageViewports.get(node);
      if(!surface||!pageViewport||!source.intersectsNode(surface))continue;
      const range=document.createRange();
      range.selectNodeContents(surface);
      if(surface.contains(source.startContainer))range.setStart(source.startContainer,source.startOffset);
      if(surface.contains(source.endContainer))range.setEnd(source.endContainer,source.endOffset);
      const quote=range.toString();
      if(!quote.trim())continue;
      const bounds=node.getBoundingClientRect(), viewportRects:number[][]=[];
      const rects=mergeSelectionRects(range.getClientRects()).map(r=>{
        viewportRects.push([r.left-bounds.left,r.top-bounds.top,r.right-bounds.left,r.bottom-bounds.top]);
        const a=pageViewport.convertToPdfPoint(r.left-bounds.left,r.top-bounds.top),b=pageViewport.convertToPdfPoint(r.right-bounds.left,r.bottom-bounds.top);
        return [a[0],a[1],b[0],b[1]];
      });
      if(rects.length)pages.push({page:Number(node.dataset.page),viewport:pageViewport,rects,viewportRects,quote});
    }
    if(pages.length)onSelect(pages.length===1?pages[0]:{...pages[0],pages,quote:pages.map(value=>value.quote).join('\n\n')});
  }
  captureRef.current=capture;
  function rectStyle(rect:number[]){if(!viewport)return {};const a=viewport.convertToViewportPoint(rect[0],rect[1]),b=viewport.convertToViewportPoint(rect[2],rect[3]);return {left:Math.min(a[0],b[0]),top:Math.min(a[1],b[1]),width:Math.abs(a[0]-b[0]),height:Math.abs(a[1]-b[1])};}
  return <div className="continuous-page" data-page={page} ref={box} style={{width:viewport?.width||size?.width,height:viewport?.height||size?.height||Math.max(700,1050*scale)}} onMouseDown={e=>{if(tool!=='area'){selectionStart.current={x:e.clientX,y:e.clientY};onStartSelection?.();if(text.current&&box.current)trackPdfSelectionDrag(text.current,box.current,event=>captureRef.current(event));}}}>{visible&&<><canvas ref={canvas}/><div ref={text} className="textLayer" style={{pointerEvents:tool==='area'?'none':'auto'}}/><div className="annotation-layer">{annotations.filter(a=>a.pageIndex===page-1&&!a.stale).flatMap(a=>a.rects.map((rect:number[],i:number)=><div key={a.id+i} className={'annotation-mark '+a.type+(activeId===a.id?' focused':'')} style={{...rectStyle(rect),backgroundColor:a.type==='highlight'?a.color+'66':'transparent',borderColor:a.color}}/>))}</div></>}{tool==='area'&&<div className="area-capture" onPointerDown={e=>{const bounds=e.currentTarget.getBoundingClientRect();areaStart.current=[e.clientX-bounds.left,e.clientY-bounds.top];e.currentTarget.setPointerCapture(e.pointerId);}} onPointerMove={e=>{if(!areaStart.current)return;const bounds=e.currentTarget.getBoundingClientRect();setArea([...areaStart.current,e.clientX-bounds.left,e.clientY-bounds.top]);}} onPointerUp={e=>{if(!areaStart.current)return;if(!viewport){areaStart.current=null;setArea(null);return;}const bounds=e.currentTarget.getBoundingClientRect(),end=[e.clientX-bounds.left,e.clientY-bounds.top],start=areaStart.current;if(Math.abs(end[0]-start[0])>4&&Math.abs(end[1]-start[1])>4){const a=viewport.convertToPdfPoint(...start),b=viewport.convertToPdfPoint(...end);onSelect({page,viewport,rects:[[...a,...b]],quote:''});}areaStart.current=null;setArea(null);}}>{area&&<div className="area-preview" style={{left:Math.min(area[0],area[2]),top:Math.min(area[1],area[3]),width:Math.abs(area[2]-area[0]),height:Math.abs(area[3]-area[1])}}/>}</div>}<div className="continuous-page-number">第 {page} 页</div></div>;
}
