# 本地开发与 Agent 接入指南

本文面向需要开发、调试或接入 Literature KB 的同学。目标是让你在本地完整启动服务，并让任意能发 HTTP 请求的 Agent 安全地调用文献库。

> 接口字段、枚举和校验以运行中的 `GET /openapi.json` 为准；接口语义详见 [API 文档](api.md)。

## 1. 架构与调用边界

```mermaid
flowchart LR
    A["Agent / 调用方"] -->|"HTTPS + Bearer token"| B["Literature KB API"]
    B --> C[("PostgreSQL + pgvector")]
    B --> D[("Redis")]
    B --> E["storage/ 文件目录"]
    F["RQ Worker"] --> C
    F --> D
    F --> E
F --> G["MinerU / 模型服务"]
```

- Agent 只需访问 API；不应直接访问 PostgreSQL、Redis 或 `storage/`。
- 论文解析、证据构建、向量化和引用工作流是异步任务，必须同时启动 API 和 worker。
- 前端不是 Agent 接入的必需组件。

## 2. 本地开发前提

- Python 3.12 或更高版本。
- Docker Desktop（用于 PostgreSQL 和 Redis）。
- 可访问的 MinerU 与模型服务；运行时会校验所有启用的模型角色配置。

项目根目录为仓库根目录，后文记作 `$RepoRoot`；PowerShell 示例默认在 Windows 下执行。

## 3. 首次本地启动

### 3.1 启动基础依赖

在仓库根目录执行：

```powershell
docker compose up -d postgres redis
```

这会启动 PostgreSQL（含 pgvector）和 Redis。文献结构、元数据、参考文献与图表锚点均由 MinerU 原生产物增强；API 和 worker 仍需要 PostgreSQL 与 Redis。

### 3.2 创建 Python 环境并安装后端依赖

```powershell
Set-Location backend
uv venv
uv sync --extra dev
```

后续命令使用 `uv run`，无需手工激活虚拟环境。

### 3.3 配置环境变量

复制示例配置：

```powershell
Copy-Item .env.example .env
```

至少检查并填写下列配置：

```dotenv
# 本地也建议开启，避免开发时遗漏鉴权逻辑。
API_AUTH_TOKEN=replace-with-a-long-local-secret
ADMIN_API_TOKEN=replace-with-a-different-admin-secret
ADMIN_SECRETS_MASTER_KEY=replace-with-a-valid-fernet-key

DATABASE_URL=postgresql+psycopg://literature:literature@localhost:5432/literature_kb
REDIS_URL=redis://localhost:6379/0
STORAGE_ROOT=../storage

# 外部开放论文发现；OpenAlex key 建议配置，Unpaywall 要求联系邮箱。
DISCOVERY_CONTACT_EMAIL=research@example.org
OPENALEX_API_KEY=
UNPAYWALL_EMAIL=research@example.org
DISCOVERY_DOWNLOAD_ROOT=../storage/downloads
DISCOVERY_ALLOW_FAKE_IP_DNS=false

# 仅外部论文下载暂存可选使用 COS；真实密钥只放本机 .env 或部署密钥系统。
DISCOVERY_STORAGE_PROVIDER=local
COS_BUCKET=gibbs-papers-1234567890
COS_REGION=ap-shanghai
COS_ENDPOINT=gibbs-papers-1234567890.cos.ap-shanghai.myqcloud.com
COS_SECRET_ID=
COS_SECRET_KEY=
COS_PREFIX=literature-kb

# 使用 MinerU 解析 PDF 时需要。
MINERU_API_TOKEN=...
```

不要提交 `.env`，也不要把 token 或上游模型密钥写进 Agent 的 prompt、日志或源码。
模型角色及其 key 不再从 `*_API_KEY` 环境变量读取；启动并迁移数据库后，通过第 9 节的
Provider Channels 配置。`EMBEDDING_DIMENSION` 目前必须保持为 `1024`。

### 3.4 初始化或升级数据库

```powershell
uv run alembic upgrade head
```

新环境必须先执行迁移。已有开发数据时，这条命令只会向前升级；不要使用重置脚本处理需要保留的数据。

### 3.5 分别启动 API 与 worker

打开 PowerShell 窗口，均进入 `backend` 并激活虚拟环境。API、交互、解析、维护分别使用独立进程；远程下载启动两个 worker：

窗口 A：

