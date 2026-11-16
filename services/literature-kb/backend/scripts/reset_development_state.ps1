param(
    [string]$ComposeFile = "..\docker-compose.yml",
    [string]$StorageRoot = "..\storage",
    [switch]$ConfirmReset
)

$ErrorActionPreference = "Stop"
$backendRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $backendRoot
$composePath = [IO.Path]::GetFullPath((Join-Path $backendRoot $ComposeFile))
$storagePath = [IO.Path]::GetFullPath((Join-Path $backendRoot $StorageRoot))

if (-not $storagePath.StartsWith($workspaceRoot)) { throw "Storage root must stay inside the workspace." }
if (-not $ConfirmReset) { throw "Refusing to reset development state without -ConfirmReset." }

Write-Host "Resetting development database schema and Redis queue."
Push-Location $backendRoot
try {
    python -m alembic downgrade base
    python -m alembic upgrade head
} finally {
    Pop-Location
}

docker compose -f $composePath exec -T redis redis-cli FLUSHDB | Out-Null

foreach ($name in @("pdfs", "parsed", "assets", ".reupload-staging")) {
    $target = Join-Path $storagePath $name
    if (Test-Path -LiteralPath $target) {
        $resolved = (Resolve-Path -LiteralPath $target).Path
        if (-not $resolved.StartsWith($storagePath)) { throw "Refusing to remove outside storage root: $resolved" }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
    if ($name -ne ".reupload-staging") {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        New-Item -ItemType File -Path (Join-Path $target ".gitkeep") -Force | Out-Null
    }
}

Write-Host "Development state reset complete. Backups under storage/backups were preserved."
