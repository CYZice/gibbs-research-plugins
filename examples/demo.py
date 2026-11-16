"""Create plots and a report from explicitly synthetic sample measurements."""
import os

import httpx

base = os.environ.get("GIBBS_TOOLS_URL", "http://127.0.0.1:8766")
token = os.environ.get("GIBBS_TOOLS_TOKEN", "")
client = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=120)


def post(path, payload):
    response = client.post(path, json=payload)
    response.raise_for_status()
    return response.json()


spec = {"type": "line", "title": "示例数据：样品响应对比", "xLabel": "采样时间", "xUnit": "s", "yLabel": "响应", "series": [{"name": "样品 A", "data": [[0, 1.5], [1, 2.5], [2, 3.5], [3, 4.5]]}, {"name": "样品 B", "data": [[0, 1.0], [1, 2.0], [2, 2.8], [3, 3.6]]}], "style": {"font_size": 13, "legend_location": "upper left"}}
plot = post('/api/plots', {"spec": spec, "caption": "功能演示用合成数据，不作为实验结果。", "provenance": {"source": "synthetic demonstration", "units": "arbitrary response"}})
material = {"title": "样品响应比较 · 功能演示", "goal": "演示图表与报告的联动", "plots": [{"artifact_id": plot['artifact_id'], "revision": plot['revision']}], "results": [{"label": "功能演示样品", "method": "合成数据输入", "metrics": [{"name": "response_a", "label": "样品 A 最终响应", "value": 4.5, "unit": "a.u."}, {"name": "response_b", "label": "样品 B 最终响应", "value": 3.6, "unit": "a.u."}], "conditions": ["用于软件功能演示的合成数据"]}]}
prepared = post('/api/reports/prepare', material)
figure_id = prepared['writer_input']['evidence_digest'][0]['figures'][0]['figure_id']
draft = {"abstract": "本报告使用合成样品数据，演示从科研绘图到报告生成的联动。", "background": "用户提交数据后，Agent 生成图表，并将指定版本插入报告。", "methods": [{"title": "数据与图形规格", "prose": "数据来自示例脚本。图形规格保留数值、单位、标题和系列名称，可在预览中调整字号及图例。"}], "results_by_system": [{"title": "样品响应", "prose": f"示例中样品 A 最终响应为 4.5 a.u.，样品 B 为 3.6 a.u.。[[fig:{figure_id}]]"}], "comparison": "该图用于演示两组数据在同一坐标系下的比较。", "discussion": "修改图表会创建新版本；本报告继续使用保存时指定的版本。", "conclusions": ["文献证据、图表与报告通过明确标识连接，保留来源及版本。"], "recommendations": ["在预览页提交修改意见，再让 Agent 读取反馈并重画。"]}
report = post('/api/reports', {"material": material, "draft": draft})
print('Plot:', base + plot['preview_path'])
print('Report:', base + report['preview_path'])
client.close()
