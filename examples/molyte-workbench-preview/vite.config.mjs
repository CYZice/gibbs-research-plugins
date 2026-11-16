import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
const require = createRequire('D:/Microsoft VS Code/molyte_web_v2.0_resnet/frontend/package.json');
const { defineConfig } = require('vite');
const react = require('@vitejs/plugin-react');
const root = path.dirname(fileURLToPath(import.meta.url));
const { papers, evidence } = JSON.parse(fs.readFileSync(path.join(root, 'fixtures.json'), 'utf8'));
const snapshot = (p,i) => ({id:i+1,identity_key:`paper:${p.paper_id}`,local_paper_id:p.paper_id,title:p.title,authors:p.authors,year:p.year,venue:p.venue,origins:[{source:'local',id:p.paper_id}],availability_snapshot:'local_ready',snapshot_kind:'metadata_only',is_favorite:true,collection_ids:[1],created_at:'2026-11-13',updated_at:'2026-11-13',last_seen_at:'2026-11-13'});
let saved = papers.slice(0,3).map(snapshot);
const collection = {id:1,name:'科研 Agent · 基础文献',description:'Transformer、检索增强与高效适配',item_count:3,items:saved,created_at:'2026-11-13',updated_at:'2026-11-13'};
const mock = () => ({name:'offline-literature-demo',configureServer(server){
 server.middlewares.use('/api/v1',async (req,res,next)=>{
  const url = new URL(req.url,'http://localhost'); const pathname=decodeURIComponent(url.pathname);
  let body='';for await(const chunk of req)body+=chunk;
  const data = body && req.headers['content-type']?.includes('application/json') ? JSON.parse(body):{};
  const send=(value)=>{res.setHeader('Content-Type','application/json; charset=utf-8');res.end(JSON.stringify(value));};
  if(pathname==='/literature/status')return send({status:'available',full_text_available:true,metadata_fallback:false});
  if(pathname==='/literature/uploads')return send([]);
  if(pathname.startsWith('/projects'))return send([]);
  if(pathname==='/literature/collections')return send([collection]);
  if(pathname==='/literature/saved-papers'){
   if(req.method==='POST'){const found=saved.find(p=>p.identity_key===data.identity_key);const item={...data,id:found?.id||saved.length+1,collection_ids:[1],snapshot_kind:'metadata_only',is_favorite:data.is_favorite,updated_at:'2026-11-13'};saved=[...saved.filter(p=>p.id!==item.id),item];return send(item);}
   return send(saved);
  }
  if(pathname.startsWith('/literature/saved-papers/')){const id=Number(pathname.split('/').pop());const item=saved.find(p=>p.id===id);if(item)item.is_favorite=data.is_favorite;return send({deleted:false,saved_paper:item});}
  if(pathname==='/literature/search'){
   let items=[...papers];const q=(data.query||'').toLowerCase();
   if(q && !['transformer','large language model','language model','research','agent'].includes(q))items=items.filter(p=>(p.title+' '+p.abstract+' '+p.authors.map(a=>a.name).join(' ')).toLowerCase().includes(q));
   if(data.library_scope==='favorites')items=items.filter(p=>saved.some(s=>s.local_paper_id===p.paper_id && s.is_favorite));
   if(data.year_from)items=items.filter(p=>p.year>=data.year_from);if(data.year_to)items=items.filter(p=>p.year<=data.year_to);
   if(data.sort==='year_desc')items.sort((a,b)=>b.year-a.year);
   return send({items,pagination:{page:1,page_size:30,total:items.length,total_pages:1,has_next:false,has_prev:false}});
  }
  if(pathname==='/literature/evidence/search'||pathname==='/literature/evidence/global-search'){
   const ids=data.scope?.paper_ids;return send({items:ids?evidence.filter(e=>ids.includes(e.paper.paper_id)):evidence});
  }
  const paperMatch=pathname.match(/^\/literature\/papers\/([^/]+)(\/pdf)?$/);
  if(paperMatch){const p=papers.find(p=>p.paper_id===paperMatch[1]);if(!p){res.statusCode=404;return send({detail:'Paper not found'});}if(paperMatch[2]){res.setHeader('Content-Type','application/pdf');return fs.createReadStream(path.join(root,'public/papers',p.paper_id+'.pdf')).pipe(res);}return send(p);}
  const evMatch=pathname.match(/^\/literature\/evidence\/([^/]+)\/(source|content)$/);
  if(evMatch){const e=evidence.find(e=>e.evidence_id===evMatch[1]);if(!e){res.statusCode=404;return send({detail:'Evidence not found'});}if(evMatch[2]==='source')return send({citation:{evidence_id:e.evidence_id,paper:e.paper,quote:e.content.quote,location:e.location},pdf_asset_id:e.paper.paper_id});return send({evidence_id:e.evidence_id,paper:e.paper,location:e.location,content:{text:e.content.quote,evidence_char_start:0,evidence_char_end:e.content.quote.length,window_char_start:0,window_char_end:e.content.quote.length,more_before:false,more_after:false}});}
  res.statusCode=404;send({detail:`Offline demo does not implement ${pathname}`});
 });
}});
export default defineConfig({root,plugins:[react.default(),mock()],server:{host:'127.0.0.1',port:5191,strictPort:true,fs:{allow:[root,'D:/Microsoft VS Code/molyte_web_v2.0_resnet/frontend']}},resolve:{dedupe:['react','react-dom']}});
