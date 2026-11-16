import { defineTool } from '@deepseek-ai/dsh-tools'

export const object = (description, required = false) => ({type:'object',additionalProperties:true,description,...(required?{required:true}:{})})
export const string = (description, required = false) => ({type:'string',description,...(required?{required:true}:{})})
export const integer = (description, required = false) => ({type:'integer',description,...(required?{required:true}:{})})

export function serviceUrl(config={}) {
  const url=new URL(config.serviceUrl || process.env.GIBBS_TOOLS_URL || 'http://127.0.0.1:8766')
  if(!['http:','https:'].includes(url.protocol) || url.username || url.password)throw new Error('Configure an HTTP(S) tools service URL without embedded credentials')
  if(url.protocol==='http:' && !['127.0.0.1','localhost','[::1]'].includes(url.hostname))throw new Error('Use HTTPS for a remote tools service')
  return url.href.replace(/\/$/,'')
}

export async function request(config,path,body,signal,{method='POST',raw=false}={}) {
  const token=process.env.GIBBS_TOOLS_TOKEN||''
  const res=await fetch(serviceUrl(config)+path,{method:body===undefined?'GET':method,headers:{...(token?{Authorization:`Bearer ${token}`} : {}),...(body!==undefined&&!raw?{'Content-Type':'application/json'}:{})},body:body===undefined?undefined:raw?body:JSON.stringify(body),signal:AbortSignal.any([...(signal?[signal]:[]),AbortSignal.timeout(config.timeoutMs||180000)])})
  if(!res.ok){const err=await res.json().catch(()=>({}));throw new Error(`Gibbs HTTP ${res.status}: ${typeof err.detail==='string'?err.detail:res.statusText}`)}
  return res.json()
}

export function register(ctx,config,{name,description,parameters,path,body=arg=>arg,method,transform}) {
  return ctx.tools.register(defineTool({name,description,parameters,
    output:{schema:{type:'json'},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
    async execute(args,exec){const result=await request(config,typeof path==='function'?path(args):path,method==='GET'?undefined:body(args),exec.signal,{method});return transform?transform(result,config):result},
  }))
}

export function artifactSummary(item,config){
  const base=serviceUrl(config),path=`/api/${item.kind==='plot'?'plots':'reports'}/${item.artifact_id}/${item.revision}`
  return {artifact_id:item.artifact_id,revision:item.revision,kind:item.kind,preview_url:base+item.preview_path,exports:Object.fromEntries((item.kind==='plot'?['png','svg','pdf']:['html','md','json','pdf']).map(fmt=>[fmt,`${base}${path}/${fmt}`])),...(item.warnings?{warnings:item.warnings}:{})}
}
