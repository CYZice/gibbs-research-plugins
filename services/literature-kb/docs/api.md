# Literature KB API 文档

本文是面向内部 agent 开发同学的**人类可读接入手册**。它说明：

- 稳定 API 有哪些
- 每个接口何时调用
- 输入输出有哪些关键字段
- 异步 `task` / `workflow` 应该怎么轮询
- 常见错误怎么恢复

> 机器可读事实源始终是 `/openapi.json`。当本文与运行时契约冲突时，以 `/openapi.json` 和真实后端行为为准。

---

## 1. 总览与鉴权

### API 前缀

- 业务接口前缀：`/api/v1`
- 匿名发现入口：`GET /openapi.json`、`GET /docs`、`GET /health`

### Bearer token

当服务端配置了 `API_AUTH_TOKEN` 后，所有业务接口都要求：

```http
Authorization: Bearer <token>
```

包括：

- `GET /api/v1/system/health`
- 所有 `/api/v1/...` 业务路由

认证失败时返回 `401`：

```json
{
  "error": {
    "code": "UNAUTHORIZED",
    "message": "Missing or invalid API token.",
    "details": {},
    "request_id": "req_..."
  }
}
```

### 文档分工

- `README.md`：回答“这是什么、能做什么、agent 怎么用”。
- `docs/api.md`：回答“接口怎么调、输入输出是什么、怎么处理异步状态”。
- `/openapi.json`：回答“运行时真实 schema、校验和枚举是什么”。

---

## 2. Agent 推荐调用顺序

### 最短工作流

```text
GET /api/v1/papers/meta-catalog
  -> POST /api/v1/papers/search
  -> POST /api/v1/evidence/search
  -> GET /api/v1/evidence/{evidence_id}/content
  -> GET /api/v1/evidence/{evidence_id}/source
  -> 可选：POST /api/v1/evidence/packs
  -> 可选：POST /api/v1/citations/ground
  -> GET /api/v1/workflows/{run_id}
```

### 按场景推荐

#### 场景 A：论文发现

```text
GET /api/v1/papers/meta-catalog -> POST /api/v1/papers/search -> GET /api/v1/papers/{paper_id}
```

#### 场景 B：evidence 检索与核验

```text
POST /api/v1/evidence/search -> GET /api/v1/evidence/{evidence_id}/content -> GET /api/v1/evidence/{evidence_id}/source
```

#### 场景 C：资源回源

```text
GET /api/v1/evidence/{evidence_id}/source -> GET /api/v1/assets/{asset_id} -> GET /api/v1/assets/{asset_id}/download
```

#### 场景 D：引用生成

```text
POST /api/v1/evidence/packs -> GET /api/v1/workflows/{run_id}
POST /api/v1/citations/ground -> GET /api/v1/workflows/{run_id}
```

#### 场景 E：自己灌库与处理

```text
POST /api/v1/papers -> POST /api/v1/papers/{paper_id}/operations/{operation} -> GET /api/v1/tasks/{task_id}
```

---

## 3. 横切规则

### 3.1 ID 类型不要混用

- `paper_id`：论文 ID
- `evidence_id`：证据 ID
- `asset_id`：资源 ID
- `task_id`：paper processing task ID
- `run_id`：workflow run ID

**不要把 `task_id` 当成 `run_id`，也不要把 `run_id` 当成 `task_id`。**

### 3.2 search 与 read / verify 分工不同

- `POST /api/v1/evidence/search`：找候选 evidence
- `GET /api/v1/evidence/{evidence_id}/content`：读正文窗口
- `GET /api/v1/evidence/{evidence_id}/source`：做 PDF 回源

**search 命中后，推荐继续读 content/source，再决定是否引用。**

### 3.3 scope 的语义

`EvidenceScope` 支持：

- `paper_ids`：先把候选限制在这些论文中
- `evidence_ids`：先把候选限制在这些已知 evidence 中

注意：

- 空 `scope` 表示在本地可检索 evidence 中搜索
- 同时提供 `paper_ids` 和 `evidence_ids` 时，`evidence_ids` 必须属于这些论文，否则请求无效

示例：

```json
{
  "scope": {
    "paper_ids": ["paper_123"],
    "evidence_ids": ["ev_1", "ev_2"]
  }
}
```

### 3.4 分页语义

通用分页字段：

- `page`
- `page_size`
- `total`
- `total_pages`
- `has_next`
- `has_prev`

注意：

- `page` 是 **1-based**
- `pagination.total` 表示**当前过滤 / 候选池**的大小，不是全局语义命中数

### 3.5 filters 的语义

- 同一个 list filter 内的多个值：**OR**
- 不同 filter 字段之间：**AND**

例如：

```json
{
  "filters": {
    "language": ["en", "zh"],
    "has_assets": true
  }
}
```

表示：`language = en OR zh`，并且 `has_assets = true`。

### 3.6 `top_k` 与 `top_k_per_claim`

- `POST /api/v1/evidence/search` 使用 `top_k`
- `POST /api/v1/evidence/packs` / `POST /api/v1/citations/ground` 使用 `top_k_per_claim`

含义：

- `top_k`：过滤后候选池的最大大小
- `top_k_per_claim`：每个 claim 最多拿多少 evidence 候选

当前默认值：

- `top_k = 50`
- `page = 1`
- `page_size = 10`
- `top_k_per_claim = 5`

### 3.7 常见状态值

#### TaskResponse.status

常见值：

- `queued`
- `running`
- `succeeded`
- `failed`
- `stale`（旧任务被替代时可能出现）

#### WorkflowRunResponse.status

固定值：

- `queued`
- `running`
- `succeeded`
- `failed`

#### Workflow `details.stage`

当前常见 stage：

- `claim_extraction`
- `retrieval_and_judging`
- `citation_assembly`
- `retrying`
- `succeeded`
- `failed`

### 3.8 统一错误结构

所有 JSON 错误都使用统一结构：

```json
{
  "error": {
    "code": "INVALID_ARGUMENT",
    "message": "Request validation failed.",
    "details": {
      "errors": []
    },
    "request_id": "req_..."
  }
}
```

### 3.9 常见错误恢复建议

| HTTP | 常见含义 | Agent 建议 |
| --- | --- | --- |
| `400` | 参数、filter、scope、分页或 body 不合法 | 修请求，不要盲目重试 |
| `401` | token 缺失或错误 | 更新凭据再试 |
| `404` | paper / evidence / asset / task / run 不存在 | 停止使用该 ID，必要时重新发现 |
| `409` | 内容 / representation 尚未就绪，或当前 operation 不允许 | 等待前置处理完成后再读 |
| `503` | 检索 / 模型后端暂不可用 | 做退避重试 |

---

## 4. 旧语义修正（这次必须按新契约接）

### Evidence Pack 的正确输入

`POST /api/v1/evidence/packs` 当前输入是：

- `claims`
- `scope`
- `filters`
- `top_k_per_claim`

**不是**“只处理显式 `evidence_ids`”。

### Citation Grounding 的正确输入

`POST /api/v1/citations/ground` 当前输入是：

- `draft`
- `scope`
- `filters`
- `top_k_per_claim`

**不接受**调用方传入：

- `claims`
- 顶层 `evidence_ids`

