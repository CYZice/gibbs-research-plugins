# 复用自己的功能，吸收外部交互设计

2026-11-13。文献库、报告生成和绘图的实现及插件接口采用 MIT 开源；商业 Harness 不在本仓库。这里记录会影响后续开发的取舍，实际发布能力以 README 和代码为准。

## 保留的实现

- 文献库：直接迁入原有完整后端、迁移、测试、前端与运维脚本，不另建第二套检索服务。
- 绘图：复用原 `report_engine/figures.py`，补齐 SVG/PDF 输出、字号/图例/配色/尺寸修改、多系列散点和 Nyquist、真正累积的面积图及输入校验。
- 报告：复用原证据数据类、摘要组织、引用编号、正文与图形白名单校验，以及 HTML 图表/表格排版。独立材料入口取代商业任务数据库；由调用 Agent 写正文，或使用显式配置的文本模型。无模型配置时返回配置错误，不伪装成模型生成。
- 公共输入是研究目标、已阅读证据、用户结果和固定版本图表；不导入商业账号、计费或研究调度服务。

## 外部仓库的选择

本轮阅读了以下官方仓库的 README、许可证元数据以及所列入口。没有运行它们的安装脚本，也没有将它们的源码复制进本仓库。

| 仓库 | 采用的设计 | 不引入的部分 |
|---|---|---|
| [figure-studio](https://github.com/lekhang4497/figure-studio)，MIT；检查 `src/figure_studio/server.py` | 本地预览、参数编辑、历史持久化、可复现导出 | 上游 pickle 图形上传与整个 artist 编辑器；首版用 JSON 规格，不接收序列化可执行对象 |
| [FigMirror](https://github.com/VILA-Lab/FigMirror)；检查 README、method 文档 | 视觉反馈保留修改清单和保持项；下一轮使用原数据重画 | 未核定可复用代码许可证，不复制代码；不引入它的绘图/审查多 Agent 调度 |
| [Pylustrator](https://github.com/rgerum/pylustrator)，GPL-3.0 | 修改后的图必须可复现，避免图片外部修饰后丢失源数据 | 不复制 GPL 实现，也不引入桌面 Qt 编辑器 |
| [Research Dossier](https://github.com/AppleBoiy/research-dossier)，MIT；检查 `scripts/dossier.py` | 明确来源集合、未知引用拒绝构建、正文引用与参考文献同源 | 不替换我们已有的正文核验与证据位置体系；文献存在性与论断支持度分别处理 |
| [OpenDraft](https://github.com/federicodeponte/opendraft)，MIT | 章节化草稿、修改入口和来源核验分工 | 不引入十九 Agent 论文写作编排；报告直接使用现有文献库与绘图模块 |
| [dartwork-mpl](https://github.com/dartworklabs/dartwork-mpl)，MIT | 物理尺寸、多格式导出、布局/重叠检查值得后续增强 | 当前库要求 Matplotlib 3.11.2+，首版不为样式层升级整个绘图库；自动重叠检查尚未实现 |
| [dsh-science-workbench](https://github.com/poplarity/dsh-science-workbench)，MIT | 产物预览、反馈重画 | 不采用全文件访问默认策略、自动初始化 Git 或自动提交用户项目 |
| [dsh-report-studio](https://github.com/ciceroyang/dsh-report-studio)，MIT | 原生 DSH bundle/工具入口 | 不使用工作日志模板替代科研报告 |

## 首版已经补齐的交互

预览页展示图表和报告，编辑 JSON 或字号/图例后保存独立修订；支持上一版对比及历史切换。用户可提交自然语言意见，Agent 通过待处理反馈工具读取并重画。提交意见不会自动唤醒空闲 Agent。

界面统一沿用 [DSH ui-theme](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-theme/src/styles/design-platform.css) 的配色值与语义 token：白底、冷灰侧栏、深色主按钮、蓝色强调；独立预览随系统切换深浅主题。绘图、报告与文献库移除顶部品牌 banner 和重复二级说明；常用操作图标化，保留悬停提示、无障碍名称和真实错误反馈。导出格式收在下载菜单，JSON 编辑按需展开，报告正文与导出排版保持原样。

文献前端采用自有 Molyte 文献工作台的三栏组织方式：左栏搜索、筛选和选择论文，中栏阅读摘要、分页正文、PDF 与图表，右栏检索证据、核验论断和校验引用。证据点击联动对应论文与原文页码，合法的归一化坐标显示高亮；PDF 支持翻页、缩放与适宽。窄屏通过分栏切换保留阅读空间。布局与交互适配现有 Literature KB API，不迁入 Molyte 的账号、路由、收藏/文献集后端或商业服务；原仓库保持不变。

报告在保存时读取文献证据的上下文与原文位置，并保存图表规格及版本快照。重画图表不会改变旧报告；修改报告时可显式绑定新图版。`base_revision` 冲突返回 409，要求重新读取当前版本，防止覆盖他人修改。

## 后续增强，独立于首版发布

- 基于实际图形边界的重叠、裁切与空坐标轴检查。
- 更丰富的图形类型及参数编辑器；JSON 规格仍是可复现来源。
- 导出带全部图形文件的 Markdown 包；当前独立离线交付选 HTML，Markdown 图片链接指向服务的固定版本下载。
- 在真实自托管 PostgreSQL/Redis 与用户配置的解析/模型服务上执行完整新论文入库演示。

MinerU 通过可配置外部解析服务调用；本仓库不分发它的代码、权重或商业论文语料。第三方模型和服务按各自许可使用。
