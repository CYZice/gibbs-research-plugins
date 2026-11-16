# Molyte 文献工作台

## 界面来源

仓库：`Gonghysin/molyte_web_v2.0_resnet`。

本地目录：`D:/Microsoft VS Code/molyte_web_v2.0_resnet`。

原独立页面：`/workspace/literature`。`72140ea9` 版本将该路由直接连接到 `LiteratureWorkspace`。当前已提交版本改为跳转智能研发，独立工作台组件仍保留；本地工作区已有恢复该入口的未提交改动。

三栏分别显示检索结果、论文 PDF 和原文证据。支撑材料采用原作品说明 PDF 第 12 页“图 4-3 文献工作台”的内嵌原图，没有重绘或修改界面。本次未重新运行后端或生成新的在线使用记录。

## 对应源码

以下路径均相对于 Molyte 仓库根目录。

| 内容 | 文件 |
|---|---|
| 页面入口 | `frontend/src/pages/LiteratureWorkspace.tsx` |
| 三栏工作台及交互 | `frontend/src/features/literature/LiteratureWorkbench.tsx` |
| 左侧检索栏 | `frontend/src/features/literature/LiteratureLibraryPane.tsx` |
| 中间阅读栏 | `frontend/src/features/literature/LiteratureDocumentPane.tsx` |
| 右侧证据栏 | `frontend/src/features/literature/LiteratureEvidencePane.tsx` |
| 顶部检索、上传与服务状态 | `frontend/src/features/literature/LiteratureWorkbenchHeader.tsx` |
| PDF 渲染、页码、缩放与证据定位 | `frontend/src/pages/literature/LiteraturePdfReader.tsx` |
| 布局与样式 | `frontend/src/pages/LiteratureWorkspace.css` |
| 检索、正文、证据和 PDF 接口 | `frontend/src/api/literatureWorkspace.ts` |
| 收藏与文献集接口 | `frontend/src/api/literatureLibrary.ts` |
| 路由注册 | `frontend/src/router.tsx` |
| 后端文献接口 | `backend/app/api/v1/literature.py` |

工作台在 `72140ea9` 提交中提取为可复用组件；页面支持独立工作台与智能研发内嵌两种展示模式。配套源码快照取自 `72140ea9`，包含直接打开工作台的原路由。当前工作区未提交的改动保持不动。

## 原图

`screenshots/molyte-literature-workbench-original.jpeg`（1090 × 557），直接提取自原作品说明 PDF 第 12 页。