### 轮询方式

- paper processing：用 `task_id` 查 `GET /api/v1/tasks/{task_id}`
- workflow：用 `run_id` 查 `GET /api/v1/workflows/{run_id}`

### Grounding 的 span 位置

调用方不需要提供 Python 字符 offset。Citation Grounding 会在服务端拆分 draft span，并由模型返回 `span_id` 级别的选择结果。

---

## 5. 关键返回对象怎么看

### PaperSummary

常用字段：

- `paper_id`
- `title` / `year` / `doi` / `venue`
- `processing_status`
- `capabilities`
- `asset_counts`
- `paper_quality`

其中：

- `processing_status` 适合判断 parse / evidence / embedding / multimodal 是否就绪
- `capabilities` 适合判断能否读 content、检索 evidence、下载 PDF

### EvidenceHit

常用字段：

- `evidence_id`
- `representation`
- `paper`
- `content.snippet`
- `content.quote`
- `location`
- `modality`
- `block_type`
- `score`
- `assets`
其中：

- `content.snippet` 适合列表展示
- `content.quote` 适合快速看命中句
- `location` 适合判断是否已有页码 / bbox 级定位
- `assets` 适合发现与该 evidence 绑定的图表、公式、PDF 等资源

### EvidenceContentResponse

常用字段：

- `evidence_id`
- `paper`
- `content.text`
- `content.evidence_char_start` / `content.evidence_char_end`
- `content.window_char_start` / `content.window_char_end`
- `content.more_before` / `content.more_after`
- `location`
- `assets`

适用场景：search 命中后做正文核验，确认 claim 是否真的被该上下文支持。

### EvidenceSourceResponse

常用字段：

- `citation.evidence_id`
- `citation.paper`
- `citation.quote`
- `citation.location.page`
- `citation.location.bbox`
- `citation.assets`
- `pdf_asset_id`

适用场景：把 evidence 还原到 PDF、页码、框选区域和相关资源。

### TaskResponse

常用字段：

- `task_id`
- `paper_id`
- `operation`
- `status`
- `progress`
- `error_message`
- `details`

其中 `details` 常见会带：

- `queue`
- `provider`
- `result`

### WorkflowSubmissionResponse

常用字段：

- `run_id`
- `workflow`
- `status`

提交 workflow 后通常立刻拿到：

```json
{
  "run_id": "run_123",
  "workflow": "evidence_pack",
  "status": "queued"
}
```

### WorkflowRunResponse

常用字段：

- `run_id`
- `workflow`
- `status`
- `progress`
- `request`
- `result`
- `details`
- `error_message`

`details.stage` 常见值包括：

- `claim_extraction`
- `retrieval_and_judging`
- `citation_assembly`
- `retrying`
- `succeeded`
- `failed`

#### evidence_pack.result 重点看什么

通常重点读取：

- `claims[]`
  - `claim_id`
  - `claim`
  - `status`
  - `evidence[]`
- `evidence[]` 内常见字段：
  - `evidence_id`
  - `quote`
  - `paper`
  - `source_location`
  - `assets`
  - `retrieval_score`
  - `verdict`
  - `confidence`
  - `reason`

#### citation_grounding.result 重点看什么

通常重点读取：

- `grounded_answer`
- `draft_spans`
- `claims`
- `citations`
- `unverified_claims`
- `contradictory_evidence`

注意：grounding 结果里的 citation marker 是工作流写回的，例如 `[1]`、`[unverified]`；调用方不需要自己提供字符级 span offset。

---

## 6. System

### `GET /health`

**用途**

- 无鉴权基础探活。

**何时调用**

- 容器 / 进程存活检查。
- 反向代理、负载均衡或本地 smoke test。

**输入参数**

- 无。

**成功响应关键字段**

- `status`

**最小示例**

```json
{
  "status": "ok"
}
```

---

### `GET /api/v1/system/health`

**用途**

- 带业务语义的系统健康检查。

**何时调用**

- Agent 启动前检查检索、解析、workflow 等能力是否在线。

**输入参数**

- 无。

**成功响应关键字段**

- `status`
- `checked_at`
- `components[]`
- `capabilities[]`

**最小示例**

```json
{
  "status": "ok",
  "checked_at": "2026-07-11T10:00:00Z",
  "components": [
    {
      "name": "database",
      "status": "ok",
      "latency_ms": 8,
      "detail": true
    }
  ],
  "capabilities": ["paper_search", "evidence_search", "workflow"]
}
```

**Agent 使用提示**

- 这里更适合做“能不能开始工作”的前置检查；真正排查单个任务失败原因时，还是回到 `task` / `workflow` 详情。

---

## 7. Catalog / Papers

### `GET /api/v1/papers/meta-catalog`

**用途**

- 返回当前可筛选字段、排序字段和过滤语义。

**何时调用**

- 你想动态生成搜索 UI。
- 你不确定某个字段能否用于 `papers/search` 或 `evidence/search`。

**Query 参数**

- `include_sample_values`：是否附带部分字段示例值。

**成功响应关键字段**

- `fields[]`
- `sort_fields[]`
- `default_sort`
- `default_fields[]`
- `filter_semantics`

**最小示例**

```json
{
  "fields": [
    {
      "name": "year",
      "type": "integer",
      "paper_search_filterable": true,
      "evidence_search_filterable": true,
      "sortable": true,
      "operators": ["eq", "gte", "lte"]
    }
  ],
  "sort_fields": ["year", "created_at", "updated_at", "paper_quality.score"],
  "default_sort": {"field": "year", "order": "desc"},
  "default_fields": ["paper_id", "title", "year"],
  "filter_semantics": "Values within one list filter are ORed; different filter fields are ANDed."
}
```

**Agent 使用提示**

- 如果你的 agent 会自动构造 filters，先看这里比硬编码字段名更稳。

---

### `POST /api/v1/papers/search`

**用途**

- 搜论文元数据，拿到候选 `paper_id`。

**何时调用**

- 你需要先缩小论文范围，再做 evidence 检索。
- 你要根据标题、年份、venue、doi 等找到目标论文。

**请求体关键字段**

- `query`：可选，自然语言或关键词搜索。
- `filters.year.gte` / `filters.year.lte`
- `filters.venue[]`
- `filters.doi`
- `filters.language[]`
- `filters.publication_type[]`
- `filters.has_fulltext`
- `filters.ai_ready`
- `sort.field`：`year` / `created_at` / `updated_at` / `paper_quality.score`
- `sort.order`：`asc` / `desc`
- `page`：默认 `1`
- `page_size`：默认 `20`，最大 `100`

**最小请求示例**

```json
{
  "query": "retrieval augmented generation",
  "filters": {
    "year": {"gte": 2022},
    "language": ["en"],
    "has_fulltext": true
  },
  "sort": {
    "field": "year",
    "order": "desc"
  },
  "page": 1,
  "page_size": 20
}
```

**成功响应关键字段**

- `items[]`：每项是 `PaperSummary`
- `pagination`

**最小响应示例**

