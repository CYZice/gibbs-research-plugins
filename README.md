# Gibbs Research Plugins

**给 Agent 使用的文献证据库、科研绘图和带引用报告。三个模块完整开源，组合成一条可修改、可复现的科研交付流程。**

```text
论文导入 → 检索 → 阅读原文与核验 → 数据绘图 → 预览反馈与重画 → 带引用报告
```

文献库沿用现有 Literature KB；绘图与报告复用 Gibbs 原有功能。报告直接取用文献上下文、原文位置与绘图产物，不依赖商业平台的 Harness、账号或计算任务数据库。

## 能做什么

- **文献证据**：论文上传与开放资源发现/导入、关键词与向量检索、正文窗口阅读、证据上下文与 PDF 页码/位置、图表资源、参考关系、论断核验、草稿引用及异步状态。完整服务与管理前端位于 `services/literature-kb`。
- **科研绘图**：折线、柱形、散点、累积面积、RDF、Arrhenius 和 Nyquist 数据图、饼图；保留 JSON 数据/规格，调整字号、图例、尺寸及配色，导出 PNG/SVG/PDF。Arrhenius 入口绘制传入坐标及可选拟合，不自动推导物理拟合或单位变换。
- **研究报告**：从研究目标、文献证据、用户结果和图表组织八节草稿，核对引用/图形标识，插入指定版本图表，输出 HTML/Markdown/JSON/PDF。由当前 Agent 写正文，或配置自己的文本模型。
- **预览与修改**：本地预览、上一版对比、历史切换、反馈队列、重画与修订冲突检测。报告保存证据和图表快照，旧报告不随新图悄悄变化。

引用标识检查负责发现未知来源，论断是否受原文支持由阅读上下文与文献核验工作流负责。模型输出保留数值检查提示，用户在预览中核对。

## 先跑通绘图和报告

