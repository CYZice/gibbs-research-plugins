param(
    [string]$PdfPath = "C:\Users\Lenovo\Desktop\10-NN-Non-flammable-Electrolyte-Enables-Li-Metal-Batteries-with-Aggressive-Cathode-Chemistries.pdf",
    [string]$ApiBaseUrl = "http://127.0.0.1:8000",
    [switch]$StartServer,
    [int]$Port = 8000,
    [int]$ParsePollSeconds = 10,
    [int]$ParseMaxPolls = 60,
    [string[]]$Queries = @(
        "non-flammable electrolyte lithium metal batteries",
        "aggressive cathode chemistries",
        "what electrolyte enables lithium metal batteries"
    )
)

$ErrorActionPreference = "Stop"

function Load-DotEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $name, $value = $trimmed.Split("=", 2)
        $name = $name.Trim()
        $value = $value.Trim().Trim('"').Trim("'")
        if ($name) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function Require-Env {
    param([string]$Name)
    if (-not [Environment]::GetEnvironmentVariable($Name, "Process")) {
        throw "Missing required environment variable: $Name"
    }
}

function Invoke-JsonPost {
    param(
        [string]$Uri,
        [hashtable]$Body,
        [int]$MaxAttempts = 3
    )
    $json = $Body | ConvertTo-Json -Depth 12
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            return Invoke-RestMethod `
                -Uri $Uri `
                -Method Post `
                -ContentType "application/json" `
                -Body $json
        } catch {
            if ($attempt -eq $MaxAttempts) {
                throw
            }
            Write-Host "retry_json_post attempt=$attempt uri=$Uri reason=$($_.Exception.Message)"
            Start-Sleep -Seconds 5
        }
    }
}

function Assert-True {
    param(
        [bool]$Condition,
        [string]$Message
    )
    if (-not $Condition) {
        throw $Message
    }
}

function Get-ItemCount {
    param($Value)
    if ($null -eq $Value) {
        return 0
    }
    if ($Value -is [array]) {
        return $Value.Count
    }
    return 1
}

function Wait-Health {
    param([string]$BaseUrl)
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $health = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get
            if ($health.status -eq "ok") {
                return
            }
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    throw "Backend did not become healthy: $BaseUrl"
}

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message =="
}

function Show-PackDiagnostics {
    param($Pack, $PackItem)
    $warningCount = Get-ItemCount $Pack.warnings
    if ($warningCount -gt 0) {
        Write-Host "pack_warnings=$($Pack.warnings -join ',')"
    } else {
        Write-Host "pack_warnings=<none>"
    }
    Write-Host "pack_coverage=$($Pack.coverage | ConvertTo-Json -Compress -Depth 6)"
    Write-Host "pack_summary=$($PackItem.usable_summary.Substring(0, [Math]::Min(240, $PackItem.usable_summary.Length)))"
}

function Show-ModelLogs {
    param([string[]]$Paths)
    $patterns = @(
        "CONTEXT_PACK_MODEL_",
        "AGENT_SUPPORT_TYPE_DECISION"
    )
    Write-Step "Model/support-type diagnostic logs"
    $found = $false
    foreach ($path in $Paths) {
        if (-not (Test-Path -LiteralPath $path)) {
            continue
        }
        $matches = Select-String -LiteralPath $path -Pattern $patterns -SimpleMatch
        foreach ($match in $matches) {
            $found = $true
            Write-Host "$([IO.Path]::GetFileName($path)):$($match.LineNumber): $($match.Line)"
        }
    }
    if (-not $found) {
        Write-Host "No model/support-type diagnostic logs found."
    }
}

$backendDir = Split-Path -Parent $PSScriptRoot
Load-DotEnv (Join-Path $backendDir ".env")

if (-not (Test-Path -LiteralPath $PdfPath)) {
    throw "PDF does not exist: $PdfPath"
}

Require-Env "MINERU_API_TOKEN"

$serverProcess = $null
$stdoutLog = Join-Path $backendDir "smoke_uvicorn_stdout.log"
$stderrLog = Join-Path $backendDir "smoke_uvicorn_stderr.log"

