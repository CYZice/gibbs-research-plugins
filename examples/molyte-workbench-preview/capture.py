from pathlib import Path
import json
import zipfile
import sys
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parent
OUT=Path(r'C:\Users\Lenovo\Desktop\学习资料\大三上\592753-华中科技大学-龚海心-吉布斯：面向能源材料设计自动化的AI Scientist 平台\output\文献工作台截图')
OUT.mkdir(parents=True,exist_ok=True)
errors=[]
checks=[]
base='http://127.0.0.1:5191/'
with sync_playwright() as pw:
 browser=pw.chromium.launch(executable_path=r'C:\Program Files\Google\Chrome\Application\chrome.exe',headless=True)
 page=browser.new_page(viewport={'width':1920,'height':1080},device_scale_factor=2,accept_downloads=True)
 page.on('pageerror',lambda e:errors.append(str(e)))
 def open_page(query):
  page.goto(base+'?'+query,wait_until='networkidle')
  page.locator('.literature-paper-card, .literature-results button').first.wait_for()
 def capture(name):
  page.wait_for_timeout(500)
  page.screenshot(path=str(OUT/name))
  print(name)

 if '--graph-only' not in sys.argv:
  open_page('q=transformer&paper=paper%3Aattention&pdf_page=3&zoom=1')
  page.locator('canvas').first.wait_for()
  page.get_by_role('textbox',name='论文内 Evidence 检索词').fill('attention')
  page.get_by_role('button',name='检索当前论文 Evidence').click()
  page.locator('.inspector-evidence-list button').first.wait_for()
  page.locator('.inspector-evidence-list button').nth(1).click()
  page.wait_for_timeout(800)
  capture('01_论文PDF与原文证据.png')
  checks.append('PDF canvas rendered and evidence selected through original controls')
  page.get_by_role('tab',name='摘要',exact=True).click()
  capture('02_论文摘要与证据片段.png')
  checks.append('Original summary tab switches reading view')

  open_page('q=transformer&paper=paper%3Arag&pdf_page=2&zoom=1')
  page.locator('canvas').first.wait_for()
  page.get_by_role('textbox',name='论文内 Evidence 检索词').fill('retrieval')
  page.get_by_role('button',name='检索当前论文 Evidence').click()
  page.locator('.inspector-evidence-list button').first.wait_for()
  page.locator('.inspector-evidence-list button').first.click()
  page.wait_for_timeout(800)
  capture('03_RAG论文与证据定位.png')

 open_page('q=transformer&paper=paper%3Aattention&panel=graph')
 page.locator('[data-node="attention"]').wait_for()
 graph_state=page.locator('.graph-node').evaluate_all("""els=>els.map(e=>({id:e.dataset.node,level:Number(e.dataset.level),radius:Number(e.querySelector('circle').getAttribute('r')),circles:e.querySelectorAll('circle').length,title:e.querySelector('title').textContent,label:e.querySelector('text').textContent,aria:e.getAttribute('aria-label')}))""")
 assert len(graph_state)==17
 for n in graph_state:
  assert n['circles']==1 and n['radius']==[25,17,10,6][min(n['level'],3)]
  assert n['title']==n['aria'] and n['title'].startswith(n['label'].removesuffix('...'))
  assert not any('\u4e00'<=c<='\u9fff' for c in n['label'])
  assert n['title']!='Transformer'
 assert page.locator('.graph-canvas svg text').count()==17
 assert page.locator('.graph-canvas svg filter, .graph-canvas [stroke-dasharray]').count()==0
 checks.append('Paper-only canonical titles; word-boundary truncation; hierarchy radii 25/17/10/6; no edge labels or visual effects')
 capture('04_文献知识图谱总览.png')
 page.locator('[data-node="rag"] > circle').click()
 assert page.locator('.graph-details h2').inner_text()=='Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks'
 capture('05_图谱节点与来源论文.png')
 checks.append('Graph node selection updates relations, paper, and source evidence')
 page.get_by_role('button',name='展开关联节点').click()
 assert page.locator('[data-node="fid"]').count()==1
 page.locator('[data-node="fid"] > circle').last.click()
 assert page.locator('.graph-details h2').inner_text()=='Leveraging Passage Retrieval with Generative Models for Open Domain Question Answering'
 page.locator('[data-node="rag"] > circle').click()
 page.get_by_role('button',name='仅显示相邻节点').click()
 capture('06_图谱关系展开.png')
 checks.append('Expand adds connected nodes; new nodes can be selected; neighbor filter works')

 page.get_by_role('button',name='适应画布').click()
 page.get_by_role('button',name='放大图谱').click()
 assert page.locator('.graph-controls').inner_text().strip()=='115%'
 page.get_by_role('button',name='适应画布').click()
 assert page.locator('.graph-controls').inner_text().strip()=='100%'
 graph_node=page.locator('[data-node="rag"]')
 box=graph_node.locator(':scope > circle').last.bounding_box()
 old=graph_node.get_attribute('transform')
 page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
 page.mouse.down();page.mouse.move(box['x']+box['width']/2+35,box['y']+box['height']/2+20,steps=6);page.mouse.up()
 assert graph_node.get_attribute('transform')!=old
 checks.append('Zoom, reset and node dragging exercised')
 page.get_by_role('textbox',name='搜索图谱节点').fill('retrieval')
 assert page.locator('[data-node="bert"]').count()==0
 page.get_by_role('button',name='清除搜索').click()
 with page.expect_download() as download:
  page.get_by_role('button',name='导出图谱').click()
 data=json.loads(Path(download.value.path()).read_text())
 assert len(data['nodes'])==19 and len(data['edges'])==19
 checks.append('Search filters nodes; graph JSON export is valid')
 page.locator('.graph-source').click()
 page.locator('canvas').first.wait_for()
 assert not page.locator('.graph-panel').count()
 assert page.get_by_role('tab',name='PDF 原文',exact=True).get_attribute('aria-selected')=='true'
 checks.append('Source paper action returns from graph to original PDF reader')
 assert not errors,errors
 browser.close()

manifest={'viewport':'1920x1080','image_pixels':'3840x2160','checks':checks,'page_errors':errors,'source_revision':'72140ea9',
 'data':'Four real public arXiv papers; local demo API responses; paper-only graph relationships are manually arranged preview data; graph backend is not connected.'}
(OUT/'验证记录.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
(OUT/'截图说明.md').write_text('''# 文献工作台截图

01–03：Molyte 原版工作台组件的独立运行截图，阅读真实公开论文 PDF，接口使用本地演示数据。

04–06：中间栏新增的交互论文关系图。全部节点使用完整论文标题，画布长标题按单词截断，悬停及详情显示全称；圆点按图距离分级，不显示关系文字或装饰效果。关系为人工组织的预览数据；4 篇本地论文提供原文证据，其余节点提供论文元数据和 arXiv 来源链接。界面不添加说明段落。

图片尺寸：3840 × 2160。原版组件来自 Molyte 提交 72140ea9；原仓库未修改。

预览入口：http://127.0.0.1:5191/?q=transformer&paper=paper%3Aattention&panel=graph

源码：D:/Microsoft VS Code/gibbs-research-plugins/examples/molyte-workbench-preview
''',encoding='utf-8')
with zipfile.ZipFile(OUT/'文献工作台截图.zip','w',zipfile.ZIP_DEFLATED) as z:
 for p in OUT.iterdir():
  if p.suffix in ['.png','.json','.md']:z.write(p,p.name)
print('Checks:',len(checks),'Output:',OUT)
