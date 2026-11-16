param([switch]$KeepRunning)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$compose = Join-Path $root "docker-compose.test.yml"
$project = "literature-kb-integration"
$postgresDsn = "postgresql://literature_test:literature_test@127.0.0.1:15433/literature_kb_test"
$redisUrl = "redis://127.0.0.1:16380/0"

function Wait-Services {
    $deadline = (Get-Date).AddSeconds(60)
    do {
        docker compose -p $project -f $compose ps | Out-Null
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "SilentlyContinue"
        python -c "import psycopg,redis; psycopg.connect('$postgresDsn',connect_timeout=1).close(); assert redis.Redis.from_url('$redisUrl',socket_connect_timeout=1).ping()" 2>$null
        $ready = $LASTEXITCODE -eq 0
        $ErrorActionPreference = $previousPreference
        if ($ready) { return }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw "Local integration services did not become ready."
}

try {
    docker compose -p $project -f $compose up -d
    Wait-Services
    $env:RUN_REAL_INFRA_TESTS = "1"
    $env:DATABASE_URL = "postgresql+psycopg://literature_test:literature_test@127.0.0.1:15433/literature_kb_test"
    Push-Location (Join-Path $root "backend")
    try {
        python -m alembic upgrade head
        if ($LASTEXITCODE -ne 0) { throw "Alembic integration migration failed." }
        python -m pytest tests/integration/test_local_infrastructure.py -q -m integration
        if ($LASTEXITCODE -ne 0) { throw "Local integration pytest failed." }
    }
    finally { Pop-Location }

    docker compose -p $project -f $compose stop redis-test
    python -c "import redis,sys; r=redis.Redis.from_url('$redisUrl',socket_connect_timeout=1,socket_timeout=1);`ntry:r.ping();sys.exit(1)`nexcept redis.RedisError:sys.exit(0)"
    if ($LASTEXITCODE -ne 0) { throw "Redis outage was not observed." }
    docker compose -p $project -f $compose start redis-test
    Wait-Services

    docker compose -p $project -f $compose stop postgres-test
    python -c "import psycopg,sys;`ntry:psycopg.connect('$postgresDsn',connect_timeout=1);sys.exit(1)`nexcept psycopg.OperationalError:sys.exit(0)"
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL outage was not observed." }
    docker compose -p $project -f $compose start postgres-test
    Wait-Services
}
finally {
    Remove-Item Env:RUN_REAL_INFRA_TESTS -ErrorAction SilentlyContinue
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
    if (-not $KeepRunning) { docker compose -p $project -f $compose down -v }
}