```json
{
  "items": [
    {
      "paper_id": "paper_123",
      "title": "Example Paper",
      "year": 2024,
      "doi": "10.1000/example",
      "venue": "ACL",
      "processing_status": {
        "parse": "ready",
        "fulltext": "ready",
        "evidence": "ready",
        "embedding": "ready",
        "multimodal": "ready"
      },
      "capabilities": {
        "has_pdf": true,
        "has_fulltext": true,
        "has_evidence": true,
        "has_embeddings": true,
        "has_multimodal_assets": true
      }
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 1,
    "total_pages": 1,
    "has_next": false,
    "has_prev": false
  }
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`

**Agent 使用提示**

- 如果后续要做 Pack / Ground，先用论文搜索拿到一小组 `paper_ids`，再塞进 `scope.paper_ids`，效果通常比全库开放检索更稳定。

---

### `POST /api/v1/papers`

**用途**

- 上传 PDF，创建论文记录与 PDF asset。

**何时调用**

- 本地库里还没有这篇论文，需要灌库。

**请求方式**

- `multipart/form-data`

**表单字段**

- `file`：必填，PDF 文件。
- `title`：可选，默认使用文件名。

**成功响应关键字段**

- `paper`：新建或命中的 `PaperSummary`
- `task`：一次 `ingest` 任务结果

**最小响应示例**

```json
{
  "paper": {
    "paper_id": "paper_123",
    "title": "uploaded.pdf"
  },
  "task": {
    "task_id": "task_123",
    "paper_id": "paper_123",
    "operation": "ingest",
    "status": "succeeded",
    "progress": 1.0
  }
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`：不是 PDF、空文件、超过大小限制。
- `409 INGEST_CONFLICT`：罕见冲突。

**Agent 使用提示**

- 这一步只完成“入库 + PDF 落盘 + 记录创建”。要得到 content / evidence / embedding，还要继续跑后续 `operations`。
- 如果上传的是重复 PDF，服务端会返回已有 `paper`，并给一个 `duplicate` 语义的 `ingest` task。

---

### `GET /api/v1/papers/{paper_id}`

**用途**

- 读取单篇论文的摘要信息与处理状态。

**何时调用**

- 已经拿到 `paper_id`，想确认它是否 ready、有哪些能力。

**路径参数**

- `paper_id`

**成功响应关键字段**

- 整体就是 `PaperSummary`

**最小响应示例**

```json
{
  "paper_id": "paper_123",
  "title": "Example Paper",
  "year": 2024,
  "venue": "ACL",
  "processing_status": {
    "parse": "ready",
    "fulltext": "ready",
    "evidence": "ready",
    "embedding": "ready",
    "multimodal": "ready"
  },
  "capabilities": {
    "has_pdf": true,
    "has_fulltext": true,
    "has_evidence": true,
    "has_embeddings": true,
    "has_multimodal_assets": true
  }
}
```

**常见错误码**

- `404 PAPER_NOT_FOUND`

**Agent 使用提示**

- 在调用 `/content`、`/evidence`、`/assets` 前，可以先看这里判断是否值得继续读。

---

## 8. Paper Read / Browse

### `GET /api/v1/papers/{paper_id}/content`

**用途**

- 分段读取 canonical paper content。

**何时调用**

- 你要通读正文。
- 你要在某篇 paper 内做更长窗口的上下文分析。

**路径参数**

- `paper_id`

**Query 参数**

- `offset`：默认 `0`
- `limit`：默认 `2000`，范围 `1..10000`

**成功响应关键字段**

- `paper`
- `content.text`
- `content.char_start` / `content.char_end`
- `content.chars_returned`
- `content.next_offset`
- `content.more`

**最小响应示例**

```json
{
  "paper": {
    "paper_id": "paper_123",
    "title": "Example Paper"
  },
  "content": {
    "text": "Introduction ...",
    "char_start": 0,
    "char_end": 1200,
    "chars_returned": 1200,
    "next_offset": 1200,
    "more": true
  }
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`
- `404 PAPER_NOT_FOUND`
- `409 CONTENT_NOT_READY`

**Agent 使用提示**

- 建议按 `next_offset` 滚动读取，不要自己猜下一个 offset。

---

### `GET /api/v1/papers/{paper_id}/evidence`

**用途**

- 浏览某篇论文已经构建好的 evidence。

**何时调用**

- 你已知论文范围，只想在该论文内部看可用证据。

**路径参数**

- `paper_id`

**Query 参数**

- `modality`
- `block_type`
- `section_type`
- `page`：默认 `1`
- `page_size`：默认 `20`，最大 `100`

**成功响应关键字段**

- `items[]`：每项是 `EvidenceHit`
- `pagination`

**最小响应示例**

```json
{
  "items": [
    {
      "evidence_id": "ev_123",
      "representation": {
        "representation_id": "repr_123",
        "type": "canonical"
      },
      "paper": {
        "paper_id": "paper_123",
        "title": "Example Paper"
      },
      "content": {
        "snippet": "...",
        "quote": "The method improves accuracy by 4.2%."
      },
      "modality": "text",
      "block_type": "paragraph",
      "assets": []
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 1,
    "total_pages": 1,
    "has_next": false,
    "has_prev": false
  }
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`
- `404 PAPER_NOT_FOUND`

**Agent 使用提示**

- 这是“按 paper browse”，不是向量检索；适合已知论文后做结构化浏览。

---

### `GET /api/v1/papers/{paper_id}/assets`

**用途**

- 浏览论文挂载的资源。

**何时调用**

- 你需要图、表、PDF、附件等资源清单。

**路径参数**

- `paper_id`

**Query 参数**

- `asset_types`：逗号分隔，例如 `pdf,figure,table`
- `page`：默认 `1`
- `page_size`：默认 `20`，最大 `100`

**成功响应关键字段**

- `items[]`
  - `asset_id`
  - `asset_type`
  - `mime_type`
  - `caption`
  - `location`
- `pagination`

**最小响应示例**

```json
{
  "items": [
    {
      "asset_id": "asset_pdf_1",
      "asset_type": "pdf",
      "mime_type": "application/pdf",
      "caption": null,
      "location": null
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 20,
    "total": 1,
    "total_pages": 1,
    "has_next": false,
    "has_prev": false
  }
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`
- `404 PAPER_NOT_FOUND`

**Agent 使用提示**

- 如果是 evidence 回源，通常先从 `/evidence/{evidence_id}/source` 发现相关 `asset_id`，再来这里或直接读单个 asset。

---

### `GET /api/v1/papers/{paper_id}/relations`

**用途**

- 读取论文 references 或 citations 关系。

**何时调用**

- 你要做引文网络浏览、追踪参考文献或被引文献。

**路径参数**

- `paper_id`

**Query 参数**

- `relation`：必填，只能是 `references` 或 `citations`
- `page`：默认 `1`
- `page_size`：默认 `25`，最大 `100`

**成功响应关键字段**

- `items[]`
  - `relation`
  - `reference_id`
  - `paper_id`
  - `raw_text`
  - `parsed_title`
  - `parsed_authors[]`
  - `parsed_year`
  - `doi`
  - `target_paper`
  - `order_index`
- `pagination`

**最小响应示例**

