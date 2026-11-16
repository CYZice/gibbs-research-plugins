# OpenAlex 特定领域文献批量采集与解析入库指南

本文说明如何把某个 OpenAlex `Domain / Field / Subfield / Topic` 下的论文批量下载为 PDF，
暂存到本地或腾讯 COS，再批量送入 Literature KB 完成注册、解析、Evidence 构建、向量化和索引。

运行时接口契约以 `/openapi.json` 为准。接口字段参考见
[`api.md`](api.md)，本地服务启动参考见
[`local-development-agent-guide.md`](local-development-agent-guide.md)。

## 1. 先理解两条独立流水线

```text
OpenAlex 分类
  → 枚举 Works
  → 解析免费 PDF 候选
  → 必要时使用 OpenAlex Content 免费额度
  → staged PDF

staged PDF
  → register
  → parse
  → evidence
  → resource_context
  → vectorize
  → index
  → ready
```

第一条是 Harvest，负责找到论文并暂存 PDF；第二条是 Import Pipeline，负责把暂存 PDF
注册成 `Paper` 并处理成可检索文献。两者状态不要混用：

- `staged`：PDF 已下载并暂存，还没有注册成 `Paper`。
- `registering`：已创建 ImportBatch 和 Pipeline，等待或正在执行 `register`。
- `registered`：已经得到 `paper_id`，但后续解析和索引可能仍在运行。
- ImportBatch item `succeeded`：该论文已完成 `register → ... → index`。
- ImportBatch `completed` 且 `failed_items=0`：本批所有论文已经完整入库。

## 2. 启动前准备

### 2.1 必需配置

后端至少需要：

```dotenv
PIPELINE_ENABLED=true
HARVEST_ENABLED=true

API_AUTH_TOKEN=replace-with-user-token
ADMIN_API_TOKEN=replace-with-admin-token
ADMIN_SECRETS_MASTER_KEY=replace-with-a-fernet-key
```

还需要正常配置：

- PostgreSQL 与 Redis。
- 本地存储或腾讯 COS。
- MinerU 解析服务。
- embedding Provider 和至少一把健康 API Key。
- OpenAlex API Key。

`ADMIN_SECRETS_MASTER_KEY` 用于加密保存在数据库中的 OpenAlex 和模型 API Key。生产环境
不能临时更换该值，否则历史密钥无法解密。

如果目前只有一把 OpenAlex Key，可以暂时使用：

```dotenv
OPENALEX_API_KEY=replace-with-openalex-key
```

生产使用建议在管理控制台集中添加 Key。数据库密钥池存在可用 Key 时，系统会按密钥池额度
调度；单个环境变量 Key 主要用于本地验证和兼容现有部署。

### 2.2 必需 Worker

只下载 PDF 时至少启动：

```powershell
Set-Location backend
uv run python -m app.worker --role maintenance
uv run python -m app.worker --role harvest
```

需要继续解析入库时，还要分别启动：

```powershell
uv run python -m app.worker --role ingest
uv run python -m app.worker --role parse
uv run python -m app.worker --role content
uv run python -m app.worker --role embedding
uv run python -m app.worker --role index
```

每条命令在独立窗口运行。Windows 使用 `SimpleWorker`，单个 Worker 同一时间只处理一个
job；需要并行时可以为同一 role 启动多个进程。生产环境的
`docker-compose.prod.yml` 已定义 `worker-harvest` 和各 Pipeline Worker。

`maintenance` Worker 必须持续运行，它负责 Pipeline Dispatcher、投递租约恢复、
missing-job recovery、Harvest 跨天额度恢复和其他维护任务。

### 2.3 OpenAlex Content CLI

后端依赖固定了：

```text
openalex-official==0.3.3
```

默认 `HARVEST_CLI_ENABLED=true`。系统会在需要 OpenAlex Content API 时自动调用 CLI，
通过 `OPENALEX_API_KEY` 环境变量传入当前租用的 Key；CLI 不可用或执行异常时回退到 HTTP
下载器。使用者不需要手动运行 CLI。

## 3. 使用前端完成采集和入库

### 3.1 添加 OpenAlex Key

1. 打开 Literature KB 前端。
2. 进入“管理控制台”。
3. 输入 `ADMIN_API_TOKEN`。
4. 打开“OpenAlex 采集”。
5. 填写别名和 OpenAlex API Key，点击“添加 Key”。
6. 点击“检测”，确认状态为 `active`，并能看到今日免费余额和重置时间。

系统不会返回或展示 Key 明文，只显示别名和后缀。

### 3.2 选择特定领域

