# Molyte 文献工作台独立预览

直接提取 Molyte `72140ea9` 的三栏工作台组件，在独立入口运行。原仓库不改动，新增知识图谱只存在于本预览目录。

## 运行

依赖复用 `D:/Microsoft VS Code/molyte_web_v2.0_resnet/frontend/node_modules`。

在本仓库根目录执行：

```powershell
python examples/molyte-workbench-preview/prepare.py
node "D:/Microsoft VS Code/molyte_web_v2.0_resnet/frontend/node_modules/vite/bin/vite.js" --config examples/molyte-workbench-preview/vite.config.mjs
```

打开 `http://127.0.0.1:5191/?q=transformer&paper=paper%3Aattention&panel=graph`。

截图与交互验证：`python examples/molyte-workbench-preview/capture.py`。

## 数据来源与实现

- PDF：arXiv 1706.03762、1810.04805、2005.11401、2106.09685。
- 证据：`prepare.py` 从真实 PDF 正文提取，保留页码和坐标。
- 文献 API：本地演示响应，不连接生产账户、知识库或写入真实数据。
- 图谱：人工组织的 mock 节点与关系，具备选择、拖动、平移、缩放、搜索、关系展开、导出和回到 PDF 的交互。尚未接入自动建图后端。
- 页面保留原版布局与配色，不在界面中添加 mock 标签或说明段落。

`original/` 为原组件快照；其中服务状态文字改为“本地文献库”，避免把本地演示状态写成在线服务证明。`src/` 为独立挂载入口和新增图谱，不改变原工作台三栏结构。

图谱采用纯论文节点，所有节点保存完整论文标题，画布长标题按单词截断为“...”，悬停和详情显示全称。以 Attention Is All You Need 为根节点，按图距离划分层级，圆点半径依次为 25、17、10、6。不显示关系文字、节点图标、光圈、虚线、阴影或背景网点。