```json
{
  "items": [
    {
      "relation": "references",
      "reference_id": "ref_123",
      "paper_id": "paper_123",
      "parsed_title": "Prior Work",
      "parsed_authors": ["Smith"],
      "parsed_year": 2021,
      "doi": "10.1000/prior",
      "target_paper": {
        "paper_id": "paper_456",
        "title": "Prior Work"
      },
      "order_index": 1
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 25,
    "total": 1,
    "total_pages": 1,
    "has_next": false,
    "has_prev": false
  }
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`
- `404 PAPER_NOT_FOUND`

**Agent 使用提示**

- `references` 表示该 paper 引用了谁；`citations` 表示谁引用了该 paper。不要把方向搞反。

---

### `GET /api/v1/papers/{paper_id}/citation-contexts`

**用途**

- 读取论文内的 citation context 窗口。

**何时调用**

- 你想知道某条 reference 在正文中是如何被提及的。

**路径参数**

- `paper_id`

**Query 参数**

- `page`：默认 `1`
- `page_size`：默认 `25`，最大 `100`

**成功响应关键字段**

- `items[]`
  - `citation_context_id`
  - `paper`
  - `reference`
  - `context.quote`
  - `context.location`
- `pagination`

**最小响应示例**

```json
{
  "items": [
    {
      "citation_context_id": "ctx_123",
      "paper": {
        "paper_id": "paper_123",
        "title": "Example Paper"
      },
      "reference": {
        "reference_id": "ref_123",
        "parsed_title": "Prior Work",
        "parsed_year": 2021,
        "doi": "10.1000/prior"
      },
      "context": {
        "quote": "Following Prior Work, we ...",
        "location": {
          "page": 3,
          "bbox": [100, 120, 420, 180]
        }
      }
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 25,
    "total": 1,
    "total_pages": 1,
    "has_next": false,
    "has_prev": false
  }
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`
- `404 PAPER_NOT_FOUND`

**Agent 使用提示**

- 这对“为什么作者在这里引用这篇文献”这类问题很有帮助；它和 bibliography formatting 是不同层面。

---

## 9. Evidence Retrieval / Verify

### `POST /api/v1/evidence/search`

**用途**

- 按自然语言 query 检索 evidence 候选。

**何时调用**

- 你需要找支持 / 反驳某个说法的候选证据。

**请求体关键字段**

- `query`：必填。
- `scope.paper_ids[]`
- `scope.evidence_ids[]`
- `filters.year`
- `filters.venue[]`
- `filters.doi`
- `filters.language[]`
- `filters.publication_type[]`
- `filters.modality[]`
- `filters.block_type[]`
- `filters.section_type[]`
- `filters.has_assets`
- `top_k`：默认 `50`，最大 `100`
- `page`：默认 `1`
- `page_size`：默认 `10`，最大 `50`

**最小请求示例**

```json
{
  "query": "evidence that retrieval improves factual accuracy",
  "scope": {
    "paper_ids": ["paper_123", "paper_456"]
  },
  "filters": {
    "modality": ["text"],
    "section_type": ["results", "discussion"],
    "language": ["en"]
  },
  "top_k": 20,
  "page": 1,
  "page_size": 10
}
```

**成功响应关键字段**

- `items[]`：每项是 `EvidenceHit`
- `pagination`

**最小响应示例**

```json
{
  "items": [
    {
      "evidence_id": "ev_123",
      "representation": {
        "representation_id": "repr_123",
        "type": "canonical"
      },
      "paper": {
        "paper_id": "paper_123",
        "title": "Example Paper"
      },
      "content": {
        "snippet": "... improves factual accuracy ...",
        "quote": "Our retrieval component improves factual accuracy by 6.1%."
      },
      "location": {
        "page": 5,
        "bbox": [90, 150, 430, 210]
      },
      "modality": "text",
      "block_type": "paragraph",
      "score": 0.82,
      "assets": []
    }
  ],
  "pagination": {
    "page": 1,
    "page_size": 10,
    "total": 7,
    "total_pages": 1,
    "has_next": false,
    "has_prev": false
  }
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`
- `503 BACKEND_UNAVAILABLE` 或等价后端不可用错误

**Agent 使用提示**

- 命中之后不要直接引用，优先继续调用 `/content` 和 `/source`。
- `pagination.total` 是过滤后候选池大小，不是“数据库里一共有多少条相似证据”。

---

### `GET /api/v1/evidence/{evidence_id}`

**用途**

- 读取单条 evidence 的基础命中对象。

**何时调用**

- 你已有 `evidence_id`，只想快速取回 `EvidenceHit`。

**路径参数**

- `evidence_id`

**成功响应关键字段**

- 与 `EvidenceHit` 一致，但通常不带 `score`

**最小响应示例**

```json
{
  "evidence_id": "ev_123",
  "representation": {
    "representation_id": "repr_123",
    "type": "canonical"
  },
  "paper": {
    "paper_id": "paper_123",
    "title": "Example Paper"
  },
  "content": {
    "snippet": "...",
    "quote": "Our retrieval component improves factual accuracy by 6.1%."
  },
  "modality": "text",
  "block_type": "paragraph",
  "assets": []
}
```

**常见错误码**

- `404 EVIDENCE_NOT_FOUND`
- `409 CONTENT_NOT_READY`

**Agent 使用提示**

- 如果要判断真支持还是伪相关，还是继续读 `/content` 更合适。

---

### `GET /api/v1/evidence/{evidence_id}/content`

**用途**

- 读取某条 evidence 所在正文窗口。

**何时调用**

- search 命中后做人工 / agent 核验。

**路径参数**

- `evidence_id`

**Query 参数**

- `before`：默认 `1200`
- `after`：默认 `2400`

**成功响应关键字段**

- `content.text`
- `content.evidence_char_start` / `content.evidence_char_end`
- `content.window_char_start` / `content.window_char_end`
- `content.more_before` / `content.more_after`
- `location`
- `assets`

**最小响应示例**

```json
{
  "evidence_id": "ev_123",
  "paper": {
    "paper_id": "paper_123",
    "title": "Example Paper"
  },
  "content": {
    "text": "... larger context window ...",
    "evidence_char_start": 5010,
    "evidence_char_end": 5102,
    "window_char_start": 3810,
    "window_char_end": 7502,
    "more_before": true,
    "more_after": true
  },
  "location": {
    "page": 5,
    "bbox": [90, 150, 430, 210]
  },
  "assets": []
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`
- `404 EVIDENCE_NOT_FOUND`
- `409 CONTENT_NOT_READY`

**Agent 使用提示**

- `before` / `after` 越大，上下文越完整，但 token 成本也越高；通常先小窗验证，不够再扩大。

---

### `GET /api/v1/evidence/{evidence_id}/source`

**用途**

- 返回可引用的 citation 结构和 PDF 回源信息。

**何时调用**

- 你需要页码、bbox、PDF asset，或者要把 evidence 映射回原论文。

**路径参数**

- `evidence_id`

**成功响应关键字段**

- `citation`
  - `evidence_id`
  - `paper`
  - `quote`
  - `location`
  - `assets`
- `pdf_asset_id`

**最小响应示例**

```json
{
  "citation": {
    "evidence_id": "ev_123",
    "paper": {
      "paper_id": "paper_123",
      "title": "Example Paper"
    },
    "quote": "Our retrieval component improves factual accuracy by 6.1%.",
    "location": {
      "page": 5,
      "bbox": [90, 150, 430, 210]
    },
    "assets": [
      {
        "asset_id": "asset_fig_1",
        "asset_type": "figure",
        "mime_type": "image/png",
        "caption": "Figure 2"
      }
    ]
  },
  "pdf_asset_id": "asset_pdf_1"
}
```