1. 从左侧进入“批量采集”。
2. 搜索领域关键词，例如 `electrochemistry`、`battery`。
3. 可限制搜索层级，也可以同时搜索全部层级。
4. 选择合适的 `Domain / Field / Subfield / Topic`。
5. 可点击下属分类进一步缩小范围。

分类匹配使用 OpenAlex 的高召回 `topics.*.id`：

- 选择 `subfield` 会匹配 Work 的所有 topics 中属于该 subfield 的论文。
- 选择 `topic` 会匹配该 Topic。
- 不只匹配 `primary_topic`，因此会保留跨领域论文。
- `include_descendants=true` 会冻结当前下属分类快照，便于以后审计任务范围。

### 3.3 设置过滤条件并预估

支持：

- 起止年份，均为闭区间。
- OpenAlex Work 类型，例如 `article,review`。
- 语言代码，例如 `en,zh`；留空表示不限制。

先点击“预估数量”，确认范围是否过大，再填写任务名并创建。预估只是 OpenAlex 当前
`meta.count`，实际枚举数量可能随 OpenAlex 数据更新而变化。

任务总量没有应用层上限。系统使用 `per_page=100` 和 cursor 深分页，每页落库后保存
cursor，可以跨进程重启和跨天继续。

### 3.4 观察下载

任务页面重点关注：

- `已枚举`：已写入暂存数据库的 OpenAlex Works 数。
- `PDF 暂存`：已经获得并验证 PDF 的数量。
- `无源`：当前所有候选都没有返回有效 PDF。
- `失败`：内部数据或不可恢复执行错误。
- `免费额度余额`：所有健康 OpenAlex Key 的 daily free credit 合计。

下载顺序为：

1. 已有暂存文件或跨任务重复文献。
2. OpenAlex Work 中的 `best_oa_location`、`primary_location` 和其他 `locations[].pdf_url`。
3. Unpaywall DOI 候选，以及现有 CORE、arXiv 免费来源。
4. OpenAlex Content API 的每日免费额度。

`license` 和 `oa_status` 会保存到元数据，但不参与下载判定。下载器仍会拒绝登录、验证码、
付费墙、403、非公网地址、超时、超限文件和无效 PDF，不会绕过技术访问控制。

当所有 Key 的免费额度耗尽时，任务进入 `waiting_quota`。只要 `maintenance` 和 Harvest
Worker 正常运行，系统会在 OpenAlex 返回的 UTC 重置时间后自动恢复；不消费
`prepaid_remaining_usd`。

### 3.5 批量解析入库

推荐等待 Harvest 任务进入 `completed` 或 `completed_with_errors`，再执行入库：

- “全部暂存入库”：把当前所有 `staged` 条目创建为一个 ImportBatch。
- “导入所选”：只导入勾选的 `staged` 条目。

如果在 Harvest 尚未完成时执行“全部暂存入库”，调用只处理当时已经 `staged` 的 PDF；
之后新下载的 PDF 需要再次执行入库。

超大集合建议每次选择不超过 5000 个 `item_id`，分成多个 ImportBatch。这样可以缩短首次
数据库事务，并让失败重试和进度观察更清晰。

入库后继续观察条目的 Pipeline 阶段：

```text
register → parse → evidence → resource_context → vectorize → index
```

不要把 Harvest 的 `registered` 当作 ready。只有关联 ImportBatch item 为 `succeeded`，
或者整个 ImportBatch 为 `completed` 且 `failed_items=0`，才表示完整解析和索引结束。

## 4. 使用 API 自动化

以下示例使用 PowerShell 7。普通 Harvest API 使用 `API_AUTH_TOKEN`，管理员密钥接口使用
独立的 `ADMIN_API_TOKEN`。

### 4.1 定义调用函数

```powershell
$BaseUrl = "http://127.0.0.1:8000"
$ApiToken = "replace-with-user-token"
$AdminToken = "replace-with-admin-token"

$ApiHeaders = @{ Authorization = "Bearer $ApiToken" }
$AdminHeaders = @{ Authorization = "Bearer $AdminToken" }

function Invoke-LkbApi {
    param(
        [Parameter(Mandatory)] [string] $Method,
        [Parameter(Mandatory)] [string] $Path,
        [object] $Body,
        [hashtable] $Headers = $ApiHeaders
    )

    $request = @{
        Uri = "$BaseUrl$Path"
        Method = $Method
        Headers = $Headers
    }
    if ($null -ne $Body) {
        $request.ContentType = "application/json"
        $request.Body = $Body | ConvertTo-Json -Depth 10
    }
    Invoke-RestMethod @request
}
```

### 4.2 添加并检测 OpenAlex Key

