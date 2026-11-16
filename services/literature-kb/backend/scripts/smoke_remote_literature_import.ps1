param(
    [string]$ApiBase = "http://127.0.0.1:8000",
    [string]$ApiToken = "",
    [string]$Query = "retrieval augmented generation",
    [string]$ExternalId = "",
    [string]$ExternalSource = "openalex",
    [string[]]$Sources = @("openalex", "arxiv"),
    [int]$TimeoutMinutes = 30,
    [switch]$AllowLocalStaging,
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"
$ApiBase = $ApiBase.TrimEnd("/")
$headers = @{}
if ($ApiToken) { $headers.Authorization = "Bearer $ApiToken" }

function Invoke-Api([string]$Method, [string]$Path, $Body = $null) {
    $arguments = @{ Method = $Method; Uri = "$ApiBase$Path"; Headers = $headers }
    if ($null -ne $Body) {
        $arguments.ContentType = "application/json"
        $arguments.Body = $Body | ConvertTo-Json -Depth 12 -Compress
    }
    Invoke-RestMethod @arguments
}

function Wait-Import([string]$BatchId) {
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    $last = ""
    do {
        $batch = Invoke-Api GET "/api/v1/import-batches/$BatchId"
        $item = $batch.items | Select-Object -First 1
        $current = "$($batch.status)/$($item.status)/$($item.details.stage)"
        if ($current -ne $last) {
            Write-Host "  $current"
            $last = $current
        }
        if ($batch.status -in @("succeeded", "partial_failed", "failed")) {
            return $batch
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Import batch $BatchId did not finish within $TimeoutMinutes minute(s)."
}

Write-Host "[1/6] Preflight"
$schema = Invoke-RestMethod -Uri "$ApiBase/openapi.json"
if (-not $schema.paths.'/api/v1/literature/search' -or
    -not $schema.paths.'/api/v1/literature/imports') {
    throw "The running API is stale. Restart the API and workers from this branch first."
}
$health = Invoke-Api GET "/api/v1/system/health"
$storage = $health.components | Where-Object name -eq "discovery_storage" | Select-Object -First 1
if (-not $storage) { throw "Health response does not expose discovery_storage." }
if (-not $AllowLocalStaging -and $storage.detail.provider -ne "cos") {
    throw "COS staging is required. Configure DISCOVERY_STORAGE_PROVIDER=cos or pass -AllowLocalStaging."
}
if ($storage.status -ne "healthy") {
    throw "Discovery storage is not healthy: $($storage.status)."
}
if ($PreflightOnly) {
    Write-Host "Preflight passed. No paper was downloaded or imported."
    exit 0
}

Write-Host "[2/6] Select an allowed remote paper"
$selected = $null
if ($ExternalId) {
    $selected = [pscustomobject]@{
        remote = [pscustomobject]@{ source = $ExternalSource; external_id = $ExternalId; license_policy = "allowed" }
        remote_options = @([pscustomobject]@{ source = $ExternalSource; external_id = $ExternalId; license_policy = "allowed"; downloadable = $true })
        metadata = [pscustomobject]@{ title = $ExternalId; doi = $null }
    }
} else {
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $search = Invoke-Api POST "/api/v1/literature/search" @{
        query = $Query
        search_mode = "lexical"
        sources = @("local") + $Sources
        filters = @{ open_access_only = $true }
        page_size = 50
        cursor = $null
    }
    $watch.Stop()
    foreach ($remoteStatus in $search.source_status | Where-Object source -ne "local") {
        Write-Host "  $($remoteStatus.source)=$($remoteStatus.status), latency=$($remoteStatus.latency_ms)ms"
    }
    Write-Host "  total=$($watch.ElapsedMilliseconds)ms"
    $selected = $search.items | Where-Object {
        @($_.remote_options | Where-Object license_policy -eq "allowed").Count -gt 0 -and
        -not $_.local_paper
    } | Select-Object -First 1
    if (-not $selected) {
        throw "No new license_policy=allowed paper was found. Try another -Query or provide -ExternalId."
    }
}
$externalId = $selected.remote.external_id
Write-Host "  $externalId | $($selected.metadata.doi) | $($selected.metadata.title)"
$options = @($selected.remote_options | ForEach-Object {
    @{ source = $_.source; external_id = $_.external_id }
})
$importItem = @{
    options = $options
    preferred_source = $null
    license_acknowledged = $false
}

Write-Host "[3/6] Import and wait for complete processing"
$submission = Invoke-Api POST "/api/v1/literature/imports" @{
    items = @($importItem)
}
$batch = Wait-Import $submission.batch_id
$item = $batch.items | Select-Object -First 1
if ($batch.status -ne "succeeded" -or -not $item.paper_id) {
    throw "Import failed at stage '$($item.details.stage)': $($item.error_message)"
}
$paperId = $item.paper_id
if (-not $item.details.staged_storage_key) {
    throw "Import succeeded without a staged storage key; COS recovery was not exercised."
}
if (-not $item.details.selected_source -or -not @($item.details.source_attempts).Count) {
    throw "Import succeeded without selected_source/source_attempts diagnostics."
}
$selectedAttempt = $item.details.source_attempts | Where-Object {
    $_.source -eq $item.details.selected_source -and $_.status -eq "succeeded"
} | Select-Object -First 1
if (-not $selectedAttempt) {
    throw "selected_source '$($item.details.selected_source)' has no successful source_attempt."
}

Write-Host "[4/6] Verify Paper, Evidence, Embedding and PDF source"
$paper = Invoke-Api GET "/api/v1/papers/$paperId"
if (-not $paper.capabilities.ai_ready) { throw "Paper $paperId is not AI-ready." }
$evidence = Invoke-Api GET "/api/v1/papers/$paperId/evidence?page=1&page_size=5"
$firstEvidence = $evidence.items | Select-Object -First 1
if (-not $firstEvidence.evidence_id) { throw "Paper $paperId has no searchable Evidence." }
$source = Invoke-Api GET "/api/v1/evidence/$($firstEvidence.evidence_id)/source"
if (-not $source.pdf_asset_id) { throw "Evidence cannot be traced back to the imported PDF." }

Write-Host "[5/6] Verify local search"
$local = Invoke-Api POST "/api/v1/papers/search" @{
    query = $(if ($paper.doi) { $paper.doi } else { $paper.title })
    search_mode = "lexical"
    filters = @{}
    page = 1
    page_size = 20
}
if ($paperId -notin @($local.items.paper_id)) {
    throw "Imported paper $paperId was not returned by local search."
}

Write-Host "[6/6] Verify idempotent re-import"
$repeatSubmission = Invoke-Api POST "/api/v1/literature/imports" @{
    items = @($importItem)
}
$repeat = Wait-Import $repeatSubmission.batch_id
$repeatItem = $repeat.items | Select-Object -First 1
if ($repeat.status -ne "succeeded" -or $repeatItem.paper_id -ne $paperId) {
    throw "Idempotency failed: first=$paperId, repeat=$($repeatItem.paper_id)."
}

Write-Host "PASS: remote literature import is searchable, traceable and idempotent."
Write-Host "paper_id=$paperId source=$($item.details.selected_source) external_id=$externalId staged_key=$($item.details.staged_storage_key)"