**常见错误码**

- `404 EVIDENCE_NOT_FOUND`

**Agent 使用提示**

- 这是最终“给出处”的关键接口；如果你需要下载整篇 PDF，可继续用 `pdf_asset_id` 调 asset 下载。

---

## 10. Assets

### `GET /api/v1/assets/{asset_id}`

**用途**

- 读取单个 asset 的元数据。

**何时调用**

- 你已有 `asset_id`，想知道它是什么、来自哪篇论文、有没有 location。

**路径参数**

- `asset_id`

**成功响应关键字段**

- `asset_id`
- `paper`
- `asset_type`
- `mime_type`
- `sha256`
- `size_bytes`
- `caption`
- `location`
- `created_from`

**最小响应示例**

```json
{
  "asset_id": "asset_fig_1",
  "paper": {
    "paper_id": "paper_123",
    "title": "Example Paper"
  },
  "asset_type": "figure",
  "mime_type": "image/png",
  "sha256": "abc123",
  "size_bytes": 204800,
  "caption": "Figure 2",
  "location": {
    "page": 5,
    "bbox": [90, 150, 430, 210]
  },
  "created_from": "parse"
}
```

**常见错误码**

- `404 ASSET_NOT_FOUND`

**Agent 使用提示**

- 对 agent 来说，`asset_type + mime_type + caption + location` 往往已经足够决定要不要下载。

---

### `GET /api/v1/assets/{asset_id}/download`

**用途**

- 下载 asset 二进制内容。

**何时调用**

- 你真的需要文件本体，例如 PDF、图像、表格裁片。

**路径参数**

- `asset_id`

**响应**

- 成功时直接返回二进制文件，不是 JSON。
- `Content-Type` 来自 `asset.mime_type` 或文件类型推断。

**常见错误码**

- `404 ASSET_NOT_FOUND`

**Agent 使用提示**

- 先调用 `/assets/{asset_id}` 确认类型再下载，可以避免不必要的大文件传输。

---

## 11. Bibliography

### `POST /api/v1/bibliography/format`

**用途**

- 把 `paper_id` / `reference_id` 批量格式化为引文字符串。

**何时调用**

- 你已经有明确论文或 reference 列表，只需要生成 bibliography 文本。

**请求体关键字段**

- `style`：当前支持 `gbt7714`、`apa`、`ieee`
- `paper_ids[]`
- `reference_ids[]`

至少要提供 `paper_ids` 或 `reference_ids` 之一。

**最小请求示例**

```json
{
  "style": "apa",
  "paper_ids": ["paper_123"],
  "reference_ids": ["ref_456"]
}
```

**成功响应关键字段**

- `style`
- `items[]`
  - `source_type`
  - `paper_id` / `reference_id`
  - `formatted`

**最小响应示例**

```json
{
  "style": "apa",
  "items": [
    {
      "source_type": "paper",
      "paper_id": "paper_123",
      "formatted": "Smith, J. (2024). Example Paper. ACL. DOI: 10.1000/example."
    },
    {
      "source_type": "reference",
      "reference_id": "ref_456",
      "formatted": "Doe, A. (2021). Prior Work."
    }
  ]
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`：style 不支持，或两类 ID 都没给。
- `404 PAPER_NOT_FOUND`
- `404 REFERENCE_NOT_FOUND`

**Agent 使用提示**

- 这是“格式化输出”，不是“自动找支持证据”；不要把它和 Pack / Ground 混用。

---

## 12. Paper Processing Tasks

### `POST /api/v1/papers/{paper_id}/operations/{operation}`

**用途**

- 对单篇论文发起异步处理任务。

**何时调用**

- 论文刚入库，还没有 parse / evidence / embedding / multimodal 结果。

**路径参数**

- `paper_id`
- `operation`：只能是 `parse`、`build_evidence`、`enrich_multimodal`、`embed`

**请求体关键字段**

- `force`：默认 `false`
- `options`：可选扩展参数字典

**最小请求示例**

```json
{
  "force": false,
  "options": {}
}
```

**成功响应关键字段**

- 返回 `TaskResponse`
- 如果同一 `paper_id + operation` 已有 `queued/running` 任务，且 `force=false`，会直接返回当前任务

**最小响应示例**

```json
{
  "task_id": "task_123",
  "paper_id": "paper_123",
  "operation": "build_evidence",
  "status": "queued",
  "progress": 0.0,
  "details": {
    "queue": {
      "attempt": 0,
      "job_id": "job_123"
    }
  }
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`：operation 不支持。
- `404 PAPER_NOT_FOUND`
- `409 OPERATION_NOT_ALLOWED`：例如 parse 时没有 PDF。

**Agent 使用提示**

- 常见顺序通常是：`parse -> build_evidence -> enrich_multimodal / embed`。
- `force=true` 会把当前同操作进行中的任务标为 `stale` 再重提，不要无脑使用。

---

### `GET /api/v1/tasks/{task_id}`

**用途**

- 查询单个 paper processing task 的状态。

**何时调用**

- 轮询 parse / build_evidence / enrich_multimodal / embed 是否完成。

**路径参数**

- `task_id`

**成功响应关键字段**

- `task_id`
- `paper_id`
- `operation`
- `status`
- `progress`
- `error_message`
- `details`
- `created_at` / `started_at` / `finished_at`

**最小响应示例**

```json
{
  "task_id": "task_123",
  "paper_id": "paper_123",
  "operation": "parse",
  "status": "running",
  "progress": 0.4,
  "error_message": null,
  "details": {
    "queue": {
      "attempt": 0,
      "job_id": "job_123"
    }
  }
}
```

**常见错误码**

- `404 TASK_NOT_FOUND`

**Agent 使用提示**

- 轮询 task 时只用 `task_id`；如果你手里拿的是 `run_id`，应该去查 workflow，不是这里。

---

### `POST /api/v1/tasks/{task_id}/refresh`

**用途**

- 刷新并返回 task 当前状态。

**何时调用**

- 兼容某些客户端希望用 POST 做轮询的场景。

**路径参数**

- `task_id`

**成功响应关键字段**

- 与 `GET /api/v1/tasks/{task_id}` 相同。

**最小响应示例**

```json
{
  "task_id": "task_123",
  "paper_id": "paper_123",
  "operation": "embed",
  "status": "succeeded",
  "progress": 1.0,
  "details": {}
}
```

**常见错误码**

- `404 TASK_NOT_FOUND`

**Agent 使用提示**

- 这是 refresh 别名，不是“重新执行任务”。要重跑任务，还是回到 `POST /papers/{paper_id}/operations/{operation}`。

---

## 13. Agent Workflows

### `POST /api/v1/evidence/packs`

**用途**

- 对显式 `claims[]` 做 evidence 检索、判定和打包。

**何时调用**

- 你已经把问题拆成一组明确 claim，想批量验证每条 claim 是否有支持证据。

**请求体关键字段**

- `claims[]`
  - `claim_id`：可选
  - `text`：必填