```powershell
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

```powershell
uv run python -m app.worker --role interactive
uv run python -m app.worker --role ingest
uv run python -m app.worker --role maintenance
uv run python -m app.worker --role remote
uv run python -m app.worker --role remote
uv run python -m app.worker --role parse
uv run python -m app.worker --role content
uv run python -m app.worker --role embedding
uv run python -m app.worker --role index
```

以上每条 worker 命令需要在独立窗口运行。Windows 上 worker 使用一次只处理一个 job 的
`SimpleWorker`，所以两个 remote 进程才能并行下载两篇论文。Pipeline 开启后，MinerU
解析、Evidence/资源处理、Embedding API 和本地索引由后四个角色形成有界流水线。

## 4. 启动验证与接口发现

设定本地地址和鉴权头：

```powershell
$BaseUrl = "http://127.0.0.1:8000"
$Token = "replace-with-a-long-local-secret"
$Headers = @{ Authorization = "Bearer $Token" }
```

先验证进程存活（该接口不鉴权）：

```powershell
Invoke-RestMethod "$BaseUrl/health"
```

再检查业务依赖是否就绪：

```powershell
Invoke-RestMethod "$BaseUrl/api/v1/system/health" -Headers $Headers
```

Agent 或调用程序首次接入应读取运行时契约：

```powershell
Invoke-RestMethod "$BaseUrl/openapi.json" | ConvertTo-Json -Depth 20
```

浏览器可打开 `$BaseUrl/docs` 查看 Swagger UI。`/openapi.json` 和 `/docs` 是匿名入口，但所有 `/api/v1/*` 业务路由都需要 Bearer token（当 `API_AUTH_TOKEN` 已设置时）。

## 5. Agent 最小调用链

### 5.1 发现外部 OA 论文并暂存 PDF

外部发现不会创建本地 `Paper`。先搜索和解析下载候选，再暂存一个技术上可直接获取的 PDF：

```powershell
$discoveryBody = @{
  query = "retrieval augmented generation"
  year_from = 2023
  year_to = 2026
  open_access_only = $true
  publication_types = @("article")
  page_size = 10
  cursor = "*"
} | ConvertTo-Json -Depth 6

$external = Invoke-RestMethod `
  -Uri "$BaseUrl/api/v1/discovery/search" `
  -Method Post -Headers $Headers -ContentType "application/json" -Body $discoveryBody

$openAlexId = $external.items[0].external_id
$resolved = Invoke-RestMethod `
  -Uri "$BaseUrl/api/v1/discovery/works/resolve" `
  -Method Post -Headers $Headers -ContentType "application/json" `
  -Body (@{ openalex_id = $openAlexId } | ConvertTo-Json)

$staged = Invoke-RestMethod `
  -Uri "$BaseUrl/api/v1/discovery/downloads" `
  -Method Post -Headers $Headers -ContentType "application/json" `
  -Body (@{ openalex_id = $openAlexId } | ConvertTo-Json)
```

本地模式下文件写入 `storage/downloads/{sha256}.pdf`，同名 JSON 保存来源和许可证；它们不会
自动入库。设置 `DISCOVERY_STORAGE_PROVIDER=cos` 后，文件改为写入
`{COS_PREFIX}/downloads/{sha256}.{pdf,json}`，但不会迁移现有 Paper 或 MinerU 资产。下载接口
不接受任意 URL，只能按 OpenAlex ID 或 DOI 由服务端重新解析候选。

Clash 等代理把公网域名解析到 `198.18.0.0/15` 时，确认是受控本机 Fake-IP 后可设置
`DISCOVERY_ALLOW_FAKE_IP_DNS=true`。该兼容项生产环境默认保持关闭。

### 5.2 检索、核验并回源

搜索只返回候选 evidence，不能直接作为最终引用依据。必须继续读取正文上下文和 source。

```powershell
$searchBody = @{
  query = "evidence that retrieval improves factual accuracy"
  scope = @{ paper_ids = @() }
  filters = @{ modality = @("text") }
  top_k = 20
  page = 1
  page_size = 5
} | ConvertTo-Json -Depth 8

$search = Invoke-RestMethod `
  -Uri "$BaseUrl/api/v1/evidence/search" `
  -Method Post `
  -Headers $Headers `
  -ContentType "application/json" `
  -Body $searchBody

$evidenceId = $search.items[0].evidence_id

$content = Invoke-RestMethod `
  -Uri "$BaseUrl/api/v1/evidence/$evidenceId/content" `
  -Headers $Headers

$source = Invoke-RestMethod `
  -Uri "$BaseUrl/api/v1/evidence/$evidenceId/source" `
  -Headers $Headers
```

使用 `$content.content.text` 核验语义，使用 `$source.citation` 中的论文信息、原文和页码/bbox 生成可追溯引用。若返回 `pdf_asset_id`，可继续调用 `GET /api/v1/assets/{asset_id}/download` 下载原始资源。

### 5.3 限定论文范围

当 Agent 已知论文 ID 时，应该用 `scope.paper_ids` 缩小检索范围，避免全库检索：

```json
{
  "query": "What limitations do the authors report?",
  "scope": { "paper_ids": ["paper_abc"] },
  "filters": { "section_type": ["limitations", "discussion"] },
  "top_k": 20,
  "page": 1,
  "page_size": 10
}
```

不确定可用筛选字段时，先调用 `GET /api/v1/papers/meta-catalog`，不要猜测字段或枚举值。

### 5.4 异步工作流：Evidence Pack 与 Citation Grounding

Evidence Pack 接受明确的 `claims[]`；Citation Grounding 接受一整段 `draft`。两者都会先返回 `202` 和 `run_id`，需轮询 workflow：

```powershell
$packBody = @{
  claims = @(
    @{ claim_id = "c1"; text = "Retrieval improves factual accuracy." }
  )
  scope = @{ paper_ids = @("paper_abc") }
  top_k_per_claim = 5
} | ConvertTo-Json -Depth 8

$submission = Invoke-RestMethod `
  -Uri "$BaseUrl/api/v1/evidence/packs" `
  -Method Post `
  -Headers $Headers `
  -ContentType "application/json" `
  -Body $packBody

do {
  Start-Sleep -Seconds 2
  $run = Invoke-RestMethod `
    -Uri "$BaseUrl/api/v1/workflows/$($submission.run_id)" `
    -Headers $Headers
} while ($run.status -in @("queued", "running"))

$run
```

- 论文处理任务使用 `task_id`，查询 `GET /api/v1/tasks/{task_id}`。
- Evidence Pack / Citation Grounding 使用 `run_id`，查询 `GET /api/v1/workflows/{run_id}`。
- 不要混用两类 ID；失败时记录响应头或错误体中的 `request_id`，方便排查。

## 6. Agent 实现约定

Agent 应遵循以下规则：

1. 启动时检查 `/api/v1/system/health`；依赖未就绪时不要无限重试。
2. 首次接入或版本升级后读取 `/openapi.json`；不要把本文中的示例当作不可变 schema。
3. 将 Base URL 和 token 配置为部署环境变量，例如 `LITERATURE_KB_BASE_URL`、`LITERATURE_KB_TOKEN`。
4. 每个请求带 `Authorization: Bearer ...`；可选地发送唯一的 `X-Request-ID`，用于跨服务排障。
5. `discovery/search` 返回外部候选，不代表已下载或已进入本地库；`papers/search` 只搜索本地库。
6. `evidence/search` 的命中只是候选。要在最终回答中引用时，必须读取 `/content`，必要时再读取 `/source`。
7. 使用 `scope.paper_ids` 或 `scope.evidence_ids` 约束范围；除非任务明确要求，不进行无边界的全库检索。
8. 对 `503` 使用指数退避并限制重试次数；对 `400` 修正请求，对 `401` 刷新凭据，对 `404` 停止使用该 ID，对 `409` 等待前置任务完成。

以下是可直接放入 Agent 工具说明的简版约定：

```text
Literature KB 的 Base URL 和 Bearer token 由环境变量提供。
先读取 /openapi.json 确认运行时契约。检索时先调用 POST /api/v1/evidence/search；
每个用于结论或引用的 evidence_id 都必须再调用 GET /content，必要时调用 GET /source。
异步论文处理轮询 /api/v1/tasks/{task_id}；引用工作流轮询 /api/v1/workflows/{run_id}。
```

## 7. 接入远程环境

部署到服务器后，外部 Agent 的调用方式与本地完全相同，只需将：

```powershell
$BaseUrl = "https://kb.example.com"
$Token = $env:LITERATURE_KB_TOKEN
```

替换为真实地址和由密钥系统注入的 token。

生产环境必须：

- 仅向公网暴露经反向代理保护的 HTTPS API；不要暴露 PostgreSQL、Redis 端口。
- 将 API、worker、PostgreSQL 数据卷和 `storage/` 作为同一套持久化服务部署。
- 在反向代理或网关配置 TLS、请求大小上限、速率限制和访问日志。
- 将 `API_AUTH_TOKEN`、MinerU token 和 `ADMIN_SECRETS_MASTER_KEY` 放入服务器密钥管理；
  模型 key 通过加密的 Provider Channels 管理。
- 限制 `/openapi.json` 与 `/docs` 的访问范围（例如仅内网、VPN 或网关鉴权），若接口定义本身不应公开。

当前服务提供 REST/OpenAPI 接口，并未提供 MCP endpoint。如需在支持 MCP 的客户端中自动发现工具，可额外部署一个 MCP 包装层：该层保存 Bearer token，并把 MCP tool 调用转发到本 API；不要让 MCP 客户端直接获得长期生产 token。

## 8. 常见问题

| 现象 | 优先检查 |
| --- | --- |
| system health 报模型未配置 | 在 Admin 的 Provider Channels 中为必需角色配置渠道和加密 key。 |
| workflow/task 一直是 `queued` | worker 是否已启动，Redis 是否可连接。 |
| evidence search 返回 `503` | embedding 服务、向量库和对应论文 `embedding_status` 是否就绪。 |
| discovery 下载拒绝公网域名 | 检查是否使用 Clash Fake-IP；仅在受控本机环境启用 `DISCOVERY_ALLOW_FAKE_IP_DNS`。 |
| 返回 `409 CONTENT_NOT_READY` | 论文尚未完成 parse / build evidence / embed；轮询相应 task 后重试。 |
| 返回 `401` | 确认请求含 `Authorization: Bearer <API_AUTH_TOKEN>`，且 token 未被引号或换行污染。 |
| Agent 无法访问本机服务 | `127.0.0.1` 只对本机有效；远程 Agent 必须使用可达的 HTTPS 域名或受控网络通道。 |

## 9. Provider Channels 与 API key

1. 打开 `http://127.0.0.1:5174`，进入左侧 **Admin**。
2. 输入 `.env` 中的 `ADMIN_API_TOKEN` 并点击“连接 / 刷新”。
3. 进入 **Channels**，点击“添加渠道”。
4. 一次填写 Provider、Base URL、需要的 role/model，并逐行粘贴 key；支持 `别名,key` 格式。
5. 创建后可在同一渠道中测试、禁用或补充 key。同一个渠道的 key 池可服务多个 role。

数据库 key 依赖 `ADMIN_SECRETS_MASTER_KEY` 加密保存；未配置时前端会禁用添加按钮。rerank 仅作为可选精排层，失败、超时或未配置时搜索自动返回原 RRF 结果，并在 `search_info` 中说明降级原因。

## 10. 隔离的本地基础设施测试

在 `backend` 目录执行：

```powershell
.\scripts\run_local_integration_tests.ps1
```

脚本使用独立的 `postgres-test:15433` 和 `redis-test:16380`，验证真实 PostgreSQL、Redis、RQ、scheduled job 及容器 stop/start 恢复。测试结束后自动删除测试容器和临时数据，不连接开发文献库。

## 11. 本地运行 OpenAlex 批量采集

先在 `backend/.env` 设置：

```dotenv
HARVEST_ENABLED=true
PIPELINE_ENABLED=true
HARVEST_EXPAND_BUFFER_ITEMS=500
HARVEST_CLI_ENABLED=true
HARVEST_CLI_WORKERS=20
```

执行迁移并分别启动维护与采集 Worker：

```powershell
Set-Location backend
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m app.worker --role maintenance
.\.venv\Scripts\python.exe -m app.worker --role harvest
```

打开前端管理控制台的“OpenAlex 采集”，输入 Admin Token 后添加团队成员授权的 API Key。
普通成员从左侧“批量采集”检索分类、预估并创建任务。若所有 Key 的每日免费余额耗尽，
任务进入 `waiting_quota`，维护 Worker 会在 `/rate-limit` 返回的 UTC 重置时间后恢复。

从分类选择、PDF 下载到批量解析入库的完整前端和 PowerShell 示例见
[`openalex-batch-harvest-guide.md`](openalex-batch-harvest-guide.md)。

本地存储默认位于：

```text
{DISCOVERY_DOWNLOAD_ROOT}/corpus/openalex/{work_id}/paper.pdf
{DISCOVERY_DOWNLOAD_ROOT}/corpus/openalex/{work_id}/metadata.json
```

使用腾讯 COS 时对象键会加 `{COS_PREFIX}/`，数据库中的 storage key 仍保持
`corpus/openalex/...`。`openalex-official` 已固定为后端依赖；CLI 不可用或返回异常时自动回退
到经过相同技术校验的 Content HTTP 下载器。
