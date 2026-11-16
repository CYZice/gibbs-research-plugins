import test from 'node:test'
import assert from 'node:assert/strict'
import { Context } from '@deepseek-ai/cordis'
import { SystemPrompt } from '@deepseek-ai/dsh-system-prompt'
import { ToolsService } from '@deepseek-ai/dsh-tools'
import * as native from '@deepseek-ai/dsh-tool-executor-native'
import * as plugin from '../plugins/index.js'

test('native tools create a real plot and a report using the running Python service', {skip:!process.env.GIBBS_TEST_URL}, async()=>{
  const ctx=new Context()
  try{
    await ctx.plugin(SystemPrompt,{includeHarnessIdentity:false}).await()
    await ctx.plugin(ToolsService).await()
    await ctx.plugin(native).await()
    await ctx.plugin(plugin,{serviceUrl:process.env.GIBBS_TEST_URL}).await()
    const signal=new AbortController().signal
    async function call(name,args){
      const batch=await ctx.tools.execute({signal,calls:[{type:'tool-call',id:'live-'+name,name,arguments:JSON.stringify(args)}]})
      const result=batch.results[0].result
      assert.equal(result.isError,false,JSON.stringify(result))
      return result.value
    }
    const plot=await call('gibbs_plot_save',{spec:{type:'line',title:'Native tool integration',xLabel:'t',xUnit:'s',yLabel:'y',series:[{name:'sample',data:[[0,1.5],[1,2.5]]}]},provenance:{source:'synthetic integration test'}})
    const read=await call('gibbs_artifact_read',{artifact_id:plot.artifact_id,revision:1})
    assert.equal(read.spec.series[0].data[1][1],2.5)
    const material={title:'Native report integration',plots:[{artifact_id:plot.artifact_id,revision:1}],results:[{label:'Sample',method:'Synthetic test',metrics:[{name:'response',label:'Response',value:2.5}]}]}
    const prepared=await call('gibbs_report_prepare',{material})
    const figure=prepared.writer_input.evidence_digest[0].figures[0].figure_id
    const draft={abstract:'Synthetic tool integration test.',background:'Demonstrate the workflow.',methods:[{title:'Synthetic input',prose:'Use the supplied sample data.'}],results_by_system:[{title:'Sample',prose:`Response 2.5. [[fig:${figure}]]`}],comparison:'Same sample series.',discussion:'Synthetic data for software verification.',conclusions:['Tool workflow executed.'],recommendations:['Use authorized real data for research.']}
    const report=await call('gibbs_report_save',{material,draft})
    const html=await fetch(report.exports.html).then(r=>r.text())
    assert.ok(html.includes('data:image/png;base64,'))
    assert.ok(html.includes('Native report integration'))
    const svg=await fetch(plot.exports.svg).then(r=>r.text())
    assert.ok(svg.includes('<svg'))
  }finally{await ctx.fiber.dispose()}
})