- `scope`
- `filters`
- `top_k_per_claim`：默认 `5`，最大 `10`

**最小请求示例**

```json
{
  "claims": [
    {
      "claim_id": "c1",
      "text": "Retrieval improves factual accuracy in long-form generation."
    }
  ],
  "scope": {
    "paper_ids": ["paper_123", "paper_456"]
  },
  "filters": {
    "language": ["en"],
    "modality": ["text"]
  },
  "top_k_per_claim": 5
}
```

**成功响应关键字段**

- 返回 `202 Accepted`
- 响应体是 `WorkflowSubmissionResponse`

**最小响应示例**

```json
{
  "run_id": "run_123",
  "workflow": "evidence_pack",
  "status": "queued"
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`

**Agent 使用提示**

- 这里的输入是 `claims`，不是“显式 evidence_ids 列表”。
- 提交后不要期待同步返回结果，必须用 `run_id` 轮询 `/workflows/{run_id}`。

---

### `POST /api/v1/citations/ground`

**用途**

- 对整段 `draft` 自动抽取可核验 span，并回填 citation marker。

**何时调用**

- 你已经有初稿，需要做引用落地和核验标注。

**请求体关键字段**

- `draft`
- `scope`
- `filters`
- `top_k_per_claim`

**最小请求示例**

```json
{
  "draft": "Retrieval improves factual accuracy in long-form generation.",
  "scope": {
    "paper_ids": ["paper_123", "paper_456"]
  },
  "filters": {
    "language": ["en"],
    "modality": ["text"]
  },
  "top_k_per_claim": 5
}
```

**成功响应关键字段**

- 返回 `202 Accepted`
- 响应体是 `WorkflowSubmissionResponse`

**最小响应示例**

```json
{
  "run_id": "run_456",
  "workflow": "citation_grounding",
  "status": "queued"
}
```

**常见错误码**

- `400 INVALID_ARGUMENT`

**Agent 使用提示**

- Grounding 的输入是 `draft`，**不是** `claims`。
- Grounding 当前**不接受**调用方传入顶层 `evidence_ids` 或字符级 span offset。
- 它会尽量保持原 draft，只补 citation marker 和核验结果。

---

### `GET /api/v1/workflows/{run_id}`

**用途**

- 查询 Evidence Pack / Citation Grounding 的运行状态与结果。

**何时调用**

- 提交 workflow 后轮询进度。

**路径参数**

- `run_id`

**成功响应关键字段**

- `run_id`
- `workflow`
- `status`
- `progress`
- `request`
- `result`
- `details`
- `error_message`
- `created_at` / `started_at` / `finished_at`

**最小响应示例：运行中**

```json
{
  "run_id": "run_123",
  "workflow": "evidence_pack",
  "status": "running",
  "progress": 0.5,
  "request": {
    "claims": [
      {
        "claim_id": "c1",
        "text": "Retrieval improves factual accuracy in long-form generation."
      }
    ],
    "scope": {
      "paper_ids": ["paper_123"]
    },
    "filters": {},
    "top_k_per_claim": 5
  },
  "result": null,
  "details": {
    "stage": "retrieval_and_judging"
  },
  "error_message": null
}
```

**最小响应示例：完成后（evidence_pack）**

```json
{
  "run_id": "run_123",
  "workflow": "evidence_pack",
  "status": "succeeded",
  "progress": 1.0,
  "request": {},
  "result": {
    "claims": [
      {
        "claim_id": "c1",
        "claim": "Retrieval improves factual accuracy in long-form generation.",
        "status": "supported",
        "evidence": [
          {
            "evidence_id": "ev_123",
            "quote": "Our retrieval component improves factual accuracy by 6.1%.",
            "paper": {
              "paper_id": "paper_123",
              "title": "Example Paper"
            },
            "source_location": {
              "page": 5,
              "bbox": [90, 150, 430, 210]
            },
            "assets": [],
            "retrieval_score": 0.82,
            "verdict": "support",
            "confidence": 0.91,
            "reason": "Direct quantitative support."
          }
        ]
      }
    ]
  },
  "details": {
    "stage": "succeeded"
  },
  "error_message": null
}
```

**最小响应示例：完成后（citation_grounding）**

```json
{
  "run_id": "run_456",
  "workflow": "citation_grounding",
  "status": "succeeded",
  "progress": 1.0,
  "request": {},
  "result": {
    "grounded_answer": "Retrieval improves factual accuracy in long-form generation [1].",
    "draft_spans": [],
    "claims": [],
    "citations": [
      {
        "marker": "[1]",
        "paper_id": "paper_123",
        "evidence_id": "ev_123"
      }
    ],
    "unverified_claims": [],
    "contradictory_evidence": []
  },
  "details": {
    "stage": "succeeded"
  },
  "error_message": null
}
```

**常见错误码**

- `404 WORKFLOW_NOT_FOUND`

**Agent 使用提示**

- `status=succeeded` 不等于“所有 claim 都被支持”；最终仍要读 `result` 里的 claim 级 verdict。
- `error_message` 为空时，优先看 `details.stage` 判断卡在哪个阶段。

---

## 14. 轮询与恢复建议

### task 轮询建议

- 适用于：`parse`、`build_evidence`、`enrich_multimodal`、`embed`
- 轮询接口：`GET /api/v1/tasks/{task_id}` 或 `POST /api/v1/tasks/{task_id}/refresh`
- 成功条件：`status = succeeded`
- 失败条件：`status = failed`
- 替代条件：`status = stale`

### workflow 轮询建议

- 适用于：`evidence_pack`、`citation_grounding`
- 轮询接口：`GET /api/v1/workflows/{run_id}`
- 成功条件：`status = succeeded`
- 失败条件：`status = failed`

### 推荐恢复策略

- `400`：修请求，不要重试同一 payload。
- `401`：更新 token 后重试。
- `404`：停止使用该 ID，必要时重新发现对象。
- `409`：等待前置 processing 完成，再重试读接口。
- `503`：做指数退避重试。

---

## 15. External Discovery

External Discovery 用于从公共学术数据源发现论文，并把技术上可直接获取且通过文件校验的 PDF
暂存到本地。许可证作为来源元数据保留，不参与下载判定。它不查询本地 Evidence，也不会自动创建 `Paper`。

### 配置

```dotenv
DISCOVERY_CONTACT_EMAIL=research@example.org
OPENALEX_API_KEY=replace-with-free-openalex-key
UNPAYWALL_EMAIL=research@example.org
DISCOVERY_DOWNLOAD_ROOT=../storage/downloads
DISCOVERY_STORAGE_PROVIDER=local
DISCOVERY_MAX_DOWNLOAD_BYTES=52428800
DISCOVERY_REQUEST_TIMEOUT_SECONDS=30
DISCOVERY_SEARCH_TIMEOUT_SECONDS=8
DISCOVERY_CACHE_FRESH_SECONDS=300
DISCOVERY_CACHE_STALE_SECONDS=86400
LITERATURE_SEARCH_SESSION_SECONDS=900
DISCOVERY_ALLOW_FAKE_IP_DNS=false

# 仅当 DISCOVERY_STORAGE_PROVIDER=cos 时需要；不要提交真实密钥。
COS_BUCKET=gibbs-papers-1234567890
COS_REGION=ap-shanghai
COS_ENDPOINT=gibbs-papers-1234567890.cos.ap-shanghai.myqcloud.com
COS_SECRET_ID=
COS_SECRET_KEY=
COS_PREFIX=literature-kb
```