```powershell
$credential = Invoke-LkbApi `
    -Method POST `
    -Path "/api/v1/admin/harvest/openalex/credentials" `
    -Headers $AdminHeaders `
    -Body @{
        alias = "team-primary"
        import os
        api_key = os.environ["OPENALEX_API_KEY"]
        weight = 1.0
    }

Invoke-LkbApi `
    -Method POST `
    -Path "/api/v1/admin/harvest/credentials/$($credential.credential_id)/test" `
    -Headers $AdminHeaders
```

已有 `OPENALEX_API_KEY` 环境变量且只做本地验证时，可以跳过该步骤。

### 4.3 搜索分类

```powershell
$taxonomy = Invoke-LkbApi `
    -Method GET `
    -Path "/api/v1/harvest/openalex/taxonomy/search?q=electrochemistry&level=subfield"

$taxonomy.items |
    Select-Object level, id, display_name, works_count |
    Format-Table
```

把返回的短 ID 原样用于后续请求，不要自行拼接 `https://openalex.org/`。

查看下属 Topic：

```powershell
$ScopeId = "replace-with-subfield-id"
$children = Invoke-LkbApi `
    -Method GET `
    -Path "/api/v1/harvest/openalex/taxonomy/subfield/$ScopeId/children"

$children.items |
    Select-Object level, id, display_name, works_count |
    Format-Table
```

### 4.4 预估并创建任务

```powershell
$scope = @{
    level = "subfield"
    openalex_id = $ScopeId
    include_descendants = $true
    match_mode = "topics"
}
$filters = @{
    year_from = 2015
    year_to = 2026
    types = @("article", "review")
    languages = @()
}

$estimate = Invoke-LkbApi `
    -Method POST `
    -Path "/api/v1/harvest/jobs/estimate" `
    -Body @{
        scope = $scope
        filters = $filters
    }

$estimate.estimated_items

$job = Invoke-LkbApi `
    -Method POST `
    -Path "/api/v1/harvest/jobs" `
    -Body @{
        name = "Electrochemistry 2015-2026"
        scope = $scope
        filters = $filters
        billing_mode = "free_credit_only"
        ingest_mode = "stage_only"
    }

$JobId = $job.job_id
```

### 4.5 轮询下载任务

```powershell
$terminalHarvestStates = @(
    "completed",
    "completed_with_errors",
    "cancelled",
    "failed"
)

do {
    $job = Invoke-LkbApi `
        -Method GET `
        -Path "/api/v1/harvest/jobs/$JobId"

    [PSCustomObject]@{
        status = $job.status
        enumerated = $job.enumerated_items
        staged = $job.staged_items
        no_source = $job.no_source_items
        failed = $job.failed_items
        next_retry_at = $job.next_retry_at
    } | Format-Table

    if ($job.status -notin $terminalHarvestStates) {
        Start-Sleep -Seconds $job.next_poll_after_seconds
    }
} while ($job.status -notin $terminalHarvestStates)
```

`waiting_quota` 可能持续到下一次 UTC 免费额度重置。自动化程序可以停止轮询并在
`next_retry_at` 后继续查询；任务本身由服务端维护任务自动恢复。

分页查看无源条目：

```powershell
$page = Invoke-LkbApi `
    -Method GET `
    -Path "/api/v1/harvest/jobs/$JobId/items?status=no_source&limit=100"

$page.items |
    Select-Object item_id, openalex_id, title, error_code |
    Format-Table
```

接口返回 `next_cursor` 时，下一页追加：

```text
&cursor=<next_cursor>
```

### 4.6 创建 ImportBatch

导入当前所有已暂存 PDF：

```powershell
$import = Invoke-LkbApi `
    -Method POST `
    -Path "/api/v1/harvest/jobs/$JobId/imports" `
    -Body @{
        selection = "all_staged"
        item_ids = @()
    }

$BatchId = $import.batch_id
```

只导入指定条目：

```powershell
$import = Invoke-LkbApi `
    -Method POST `
    -Path "/api/v1/harvest/jobs/$JobId/imports" `
    -Body @{
        selection = "items"
        item_ids = @(
            "harvestitem_replace_1",
            "harvestitem_replace_2"
        )
    }
```

`selection=items` 每次最多传 5000 个 ID，并且只能选择当前状态为 `staged` 的条目。
接口返回 `202` 后，Pipeline task 已经与 ImportBatch 在同一数据库事务中持久化，不依赖
API 进程当场向 Redis 投递。

### 4.7 轮询解析入库