Python 3.12+，Node.js 20.19+。在本仓库目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\gibbs-research.exe
```

打开 <http://127.0.0.1:8766>。另开终端运行：

```powershell
.\.venv\Scripts\python.exe examples/demo.py
```

示例使用明确标注的合成数据，生成可修改图表和插图报告，不需要模型密钥或文献后台。服务默认只监听本机，运行产物存入 `.runtime`。Linux/macOS 对应可执行文件为 `.venv/bin/python`、`.venv/bin/gibbs-research`。

已有 Edge 时可用 `$env:GIBBS_BROWSER_CHANNEL='msedge'`，已有 Chrome 时填 `chrome`，替代 Chromium 下载。PDF 导出采用浏览器打印，不要求本机安装 WeasyPrint。

## 连接现有文献库

在启动工具服务的终端设置：

```powershell
$env:GIBBS_LITERATURE_URL='http://127.0.0.1:8000'
$env:GIBBS_LITERATURE_TOKEN='<文献库 API_AUTH_TOKEN>'
$env:GIBBS_TOOLS_TOKEN='<为工具服务设置的独立访问令牌>'
.\.venv\Scripts\gibbs-research.exe
```

首次自行部署文献库，启动 Docker Desktop 后运行：

```powershell
python scripts/init_literature_env.py
docker compose --env-file services/literature-kb/.env -f services/literature-kb/docker-compose.prod.yml up -d --build
```

生成的 `.env` 含随机数据库密码、用户/API/管理令牌和加密主密钥，不会被 Git 跟踪。文献 API 位于本机 8000 端口，管理前端位于 <http://127.0.0.1:3000>。首次在 Admin 页面配置解析及 embedding/文本/视觉/重排序服务；模型凭据保存在文献库管理配置中，维度按当前数据库要求为 1024。

现有文献库的 [完整 API](services/literature-kb/docs/api.md)、[本地开发与 Agent 接入](services/literature-kb/docs/local-development-agent-guide.md)及[部署说明](services/literature-kb/docs/deployment.md)一并开源。文献服务需要 PostgreSQL/pgvector、Redis、worker 和存储，插件不会替它创建第二份数据库。

## 安装 DSH 插件

支持 DSH `0.2.2-rc.2`，工具 API 以该版本验证。安装 DSH 时选择该候选版（`npm install -g @deepseek-ai/dsh@0.2.2-rc.2`），不要使用依赖链仍含已知漏洞的 `rc.1`。Python 服务启动后，在运行 DSH 的终端设置相同的 `GIBBS_TOOLS_TOKEN`：

```powershell
dsh plugin --profile gibbs add github:CYZice/gibbs-research-plugins
dsh --profile gibbs --dump-config
dsh --profile gibbs
```

也可以安装 GitHub Release 附带的 `dsh-gibbs-research-0.1.0.tgz`。插件使用直接可运行的 JavaScript，无安装时构建脚本。它不会安装或启动 Python/文献服务，也不提升会话权限。

一个 bundle 注册三个独立模块，提供 16 个工具；需要单独加载时，入口为 `dsh-gibbs-research/literature`、`/plot`、`/report`。全套建议一起安装，报告可直接复用文献和绘图功能。

试着对 Agent 说：

> 从文献库检索这个研究问题，先阅读支持结论的原文。把我的数据画成图，给我预览；确认后生成带原文出处和图表的报告。

工具返回 `preview_url` 和下载地址。预览页“交给 Agent 修改”将意见保存在队列；回到会话后让 Agent 读取 `gibbs_feedback_pending` 并生成新版本。空闲 Agent 不会被预览页自动唤醒。

预览界面采用 DSH 的白底/冷灰与深色主题配色，无顶部品牌 banner。重画、提交意见、对比和导出使用图标，悬停可查看名称；JSON 编辑器按需展开。报告与文献库使用同一套视觉样式。

文献工作台沿用 Molyte 的三栏布局：左侧论文库、中间原文阅读、右侧证据与核验。点击证据可切换到对应论文的正文或 PDF 页码并高亮原文位置；支持 PDF 翻页、缩放与适宽，手机端切换分栏阅读。

`skills/gibbs-research/SKILL.md` 提供跨客户端可阅读的完整使用流程，可放入客户端自己的技能目录。DSH 原生工具本身已包含输入契约和调用说明，打包技能文件不会自动修改用户的技能目录。

## 配置自己的模型与文件导入

报告可以完全使用当前 Agent 的模型，无需第二份密钥。需要服务端直接写作时，显式配置兼容 Chat Completions 的接口：

```powershell
$env:GIBBS_MODEL_BASE_URL='https://your-model-service.example/v1'
$env:GIBBS_MODEL='<文本模型名称>'
$env:GIBBS_MODEL_API_KEY='<模型密钥>'
```

让 Agent 调用 `gibbs_report_save` 时省略 `draft` 即可。配置错误、网络错误和输出契约错误会返回失败，不静默切换成模板文章。

上传论文前，在 DSH 进程设置 `$env:GIBBS_IMPORT_ROOT='D:\MyPapers'`。插件只读取这个显式目录内的 PDF，路径按实际文件解析，大小上限 50MB。文献库导入与核验任务提交后，用 `gibbs_literature_status` 续接 `tasks`、`workflows` 或 `import-batches`。

工具配置可通过环境变量 `GIBBS_TOOLS_URL`、`GIBBS_TOOLS_TOKEN`，或每个插件行的 `config.serviceUrl`、`config.timeoutMs` 指定。远端工具服务要求 HTTPS，用户负责部署认证反向代理。完整变量见 `.env.example`；该文件是配置参考，服务读取进程环境变量，不自动加载它。

## 导出与保存

HTML 包含内嵌图形，可离线交付；JSON 保留研究材料、草稿、原文证据与图表版本。Markdown 的图形链接指向服务中的固定版本，离线交付选 HTML 或 PDF。

报告修订需保留原始 `[[cite:ev_N]]` 和 `[[fig:ID]]` 标记，不能把已经排版的 `[1]` 当作新草稿提交。预览编辑器和 `source_draft` 保留原始标记。修改传入 `base_revision`，冲突时读取最新版本再继续。

## 开发与验证

```powershell
npm ci --ignore-scripts
npm test
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m pytest tests
```

启用真实 PDF 测试设置 `GIBBS_TEST_PDF=1`；启用隔离工作区的浏览器交互测试设置 `GIBBS_TEST_UI=1`，均需安装浏览器。文献库独立安装 `services/literature-kb/backend` 的依赖，运行该目录下 `pytest -m 'not integration'`；真实数据库/队列测试另见原接入指南。

文献工作台浏览器测试：先在 `services/literature-kb/frontend` 运行 `npm ci`，再设置 `GIBBS_TEST_LIBRARY_UI=1` 并运行 `pytest tests/test_literature_workbench_ui.py`。测试启动独立 Vite 服务，使用合成 API 数据与实际 PDF 渲染，覆盖三栏联动、翻页缩放、核验、上传导入及窄屏切换；不连接用户数据库。

架构与外部项目取舍见 [复用决策](docs/REUSE_DECISIONS.md)，源码来源见 [SOURCE_ORIGIN](docs/SOURCE_ORIGIN.md)。
比赛支撑材料清单与 DSH 插件补充说明见 [competition-support](docs/competition-support/00_支撑材料清单.md)；其中包含功能证据、截图索引和验证记录。

## 许可证

MIT。文献、报告与绘图代码均可自行部署和修改；仓库不包含商业 Harness、用户论文、运行数据库、服务凭据或模型权重。外部解析服务和模型按各自许可配置使用。