OpenAlex key 可以留空进行低额度试用，但建议申请免费 key。Unpaywall 不需要 key，但要求提供
联系邮箱。真实值只能放在本地 `.env` 或部署密钥系统中。

### `GET /api/v1/discovery/sources`

返回当前支持的数据源、用途与配置状态。`configured=false` 不一定代表完全不可调用，例如
OpenAlex 允许低额度匿名试用；它表示尚未配置推荐的生产身份信息。

### `POST /api/v1/discovery/search`

通过 OpenAlex 搜索外部论文：

```json
{
  "query": "retrieval augmented generation",
  "year_from": 2023,
  "year_to": 2026,
  "open_access_only": true,
  "publication_types": ["article"],
  "page_size": 25,
  "cursor": "*"
}
```

响应使用 OpenAlex cursor。读取 `next_cursor` 并传入下一次请求；不要构造或修改 cursor。
返回的 `download_candidates` 是候选，不代表已经下载或已经进入本地论文库。

### `POST /api/v1/discovery/works/resolve`

按 OpenAlex ID 或 DOI 解析一篇论文，并在配置邮箱时通过 Unpaywall 补充 OA 候选：

```json
{"openalex_id": "W2741809807"}
```

也可以使用：

```json
{"doi": "10.1038/nature12373"}
```

两个标识符必须且只能提供一个。候选的 `access` 可能是：

- `commercial_allowed`
- `noncommercial_only`
- `open_license_other`
- `unknown`
- `restricted`

### `POST /api/v1/discovery/downloads`

服务端会重新解析论文并选择一个具有 PDF 地址的候选，不接受调用方传入 URL：

```json
{
  "openalex_id": "W2741809807",
  "allow_unknown_license": false
}
```

`allow_unknown_license` 为兼容旧客户端而保留，当前不参与候选选择。只要来源提供 PDF 地址，
服务端就会尝试下载；网络地址、HTTP 响应、文件大小和 `%PDF-` 内容校验仍会阻止技术上不可用的文件。

成功返回 `201`。默认本地模式下，文件保存在：

```text
storage/downloads/{sha256}.pdf
storage/downloads/{sha256}.json
```

JSON sidecar 保存 DOI、标题、来源、许可证、SHA-256 与大小。查询参数不会写入 sidecar，
以免持久化临时签名或访问令牌。下载器只允许 HTTPS 公网地址，逐次校验重定向，拒绝私网、
回环地址、超限文件和非 `%PDF-` 内容。

将 `DISCOVERY_STORAGE_PROVIDER=cos` 后，外部下载暂存会写入腾讯 COS，实际对象键为
`{COS_PREFIX}/downloads/{sha256}.{pdf,json}`；返回的 `storage_key` 仍是与后端无关的
`downloads/...`。该模式仅覆盖本节的外部下载暂存，现有 Paper、MinerU 和资产下载仍使用
`STORAGE_ROOT`。对象不设置公读 ACL，下载接口也不会返回 COS 直链或临时签名。

使用 Clash 等 Fake-IP 代理时，公网域名可能被本机解析到 `198.18.0.0/15`，此时下载会默认
失败。确认该网段由受控本机代理接管后，可以显式设置 `DISCOVERY_ALLOW_FAKE_IP_DNS=true`；
该选项仍拒绝 URL 直接使用 Fake-IP 或其他私网 IP，生产环境不建议开启。

### `POST /api/v1/literature/search`

统一入口并发检索本地 `PaperSearchService` 与 OpenAlex。远程总等待预算默认 3 秒；OpenAlex
失败时仍返回本地结果，并通过 `partial=true` 与 `source_status[]` 表示 `timeout`、
`rate_limited`、`stale_cache` 等降级状态。Redis 可用时，远程查询有 5 分钟新鲜缓存、
24 小时陈旧缓存，搜索会话保存 15 分钟；Redis 不可用时仍可返回实时第一页。

```json
{
  "query": "retrieval augmented generation",
  "search_mode": "hybrid",
  "sources": ["local", "openalex"],
  "filters": {
    "year_from": 2023,
    "year_to": 2026,
    "publication_types": ["article"],
    "language": ["en"],
    "open_access_only": false
  },
  "page_size": 20,
  "cursor": null
}
```

同一 DOI、OpenAlex ID、arXiv 或 PMID 会精确合并；只有标题近似时仅返回
`possible_duplicate=true`，不会自动合并。`availability` 区分 `local_ready`、
`local_processing`、`remote_downloadable` 与 `metadata_only`。

联合检索按每个来源独立计算排序位置，标题与查询完全一致的论文优先展示。
`origins` 只包含实际命中元数据的本地或远程检索来源，不包含仅提供下载链接的服务。
应用 cursor 会先返回本轮已获取但尚未展示的结果，再推进各来源 cursor，避免跨来源聚合时漏项。

### `POST /api/v1/literature/imports`

提交 1–50 个 OpenAlex、CORE 或 arXiv 来源选项。接口不接受 PDF URL、COS key 或任意
下载地址；worker 会重新解析来源并尝试所有 PDF 候选。`license_acknowledged` 为兼容旧客户端
而保留，当前不参与下载判定。

```json
{
  "items": [
    {
      "source": "openalex",
      "external_id": "W2741809807",
      "license_acknowledged": false
    }
  ]
}
```

成功提交返回 `202` 与 `ImportBatchResponse`。轮询
`GET /api/v1/import-batches/{batch_id}` 可查看每个 item 的 `details.stage`、`task_id`、
`paper_id`、失败原因和重试时间；失败项调用
`POST /api/v1/import-batches/{batch_id}/retry-failed`。任务阶段依次为 resolve、下载、暂存、
登记、完整处理和 ready。COS 已暂存但后续失败时，重试会直接回读 staged key；本地 PDF 已登记
但模型失败时，重试只恢复处理，不重新下载。

Pipeline 开启后，大型批次使用分块入口，避免单个 HTTP 请求和数据库事务承载全部项目：

```text
POST /api/v1/import-batches
POST /api/v1/import-batches/{batch_id}/items
POST /api/v1/import-batches/{batch_id}/finalize
POST /api/v1/import-batches/manifests
GET  /api/v1/import-batches/{batch_id}/items?cursor=...&limit=100
GET  /api/v1/tasks/{task_id}/stages
```

创建批次时可设置 `source` 和 `priority`；追加接口每次接受 1–5000 个与
`literature/imports` 相同的 item，并按规范 JSON 的 SHA-256 幂等去重。调用方可以连续追加
多个分块，最后调用 `finalize` 关闭 intake。明细接口按 `item_id` 使用 cursor 分页，单页
上限 500；批次摘要只内联前 100 项并通过 `next_cursor` 指示还有更多结果。
也可以用 multipart 上传 UTF-8 `.jsonl` 或 `.csv` manifest；服务器先持久化文件，再由
Dispatcher 每轮最多展开 `PIPELINE_EXPAND_CHUNK_SIZE` 条，因此上传请求不会创建大型数据库
事务。JSONL 每行是一个 `LiteratureImportItem`；CSV 的 `options` 列使用 JSON 数组文本。
无效行会作为独立 failed item 记录，不会阻断其余行。

