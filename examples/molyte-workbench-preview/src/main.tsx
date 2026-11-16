import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { createPortal } from 'react-dom';
import { ConfigProvider } from 'antd';
import { ApartmentOutlined, PlusOutlined, MinusOutlined, FullscreenOutlined, DownloadOutlined, SearchOutlined, AimOutlined, ArrowRightOutlined, CloseOutlined, FileTextOutlined } from '@ant-design/icons';
import LiteratureWorkbench from '../original/src/features/literature/LiteratureWorkbench';
import fixtures from '../fixtures.json';
import './preview.css';

import { nodes, edges, extraNodes, extraEdges, paperLevels, shortenTitle, levelStyles, type PaperNode } from './graph-data';

function Graph({onOpenPaper}:{onOpenPaper:(id:string)=>void}){
 const [selected,setSelected]=useState('attention');
 const [positions,setPositions]=useState(nodes);
 const [zoom,setZoom]=useState(1);
 const [pan,setPan]=useState({x:0,y:0});
 const [query,setQuery]=useState('');
 const [neighbors,setNeighbors]=useState(false);
 const [expanded,setExpanded]=useState(false);
 const svg=useRef<SVGSVGElement>(null);
 const dragging=useRef<{id:string;x:number;y:number}|null>(null);
 const points=expanded?[...positions,...extraNodes]:positions;
 const displayEdges=expanded?[...edges,...extraEdges]:edges;
 const active=points.find(n=>n.id===selected)!;
 const levels=paperLevels(points,displayEdges);
 const linked=new Set([selected,...displayEdges.filter(e=>e[0]===selected||e[1]===selected).flat()]);
 const visible=(n:PaperNode)=>(!neighbors||linked.has(n.id))&&(!query||n.title.toLowerCase().includes(query.toLowerCase())||n.id===selected);
 const paper=fixtures.papers.find(p=>p.paper_id===active.id);
 const related=displayEdges.filter(e=>e[0]===selected||e[1]===selected);
 const openSource=()=>paper?onOpenPaper(paper.paper_id):window.open(`https://arxiv.org/abs/${active.arxiv}`,'_blank','noopener,noreferrer');
 const detailHost=document.querySelector('.literature-inspector');
 function toggleNeighbors(){
  if(neighbors){setNeighbors(false);setPan({x:0,y:0});setZoom(1);return;}
  const group=points.filter(n=>linked.has(n.id));
  const xs=group.map(n=>n.x),ys=group.map(n=>n.y);
  const left=Math.min(...xs),right=Math.max(...xs),top=Math.min(...ys),bottom=Math.max(...ys);
  setNeighbors(true);
  setPan({x:520-(left+right)/2,y:460-(top+bottom)/2});
  setZoom(Math.min(1.25,840/Math.max(1,right-left),700/Math.max(1,bottom-top)));
 }
 function download(){const blob=new Blob([JSON.stringify({nodes:points,edges:displayEdges},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='literature-graph.json';a.click();URL.revokeObjectURL(a.href);}
 return <>
  <section className="graph-panel">
   <div className="graph-toolbar">
    <label className="graph-search"><SearchOutlined/><input aria-label="搜索图谱节点" placeholder="Search papers" value={query} onChange={e=>setQuery(e.target.value)}/>{query&&<button onClick={()=>setQuery('')} aria-label="清除搜索"><CloseOutlined/></button>}</label>
    <button className={'graph-neighbors '+(neighbors?'on':'')} onClick={toggleNeighbors} title="仅显示相邻节点" aria-label="仅显示相邻节点"><AimOutlined/></button>
   </div>
   <div className="graph-canvas" onWheel={e=>setZoom(z=>Math.min(1.7,Math.max(.65,z-e.deltaY*.0008)))}>
    <svg ref={svg} viewBox="0 0 1040 920" role="img" aria-label="文献知识图谱" onPointerMove={e=>{if(!dragging.current||!svg.current)return;const scale=1040/svg.current.getBoundingClientRect().width/zoom;const dx=(e.clientX-dragging.current.x)*scale,dy=(e.clientY-dragging.current.y)*scale;dragging.current.x=e.clientX;dragging.current.y=e.clientY;if(dragging.current.id==='pan')setPan(p=>({x:p.x+dx,y:p.y+dy}));else setPositions(ns=>ns.map(n=>n.id===dragging.current!.id?{...n,x:n.x+dx,y:n.y+dy}:n));}} onPointerUp={()=>dragging.current=null} onPointerCancel={()=>dragging.current=null}>
     <rect width="1040" height="920" fill="#fff" onPointerDown={e=>{e.currentTarget.setPointerCapture(e.pointerId);dragging.current={id:'pan',x:e.clientX,y:e.clientY};}}/>
     <g transform={`translate(${520+pan.x} ${460+pan.y}) scale(${zoom}) translate(-520 -460)`}>
      {displayEdges.map(([a,b],i)=>{const from=points.find(n=>n.id===a)!,to=points.find(n=>n.id===b)!;if(!visible(from)||!visible(to))return null;return <line key={i} x1={from.x} y1={from.y} x2={to.x} y2={to.y} stroke="#dce4ee" strokeWidth="1.2"/>})}
      {points.filter(visible).map(n=>{const level=levels.get(n.id)!;const style=levelStyles[Math.min(level,3)],focus=n.id===selected;return <g key={n.id} data-node={n.id} data-level={level} role="button" tabIndex={0} aria-label={n.title} className="graph-node" transform={`translate(${n.x} ${n.y})`} onClick={()=>setSelected(n.id)} onKeyDown={e=>{if(e.key==='Enter')setSelected(n.id)}} onDoubleClick={()=>{setSelected(n.id);setExpanded(true)}} onPointerDown={e=>{e.stopPropagation();e.currentTarget.setPointerCapture(e.pointerId);dragging.current={id:n.id,x:e.clientX,y:e.clientY};}}>
       <title>{n.title}</title>
       <circle r={style.radius} fill={focus?'#4278bc':style.color}/>
       <text y={style.radius+20} textAnchor="middle" fill="#465b73" fontSize={style.fontSize} fontWeight={level===0?600:400}>{shortenTitle(n.title)}</text>
      </g>})}
     </g>
    </svg>
    <div className="graph-metrics">{points.filter(visible).length} papers <span>·</span> {displayEdges.filter(([a,b])=>visible(points.find(n=>n.id===a)!)&&visible(points.find(n=>n.id===b)!)).length} links</div>
    <div className="graph-controls"><button title="放大" aria-label="放大图谱" onClick={()=>setZoom(z=>Math.min(1.7,z+.15))}><PlusOutlined/></button><button title="缩小" aria-label="缩小图谱" onClick={()=>setZoom(z=>Math.max(.65,z-.15))}><MinusOutlined/></button><span>{Math.round(zoom*100)}%</span><button title="适应画布" aria-label="适应画布" onClick={()=>{setZoom(1);setPan({x:0,y:0})}}><FullscreenOutlined/></button><button title="导出" aria-label="导出图谱" onClick={download}><DownloadOutlined/></button></div>
   </div>
  </section>
  {detailHost&&createPortal(<section className="graph-details">
   <div className="panel-label"><span>Paper</span></div>
   <div className="graph-detail-content">
    
    <h2>{active.title}</h2>
    <div className="node-relations"><b>{related.length}</b> related papers</div>
    <div className="detail-heading">Related papers<button aria-label="展开关联节点" onClick={()=>{setExpanded(true);setNeighbors(false)}}><PlusOutlined/></button></div>
    <div className="relation-list">{related.map(([a,b],i)=>{const n=points.find(n=>n.id===(a===selected?b:a))!;return <button key={i} title={n.title} onClick={()=>setSelected(n.id)}><i style={{background:levelStyles[Math.min(levels.get(n.id)!,3)].color}}/><span>{shortenTitle(n.title,44)}</span><ArrowRightOutlined/></button>})}</div>
    <div className="detail-heading">Source</div>
    <button className="graph-source" onClick={openSource}><FileTextOutlined/><span><strong>{active.title}</strong><small>{active.year}</small><small>arXiv:{active.arxiv}</small></span><ArrowRightOutlined/></button>
    {paper&&<><div className="detail-heading">Evidence</div>
    <div className="graph-quote">{fixtures.evidence.find(e=>e.paper.paper_id===paper.paper_id)?.content.quote.slice(0,560)}<button onClick={openSource}>Open paper <ArrowRightOutlined/></button></div></>}
   </div>
  </section>,detailHost)}
 </>;
}

function App(){
 const [params,setParams]=useState(()=>new URLSearchParams(location.search));
 const [graph,setGraph]=useState(()=>new URLSearchParams(location.search).get('panel')==='graph');
 const [host,setHost]=useState<Element|null>(null);
 const [tabs,setTabs]=useState<Element|null>(null);
 useEffect(()=>{const update=()=>{setHost(document.querySelector('.literature-reader'));setTabs(document.querySelector('.view-switcher'));};const o=new MutationObserver(update);o.observe(document.body,{childList:true,subtree:true});update();return()=>o.disconnect()},[]);
 useEffect(()=>{document.body.classList.toggle('graph-view',graph)},[graph]);
 useEffect(()=>{const click=(e:MouseEvent)=>{const b=(e.target as Element).closest('.view-switcher button');if(b&&!b.classList.contains('graph-tab'))setGraph(false)};document.addEventListener('click',click);return()=>document.removeEventListener('click',click)},[]);
 function openPaper(id:string){const p=new URLSearchParams(params);p.set('paper',`paper:${id}`);p.set('view','pdf');p.delete('panel');p.set('pdf_page','1');setGraph(false);setParams(p);history.replaceState(null,'','?'+p.toString())}
 return <ConfigProvider theme={{token:{colorPrimary:'#5b8def',fontFamily:'"Segoe UI", "Microsoft YaHei", sans-serif',borderRadius:6}}}>
  <LiteratureWorkbench mode="standalone" targetProjectId={null} initialSearch={params} onUrlStateChange={next=>{setParams(next);history.replaceState(null,'','?'+next.toString()+(graph?'&panel=graph':''))}}/>
  {tabs&&createPortal(<button type="button" role="tab" className={graph?'graph-tab active':'graph-tab'} aria-selected={graph} onClick={()=>{setGraph(true);history.replaceState(null,'','?'+params.toString()+'&panel=graph')}}><ApartmentOutlined/> 知识图谱</button>,tabs)}
  {graph&&host&&createPortal(<Graph onOpenPaper={openPaper}/>,host)}
  {graph&&tabs&&createPortal(<button className="exit-graph" aria-label="返回 PDF" onClick={()=>setGraph(false)}><CloseOutlined/></button>,tabs)}
 </ConfigProvider>
}
createRoot(document.getElementById('root')!).render(<App/>);
