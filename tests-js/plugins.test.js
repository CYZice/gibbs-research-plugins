import test from 'node:test'
import assert from 'node:assert/strict'
import http from 'node:http'
import { Context } from '@deepseek-ai/cordis'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolsService } from '@deepseek-ai/dsh-tools'
import * as native from '@deepseek-ai/dsh-tool-executor-native'
import * as plugin from '../plugins/index.js'
import { serviceUrl } from '../plugins/bridge.js'

test('plugin registers and executes through real Cordis tools and native executor',async()=>{
  const received=[]
  const server=http.createServer(async(req,res)=>{
    let raw='';for await(const chunk of req)raw+=chunk
    received.push({path:req.url,body:raw?JSON.parse(raw):null})
    res.setHeader('Content-Type','application/json')
    if(req.url==='/api/plots')res.end(JSON.stringify({artifact_id:'plot_'+'a'.repeat(32),kind:'plot',revision:1,preview_path:'/?artifact=plot_'+ 'a'.repeat(32)}))
    else res.end(JSON.stringify({items:[],path:req.url}))
  })
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve))
  const ctx=new Context()
  try{
    await ctx.plugin(SystemPrompt,{includeHarnessIdentity:false}).await()
    await ctx.plugin(ToolsService).await()
    await ctx.plugin(native).await()
    await ctx.plugin(plugin,{serviceUrl:`http://127.0.0.1:${server.address().port}`}).await()
    assert.equal(ctx.tools.schemas().length,16)
    const signal=new AbortController().signal
    const execute=(name,args)=>ctx.tools.execute({signal,calls:[{type:'tool-call',id:'test-'+name,name,arguments:JSON.stringify(args)}]})
    const searched=await execute('gibbs_literature_search',{payload:{query:'photonic waveguide',scope:{paper_ids:['p1']}}})
    assert.equal(searched.results[0].result.isError,false,JSON.stringify(searched))
    assert.equal(received[0].path,'/api/literature/search')
    assert.equal(received[0].body.scope.paper_ids[0],'p1')
    const plotted=await execute('gibbs_plot_save',{spec:{type:'line',data:[[0,1],[1,2]]}})
    assert.equal(plotted.results[0].result.isError,false,JSON.stringify(plotted))
    const value=plotted.results[0].result.value
    assert.equal(value.revision,1)
    assert.ok(value.exports.svg.endsWith('/1/svg'))
    const bad=await execute('gibbs_plot_save',{spec:'invalid'})
    assert.equal(bad.results[0].result.isError,true)
    const aborted=new AbortController();aborted.abort()
    await assert.rejects(()=>ctx.tools.get('gibbs_feedback_pending').execute({}, {signal:aborted.signal}))
  }finally{await ctx.fiber.dispose();server.closeAllConnections();await new Promise(resolve=>server.close(resolve))}
})

test('remote service URLs require TLS and forbid URL credentials',()=>{
  assert.throws(()=>serviceUrl({serviceUrl:'http://remote.example'}))
  assert.throws(()=>serviceUrl({serviceUrl:'https://user:secret@example.com'}))
  assert.equal(serviceUrl({serviceUrl:'https://research.example'}),'https://research.example')
})
