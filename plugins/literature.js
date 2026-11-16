import { readFile, realpath, stat } from 'node:fs/promises'
import { isAbsolute, relative, resolve, sep } from 'node:path'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { object, string, register, request } from './bridge.js'

export const name='gibbs-literature'
export const inject=['tools']

export function apply(ctx,config={}){
  const actions={
    search:'检索文献证据候选。payload: {query,scope?:{paper_ids,evidence_ids},top_k?,page_size?}。搜索后须阅读正文上下文与原文位置，再用于论断。',
    papers:'检索本地论文。payload: {query?,filters?,page?,page_size?}。',
    discover:'搜索开放学术资源。payload: {query,sources:["local","openalex","core","arxiv"],filters?,page_size?}。',
    import:'按文献库原有导入契约登记外部论文；payload 详见文献库 /docs，返回异步任务后继续查询状态。',
    verify:'核验显式研究论断。payload: {claims:[{text,claim_id?}],scope?,top_k_per_claim?}；返回 run_id 后用 status 查询 workflows。',
    ground:'为草稿组织可核验引用。payload: {draft,scope?,top_k_per_claim?}；返回 run_id 后用 status 查询 workflows。',
    bibliography:'按论文标识生成引文。payload: {paper_ids,reference_ids?,style:"gbt7714"}。',
  }
  for(const [action,description] of Object.entries(actions))register(ctx,config,{name:`gibbs_literature_${action}`,description,parameters:{payload:object('文献库 v1 API 请求体',true)},path:`/api/literature/${action}`,body:args=>args.payload})
  register(ctx,config,{name:'gibbs_literature_read',description:'读取证据正文上下文与原文出处，返回 paper_id、evidence_id、页码和位置。',parameters:{evidence_id:string('证据标识',true)},path:a=>`/api/literature/evidence/${encodeURIComponent(a.evidence_id)}`,method:'GET'})
  register(ctx,config,{name:'gibbs_literature_status',description:'续接论文处理 task、证据核验/引用 workflow 或批量导入。',parameters:{kind:{type:'string',enum:['tasks','workflows','import-batches'],required:true},id:string('task_id、run_id 或 batch_id',true)},path:a=>`/api/literature/status/${a.kind}/${encodeURIComponent(a.id)}`,method:'GET'})
  register(ctx,config,{name:'gibbs_literature_resource',description:'读取论文元数据、正文窗口、已有证据、图表资源、参考/被引关系或文内引用上下文。content 用 params:{offset,limit} 分页；relations 用 params:{relation:"references"或"citations",page,page_size}。',parameters:{paper_id:string('论文标识',true),kind:{type:'string',enum:['paper','content','evidence','assets','relations','citation-contexts'],required:true},params:object('窗口和分页参数')},path:'/api/literature-resource'})
  ctx.tools.register(defineTool({name:'gibbs_literature_upload',description:'上传用户授权 PDF。路径必须在显式配置的 GIBBS_IMPORT_ROOT 目录中；不会读取其他目录。',parameters:{path:string('导入目录内 PDF 的绝对路径',true)},output:{schema:{type:'json'},render:(_a,v)=>[{type:'text',text:JSON.stringify(v)}]},async execute(args,exec){
    const root=process.env.GIBBS_IMPORT_ROOT
    if(!root||!isAbsolute(args.path))throw new Error('Set GIBBS_IMPORT_ROOT and supply an absolute PDF path')
    const [base,path]=await Promise.all([realpath(resolve(root)),realpath(args.path)])
    const rel=relative(base,path)
    if(!rel||isAbsolute(rel)||rel==='..'||rel.startsWith(`..${sep}`))throw new Error('PDF path is outside GIBBS_IMPORT_ROOT')
    const info=await stat(path)
    if(!info.isFile()||info.size>50*1024*1024)throw new Error('Expected a PDF file under 50MB')
    const data=await readFile(path,{signal:exec.signal})
    return request(config,'/api/literature-upload',data,exec.signal,{raw:true})
  }}))
}