```powershell
$terminalBatchStates = @(
    "completed",
    "completed_with_errors",
    "cancelled",
    "failed"
)

do {
    $batch = Invoke-LkbApi `
        -Method GET `
        -Path "/api/v1/import-batches/$BatchId"

    [PSCustomObject]@{
        status = $batch.status
        total = $batch.total_items
        completed = $batch.completed_items
        failed = $batch.failed_items
    } | Format-Table

    if ($batch.status -notin $terminalBatchStates) {
        Start-Sleep -Seconds 2
    }
} while ($batch.status -notin $terminalBatchStates)
```

查看每篇论文的真实阶段：

```powershell
$items = Invoke-LkbApi `
    -Method GET `
    -Path "/api/v1/import-batches/$BatchId/items?limit=500"

$items.items |
    Select-Object paper_id, status, current_stage, attempts, error_message |
    Format-Table
```

成功判定：

```powershell
if (
    $batch.status -eq "completed" -and
    $batch.completed_items -eq $batch.total_items -and
    $batch.failed_items -eq 0
) {
    "本批 PDF 已全部解析并索引完成。"
}
```

## 5. 暂停、恢复和重试

### 5.1 Harvest 控制

```text
POST /api/v1/harvest/jobs/{job_id}/pause
POST /api/v1/harvest/jobs/{job_id}/resume
POST /api/v1/harvest/jobs/{job_id}/cancel
POST /api/v1/harvest/jobs/{job_id}/retry-failed
```

- `pause`：停止继续枚举和下载，已运行的短任务会自行结束。
- `resume`：从数据库保存的 cursor 和 item 状态继续。
- `cancel`：终止该 Harvest 任务；不会删除已经暂存的 PDF。
- `retry-failed`：重新处理 `failed` 和 `no_source`，适合新增来源或上游恢复后使用。

### 5.2 ImportBatch 重试

```text
POST /api/v1/import-batches/{batch_id}/retry-failed
```

该接口只重试当前 Pipeline V3 批次中的失败阶段，不重新下载 PDF，也不会重复创建已存在的
Paper。容量等待、429 和 Provider 冷却属于背压，不会直接把 Paper 或 Batch 标记失败。

## 6. 常见问题

### 任务一直是 `waiting_quota`

检查：

1. `maintenance` 和 Harvest Worker 是否正常。
2. 管理控制台中是否至少有一把 `active` Key。
3. `/api/v1/admin/harvest/openalex/quota` 的 `next_reset_at`。
4. Key 是否被 OpenAlex 返回 429 或标记为 `daily_exhausted`。

系统只使用 daily free credit，不会自动消费 prepaid。

### `completed_with_errors` 是否表示任务失败

不一定。Harvest 中通常表示部分 Works 没有有效 PDF，即 `no_source_items > 0`；已经
`staged` 的 PDF 仍可正常批量入库。ImportBatch 中则表示部分论文的 Pipeline 失败，应查看
batch items 的 `current_stage` 和 `error_message`。

### 为什么 `registered` 后还不能检索

`registered` 只表示 Paper 已创建。至少要等到 `evidence` 和 `index` 完成才能进行稳定的
关键词检索；语义检索还需要 `vectorize` 完成。以 ImportBatch item `succeeded` 为最终标准。

### 某些超长 Evidence 没有向量

超过 `EMBEDDING_MAX_INPUT_CHARS` 的 representation 会记录
`embedding.status=skipped` 和 `reason=input_too_large`。它不会阻断整篇论文入库，完整
Evidence 和 PostgreSQL 关键词索引仍保留；同一 Evidence 的其他短 representation 仍会向量化。

### Redis 暂时不可用会不会丢导入任务

不会。Import API 先在 PostgreSQL 同一事务中创建 ImportBatch、item、Pipeline root 和
`delivery_status=pending` 的 `register` task。Redis 恢复后，Dispatcher 会继续投递。

### 重复运行同一领域会不会产生重复 Paper

Corpus 层按 OpenAlex Work ID 去重，并使用 DOI、PDF SHA-256 补充识别；注册阶段会创建或
复用既有 Paper。重复任务仍会保留自己的 Harvest 统计和审计记录，但不会故意复制同一
PDF 和 Paper。

## 7. 上线前最小验收

先用一个小 Topic 或较窄年份范围验证：

1. 分类搜索和预估返回正常。
2. 创建任务后 `enumerated_items` 持续增加。
3. 至少 10 篇进入 `staged`。
4. 创建 ImportBatch 后 API 返回 `202`。
5. Pipeline 依次经过 `register / parse / evidence / resource_context / vectorize / index`。
6. ImportBatch 最终 `completed_items=10`、`failed_items=0`。
7. 论文库中能按标题找到这些 Paper。
8. Evidence 关键词检索和语义检索都能命中至少一篇新论文。

小批量验收通过后，再扩大分类范围或取消年份限制。