try {
    if ($StartServer) {
        Remove-Item -LiteralPath $stdoutLog, $stderrLog -Force -ErrorAction SilentlyContinue
        $serverProcess = Start-Process `
            -FilePath "python" `
            -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$Port") `
            -WorkingDirectory $backendDir `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -PassThru
        $ApiBaseUrl = "http://127.0.0.1:$Port"
    }

    Wait-Health $ApiBaseUrl

    Write-Step "Uploading PDF"
    $ingest = Invoke-RestMethod `
        -Uri "$ApiBaseUrl/papers/ingest" `
        -Method Post `
        -Form @{ title = [IO.Path]::GetFileNameWithoutExtension($PdfPath); file = Get-Item $PdfPath }
    $paperId = $ingest.paper_id
    Assert-True ([bool]$paperId) "Ingest did not return paper_id."
    Write-Host "paper_id=$paperId"

    Write-Step "Submitting MinerU parse"
    $parse = Invoke-RestMethod -Uri "$ApiBaseUrl/papers/$paperId/parse" -Method Post
    Write-Host "task_id=$($parse.task_id) mineru_state=$($parse.mineru_state)"

    for ($i = 0; $i -lt $ParseMaxPolls; $i++) {
        Start-Sleep -Seconds $ParsePollSeconds
        $poll = Invoke-RestMethod -Uri "$ApiBaseUrl/papers/$paperId/parse/poll" -Method Post
        Write-Host "poll=$($i + 1) status=$($poll.status) stage=$($poll.stage) mineru_state=$($poll.mineru_state)"

        if ($poll.status -eq "failed" -or $poll.mineru_state -eq "failed") {
            throw "MinerU parse failed: $($poll.error_message)"
        }
        if ($poll.mineru_state -eq "done") {
            break
        }
        if ($i -eq ($ParseMaxPolls - 1)) {
            throw "MinerU parse did not finish within poll limit."
        }
    }

    Write-Step "Building evidence"
    $evidence = Invoke-RestMethod -Uri "$ApiBaseUrl/papers/$paperId/evidence/build" -Method Post
    Write-Host "evidence_count=$($evidence.evidence_count)"
    Assert-True ($evidence.evidence_count -gt 0) "Evidence build returned no evidence units."

    Write-Step "Building embeddings"
    $embeddings = Invoke-RestMethod -Uri "$ApiBaseUrl/papers/$paperId/embeddings/build" -Method Post
    Write-Host "embedded_count=$($embeddings.embedded_count) skipped_count=$($embeddings.skipped_count)"
    Assert-True ($embeddings.embedded_count -gt 0) "Embedding build returned no new vectors."

    foreach ($query in $Queries) {
        Write-Step "Legacy search: $query"
        $legacySearch = Invoke-JsonPost -Uri "$ApiBaseUrl/evidence/search" -Body @{
            query = $query
            top_k = 5
            filters = @{
                paper_ids = @($paperId)
            }
        }
        Assert-True ((Get-ItemCount $legacySearch.evidence) -gt 0) "Legacy search returned no evidence for query: $query"
        $legacyTop = @($legacySearch.evidence)[0]
        Write-Host "legacy_top evidence_id=$($legacyTop.evidence_id) page=$($legacyTop.page) score=$($legacyTop.score)"

        Write-Step "Agent search/content/pack/source: $query"
        $agentSearch = Invoke-JsonPost -Uri "$ApiBaseUrl/api/v1/agent/evidence/search" -Body @{
            query = $query
            paper_ids = @($paperId)
            top_k = 5
            max_per_paper = 2
            diversify_by_paper = $true
            include = @{
                paper_metadata = $true
                score_breakdown = $true
                assets = $true
            }
        }
        Assert-True ($agentSearch.schema_version -eq "agent_search.v1") "Unexpected agent search schema_version."
        Assert-True ((Get-ItemCount $agentSearch.hits) -gt 0) "Agent search returned no hits for query: $query"
        $hit = @($agentSearch.hits)[0]
        Write-Host "agent_top evidence_id=$($hit.evidence_id) page=$($hit.page) score=$($hit.score) confidence=$($hit.confidence)"
        if ((Get-ItemCount $agentSearch.warnings) -gt 0) {
            Write-Host "agent_search_warnings=$($agentSearch.warnings -join ',')"
        }

        $content = Invoke-RestMethod -Uri "$ApiBaseUrl$($hit.content_url)" -Method Get
        Assert-True ($content.schema_version -eq "agent_content.v1") "Unexpected content schema_version."
        Assert-True ([bool]$content.current.quote) "Agent content current.quote is empty."
        Assert-True ($content.context_text.Contains($content.current.quote)) "context_text does not contain current.quote."
        Write-Host "content_quote=$($content.current.quote.Substring(0, [Math]::Min(180, $content.current.quote.Length)))"

        $pack = Invoke-JsonPost -Uri "$ApiBaseUrl/api/v1/agent/evidence_pack" -Body @{
            mode = "query"
            query = $query
            top_k = 5
            filters = @{
                paper_ids = @($paperId)
            }
            need_summary = $true
            need_references = $true
            max_tokens = 2000
        }
        Assert-True ($pack.schema_version -eq "evidence_pack.v2") "Unexpected evidence pack schema_version."
        Assert-True ((Get-ItemCount $pack.items) -gt 0) "Evidence pack returned no items for query: $query"
        $packItem = @($pack.items)[0]
        Assert-True ($packItem.claim_id -eq "query_001") "Query mode did not use claim_id=query_001."
        Assert-True ($null -eq $packItem.claim) "Query mode claim should be null."
        Assert-True ($pack.evidence_markdown.Contains($packItem.citation_key)) "evidence_markdown does not contain citation key."
        Write-Host "pack_top citation_key=$($packItem.citation_key) support_type=$($packItem.support_type) verified=$($packItem.verified)"
        Show-PackDiagnostics -Pack $pack -PackItem $packItem

        $source = Invoke-RestMethod -Uri "$ApiBaseUrl$($hit.source_url)" -Method Get
        Assert-True ($source.schema_version -eq "source.v1") "Unexpected source schema_version."
        Assert-True ($source.evidence_id -eq $hit.evidence_id) "Source evidence_id does not match search hit."
        Write-Host "source page=$($source.page) bbox=$($source.bbox -join ',')"

        if ((Get-ItemCount $content.asset_refs) -gt 0) {
            $asset = @($content.asset_refs)[0]
            Assert-True ($asset.download_available -eq $false) "Temporary asset_ref should not be downloadable yet."
            $resource = Invoke-RestMethod -Uri "$ApiBaseUrl$($asset.resource_api)" -Method Get
            Assert-True ($resource.schema_version -eq "resource.v1") "Unexpected resource schema_version."
            Write-Host "resource asset_id=$($resource.asset_id) download_url=$($resource.download_url)"
        }
    }

    Write-Step "Smoke test passed"
    Write-Host "paper_id=$paperId"
} finally {
    if ($serverProcess -and -not $serverProcess.HasExited) {
        Stop-Process -Id $serverProcess.Id -Force
    }
    if ($StartServer) {
        Start-Sleep -Milliseconds 500
        Show-ModelLogs -Paths @($stdoutLog, $stderrLog)
    }
}