当前 Pipeline 的固定阶段顺序为：

```text
remote_fetch → register → parse → evidence → resource_context → vectorize → index
```

MinerU 的 `submit/poll/finalize` 是短任务；等待外部解析时不会占用 parse worker。Embedding
API 只在 `vectorize` 阶段调用，`index` 阶段只执行本地数据库索引。Pipeline 未启用时，
所有新导入入口返回 `PIPELINE_DISABLED`；历史版本批次只读。

管理员接口提供 `/api/v1/admin/pipeline/overview`、`/pipelines`、阶段时间线、阶段级 retry、
cancel 和 reconcile。响应只包含运维状态，不返回 API key、签名下载 URL 或内部 run token。

### OpenAlex 大批量采集

该子系统与交互搜索、远程导入和解析队列隔离。启用 `HARVEST_ENABLED=true` 后可调用：

需要从“选择某个领域”一直执行到“确认整批解析和索引完成”时，请直接使用
[`OpenAlex 特定领域文献批量采集与解析入库指南`](openalex-batch-harvest-guide.md)。
本节只保留接口契约摘要。

```text
GET  /api/v1/harvest/openalex/taxonomy/search
GET  /api/v1/harvest/openalex/taxonomy/{level}/{id}/children
POST /api/v1/harvest/jobs/estimate
POST /api/v1/harvest/jobs
GET  /api/v1/harvest/jobs
GET  /api/v1/harvest/jobs/{job_id}
GET  /api/v1/harvest/jobs/{job_id}/items
POST /api/v1/harvest/jobs/{job_id}/pause
POST /api/v1/harvest/jobs/{job_id}/resume
POST /api/v1/harvest/jobs/{job_id}/cancel
POST /api/v1/harvest/jobs/{job_id}/retry-failed
POST /api/v1/harvest/jobs/{job_id}/imports
```

创建示例：

```json
{
  "name": "Electrochemistry 2015-2026",
  "scope": {
    "level": "subfield",
    "openalex_id": "2505",
    "include_descendants": true,
    "match_mode": "topics"
  },
  "filters": {
    "year_from": 2015,
    "year_to": 2026,
    "types": ["article", "review"],
    "languages": []
  },
  "billing_mode": "free_credit_only",
  "ingest_mode": "stage_only"
}
```

任务总量不设上限，每页以 `per_page=100` 和 cursor 持久化推进；数据库保留完整 Work
元数据，PDF 与元数据 sidecar 进入 `corpus/openalex/{work_id}/`。状态为
`enumerating | downloading | waiting_quota | paused | completed |
completed_with_errors | cancelled | failed`。活动任务按返回的
`next_poll_after_seconds` 轮询；通常为 2 秒，等待 UTC 免费额度重置时为 30 秒。

下载时不使用 `license`、`oa_status` 作为准入门，但仍执行网络和文件技术校验。
免费 URL 均失败后，只有 `content_urls.pdf`（兼容旧 `content_url`）存在且 Key 的
`daily_remaining_usd` 足够动态单价时，
才调用 Content API。服务不会使用 settings 中的单 Key 执行 Content 下载，Content 必须来自
管理员密钥池，以便执行免费额度预留。

暂存入库需要 `PIPELINE_ENABLED=true`：

```json
{"selection": "all_staged", "item_ids": []}
```

也可设置 `selection=items` 并传入最多 5000 个 `item_ids`。接口创建现有
`ImportBatch`、`ImportBatchItem`、Pipeline root 与 `register` task，并在同一
数据库事务中把 task 标为 `delivery_status=pending`。API 不直接投递 Redis；
Pipeline Dispatcher 负责投递、租约恢复和 missing-job recovery。`literature-ingest`
Worker 从暂存存储读取 PDF，幂等注册 `Paper`，然后进入
`parse → evidence → resource_context → vectorize → index`。

Harvest 条目使用 `staged → registering → registered` 表示 PDF 暂存、Paper 注册中和
Paper 已注册；`registered` 不代表解析或索引已经完成。条目响应同时返回
`import_batch_item_id`、`pipeline_task_id`、`pipeline_status` 与 `pipeline_stage`，
调用方应继续展示 Pipeline 的真实阶段和失败状态。旧的 `imported_items`、
`imported_paper_id`、`importing/imported` 不再返回。

管理员密钥接口为：

```text
GET  /api/v1/admin/harvest/openalex/credentials
POST /api/v1/admin/harvest/openalex/credentials
POST /api/v1/admin/harvest/credentials/{id}/test
POST /api/v1/admin/harvest/credentials/{id}/enable
POST /api/v1/admin/harvest/credentials/{id}/disable
GET  /api/v1/admin/harvest/openalex/quota
```

所有响应只返回别名、Key 后缀、健康状态、免费余额、prepaid 余额、动态单价和重置时间，
永不返回明文 Key。

四层存储边界如下：

1. 远程发现结果只是临时元数据，不是 `Paper`。
2. `/discovery/downloads` 与远程导入的下载阶段只负责暂存。
3. `STORAGE_ROOT` 中的 PDF 与数据库 `Paper` / `ResourceAsset` 是长期规范对象。
4. Evidence 与 Embedding ready 后，论文才成为本地全文可检索、可核验对象。

建议在腾讯 COS 桶侧为 `{COS_PREFIX}/downloads/` 配置 7 天生命周期；应用不会主动删除暂存
对象。许可证信息继续写入元数据 sidecar，但不会阻断下载。

### Embedding 故障恢复（管理员）

`GET /api/v1/admin/embedding-repairs` 返回 Embedding 未就绪或正在恢复的论文，以及健康、待补、
批次进度、错误码和下一次重试时间。管理员可以调用：

- `POST /api/v1/admin/embedding-repairs`，请求体为 `{"paper_ids":["paper_..."]}`，只补齐缺失或
  文本已变化的向量；已健康的向量不会重新调用模型。
- `POST /api/v1/admin/embedding-repairs/stop-and-retry`，对处于 Embedding 阶段的完整处理任务发出
  安全停止请求，并在当前 HTTP 请求结束后启动仅向量恢复任务。

恢复任务在任务层最多自动重试一次；再次失败后保留为 `waiting_manual`，等待管理员修复 Provider
后重新触发。全库 `embedding-rebuild` 仅用于模型切换或全局修复，不应用于单篇故障。

## 16. 给 agent 组的最短导航

如果你是做 agent 接入，通常按下面顺序读文档最省时间：

1. 先读 `README.md` 的“典型使用步骤”和“能力边界”。
2. 再看本文的：
   - 第 2 节“Agent 推荐调用顺序”
   - 第 3 节“横切规则”
   - 第 12 节“Paper Processing Tasks”
   - 第 13 节“Agent Workflows”
3. 若要精确看字段校验，再回到 `/openapi.json`。

最终记住三件事：

- `search` 命中后，最好继续读 `content` / `source` 再引用。
- Pack 输入是 `claims`；Ground 输入是 `draft`。
- paper processing 查 `task_id`；workflow 查 `run_id`。
