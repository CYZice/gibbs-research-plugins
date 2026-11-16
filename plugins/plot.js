import { object,string,integer,register,artifactSummary } from './bridge.js'

export const name='gibbs-plot'
export const inject=['tools']
export function apply(ctx,config={}){
  register(ctx,config,{name:'gibbs_plot_save',description:'生成或反馈重画科研图。保留用户数据；每次保存产生独立版本，返回预览和 PNG/SVG/PDF 下载地址。修改时传 artifact_id 和 base_revision；不要省略既有 caption/provenance。',parameters:{spec:object('图表规格：type 为 line/bar/scatter/stacked_area/rdf/arrhenius/nyquist/pie；series、categories、xLabel、yLabel、单位及 style',true),caption:string('图注'),provenance:object('数据来源、研究条件及原文标识'),artifact_id:string('修改已有图时的标识'),base_revision:integer('修改基于的最新版本'),feedback:string('本轮修改意见'),feedback_id:string('完成某条待处理反馈时填写其 id')},path:'/api/plots',transform:artifactSummary})
  register(ctx,config,{name:'gibbs_artifact_read',description:'读取图表完整规格或报告草稿、证据和所引用的图表版本。',parameters:{artifact_id:string('产物标识',true),revision:integer('省略时读取最新版本')},path:a=>`/api/artifacts/${encodeURIComponent(a.artifact_id)}${a.revision?`?revision=${a.revision}`:''}`,method:'GET'})
  register(ctx,config,{name:'gibbs_feedback_pending',description:'读取用户在预览页面提交的待处理反馈。读取对应产物，按反馈修改并保存新版本；保存时传 feedback_id。',parameters:{},path:'/api/feedback',method:'GET'})
}
