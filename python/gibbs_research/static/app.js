const $ = id => document.getElementById(id)
const paths = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  plot: '<path d="M4 4v16h16M7 14l4-5 4 3 5-7"/>',
  report: '<path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8zM14 3v5h5M9 12h6M9 16h6"/>',
  key: '<circle cx="8" cy="9" r="4"/><path d="m11 12 8 8m-4-4 3-3m-1 5 3-3"/>',
  compare: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16M7 9l-2 3 2 3m10-6 2 3-2 3"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/>',
  code: '<path d="m8 7-5 5 5 5m8-10 5 5-5 5m-3-14-2 18"/>',
  type: '<path d="M4 6V4h16v2M12 4v16m-4 0h8"/>',
  legend: '<path d="M4 6h3m4 0h9M4 12h3m4 0h9M4 18h3m4 0h9"/>',
  refresh: '<path d="M20 10a8 8 0 0 0-14-5L3 8m0-5v5h5M4 14a8 8 0 0 0 14 5l3-3m0 5v-5h-5"/>',
  save: '<path d="M4 3h13l4 4v14H3V3zM7 3v6h10V3M7 21v-8h10v8"/>',
  send: '<path d="M12 19V5m-6 6 6-6 6 6"/>',
  chevron: '<path d="m9 5 7 7-7 7"/>',
}
function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  for (const [key, value] of Object.entries({viewBox:'0 0 24 24', fill:'none', stroke:'currentColor', 'stroke-width':'1.6', 'stroke-linecap':'round', 'stroke-linejoin':'round', 'aria-hidden':'true', class:'icon'})) svg.setAttribute(key, value)
  svg.innerHTML = paths[name]
  return svg
}
for (const el of document.querySelectorAll('[data-icon]')) el.append(icon(el.dataset.icon))
let current = null
let busy = false
let comparing = false
const sample = {type:'line',title:'不同方法的性能比较',xLabel:'样本数',yLabel:'准确率',yUnit:'%',series:[{name:'方法 A',data:[[10,71],[20,78],[40,84],[80,89]]},{name:'方法 B',data:[[10,68],[20,75],[40,80],[80,85]]}],style:{font_size:12,legend_location:'best'}}
function status(text = '', error = false) {
  $('status').textContent = text
  $('status').classList.toggle('error', error)
}
function buttonLabel(id, label) { $(id).title = label; $(id).setAttribute('aria-label', label) }
async function api(path, data, method = 'POST') {
  const res = await fetch(path, data === undefined ? {} : {method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)})
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || res.statusText))
  }
  return res.json()
}
async function refresh() {
  const items = await api('/api/artifacts')
  $('artifacts').replaceChildren()
  for (const item of items) {
    const button = document.createElement('button'), label = document.createElement('span')
    button.className = 'artifact'
    button.classList.toggle('active', current?.artifact_id === item.artifact_id)
    button.setAttribute('aria-current', current?.artifact_id === item.artifact_id ? 'page' : 'false')
    button.title = `${item.title} · ${item.kind === 'plot' ? '图表' : '报告'} · v${item.revision}`
    label.textContent = item.title
    button.append(icon(item.kind === 'plot' ? 'plot' : 'report'), label)
    button.onclick = () => run(() => load(item.artifact_id))
    $('artifacts').append(button)
  }
}
function showPreview() {
  comparing = false
  $('compare').setAttribute('aria-pressed', 'false')
  const isPlot = current.kind === 'plot'
  const preview = document.createElement(isPlot ? 'img' : 'iframe')
  preview.src = `/api/${isPlot ? 'plots' : 'reports'}/${current.artifact_id}/${current.revision}/${isPlot ? 'png' : 'html'}`
  if (isPlot) preview.alt = current.spec.title || '图表预览'
  else { preview.title = '报告预览'; preview.sandbox = 'allow-same-origin' }
  $('preview').classList.toggle('is-report', !isPlot)
  $('preview').replaceChildren(preview)
}
async function load(id, revision) {
  current = await api(`/api/artifacts/${id}${revision ? `?revision=${revision}` : ''}`)
  const isPlot = current.kind === 'plot'
  $('title').textContent = isPlot ? (current.spec.title || '研究图表') : current.prepared.title
  $('source-editor').open = false
  $('editor').value = JSON.stringify(isPlot ? current.spec : {material:current.prepared.material,draft:current.source_draft}, null, 2)
  $('plot-controls').hidden = !isPlot
  $('font-size').value = isPlot ? (current.spec.style?.font_size || 12) : 12
  $('legend').value = isPlot ? (current.spec.style?.legend_location || 'best') : 'best'
  buttonLabel('save', isPlot ? '保存并重画' : '保存报告')
  $('save').replaceChildren(icon(isPlot ? 'refresh' : 'save'))
  $('compare').hidden = !isPlot || current.revision < 2
  $('feedback').value = ''
  showPreview()
  $('download').hidden = false
  $('download').open = false
  $('exports').replaceChildren()
  for (const fmt of isPlot ? ['png','svg','pdf'] : ['html','md','json','pdf']) {
    const a = document.createElement('a'), label = document.createElement('span')
    label.textContent = fmt.toUpperCase()
    a.append(icon('download'), label)
    a.href = `/api/${isPlot ? 'plots' : 'reports'}/${id}/${current.revision}/${fmt}`
    a.target = '_blank'; a.rel = 'noopener'
    a.onclick = () => { $('download').open = false }
    $('exports').append(a)
  }
  const history = await api(`/api/artifacts/${id}/history`)
  $('history').replaceChildren(); $('history').hidden = false
  for (const item of history) {
    const option = document.createElement('option')
    option.value = item.revision; option.textContent = `v${item.revision}`
    option.title = `${new Date(item.created_at).toLocaleString()}${item.feedback ? ' · ' + item.feedback : ''}`
    option.selected = item.revision === current.revision
    $('history').append(option)
  }
  status()
  await refresh()
}
async function run(work) {
  if (busy) return
  busy = true
  for (const id of ['save','send','new','history','compare','login']) $(id).disabled = true
  $('save').setAttribute('aria-busy', 'true')
  try { await work() } catch (error) { status(error.message, true) }
  finally {
    busy = false
    for (const id of ['save','new','history','compare','login']) $(id).disabled = false
    $('send').disabled = !current
    $('save').setAttribute('aria-busy', 'false')
  }
}
function newPlot() {
  current = null; comparing = false
  $('editor').value = JSON.stringify(sample, null, 2)
  $('title').textContent = '新图表'
  $('preview').classList.remove('is-report')
  const empty = document.createElement('div'), label = document.createElement('span')
  empty.className = 'empty'; label.textContent = '添加数据'
  empty.append(icon('plot'), label); $('preview').replaceChildren(empty)
  $('font-size').value = 12; $('legend').value = 'best'
  $('source-editor').open = true; $('history').hidden = true; $('compare').hidden = true
  $('compare').setAttribute('aria-pressed', 'false')
  $('download').hidden = true; $('exports').replaceChildren(); $('plot-controls').hidden = false
  $('send').disabled = true; buttonLabel('save', '生成预览'); $('feedback').value = ''; status()
  $('save').replaceChildren(icon('refresh'))
  for (const button of $('artifacts').children) { button.classList.remove('active'); button.setAttribute('aria-current', 'false') }
}
$('new').onclick = () => { if (!busy) newPlot() }
$('save').onclick = () => run(async () => {
  const data = JSON.parse($('editor').value), isPlot = !current || current.kind === 'plot'
  if (isPlot) data.style = {...data.style,font_size:Number($('font-size').value),legend_location:$('legend').value}
  const body = isPlot ? {spec:data,caption:current?.caption || '',provenance:current?.provenance || {source:'用户输入'}} : data
  Object.assign(body, {artifact_id:current?.artifact_id,base_revision:current?.revision,feedback:$('feedback').value})
  status(isPlot ? '重画中…' : '保存中…')
  const saved = await api(isPlot ? '/api/plots' : '/api/reports', body)
  await load(saved.artifact_id, saved.revision)
  $('source-editor').open = false; status('已保存')
})
$('send').onclick = () => run(async () => {
  if (!current) throw new Error('请先生成预览')
  if (!$('feedback').value.trim()) throw new Error('请输入修改意见')
  await api(`/api/artifacts/${current.artifact_id}/feedback`, {revision:current.revision,text:$('feedback').value})
  $('feedback').value = ''; status('已提交，回到 Agent 继续修改。')
})
$('history').onchange = () => run(() => load(current.artifact_id, Number($('history').value)))
$('compare').onclick = () => run(async () => {
  if (comparing) { showPreview(); status(); return }
  const previous = await api(`/api/artifacts/${current.artifact_id}?revision=${current.revision - 1}`)
  const wrap = document.createElement('div'); wrap.className = 'comparison'
  for (const item of [previous, current]) {
    const box = document.createElement('div'), label = document.createElement('p'), img = document.createElement('img')
    label.textContent = `v${item.revision}`; img.alt = `版本 ${item.revision} 图表`
    img.src = `/api/plots/${item.artifact_id}/${item.revision}/png`
    box.append(label, img); wrap.append(box)
  }
  $('preview').replaceChildren(wrap); comparing = true; $('compare').setAttribute('aria-pressed', 'true'); status()
})
$('login').onclick = () => run(async () => {
  const token = prompt('访问令牌')
  if (token === null) return
  await api('/auth/login', {token}); await refresh(); status('已连接')
})
document.addEventListener('click', event => { if (!$('download').contains(event.target)) $('download').open = false })
document.addEventListener('keydown', event => { if (event.key === 'Escape') $('download').open = false })
newPlot()
run(async () => {
  await refresh()
  const params = new URLSearchParams(location.search)
  if (params.get('artifact')) { await load(params.get('artifact'), Number(params.get('revision')) || undefined); $('source-editor').open = false }
})
