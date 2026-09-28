# A private Postgres cluster for this project, run from the installed PostgreSQL binaries.
# No admin rights, no superuser password needed, and fully separate from any other Postgres
# server on the machine (own data directory, own port, localhost only).
#
#   powershell -ExecutionPolicy Bypass -File scripts\local_pg.ps1 init    # once
#   powershell -ExecutionPolicy Bypass -File scripts\local_pg.ps1 start
#   powershell -ExecutionPolicy Bypass -File scripts\local_pg.ps1 stop
#   powershell -ExecutionPolicy Bypass -File scripts\local_pg.ps1 status

param(
    [Parameter(Mandatory = $true)][ValidateSet("init", "start", "stop", "status")][string]$Action,
    [string]$PgBin = "C:\Program Files\PostgreSQL\16\bin",
    [int]$Port = 5434,
    [string]$DataDir = (Join-Path $env:LOCALAPPDATA "agent-orchestrator\pgdata")
)

$ErrorActionPreference = "Stop"
$pgCtl = Join-Path $PgBin "pg_ctl.exe"
$logFile = Join-Path (Split-Path $DataDir -Parent) "postgres.log"

function Invoke-Psql([string]$Sql, [string]$Db = "postgres") {
    & (Join-Path $PgBin "psql.exe") -h localhost -p $Port -U postgres -d $Db -v ON_ERROR_STOP=1 -q -c $Sql
    if ($LASTEXITCODE -ne 0) { throw "psql failed: $Sql" }
}

# pg_ctl must run in its own (hidden) console: if it inherits ours, the server's child
# processes fail with 0xC0000142 once this shell exits.
function Start-Db {
    Start-Process -FilePath $pgCtl -WindowStyle Hidden -ArgumentList @("start", "-D", "`"$DataDir`"", "-l", "`"$logFile`"")
    for ($i = 0; $i -lt 30; $i++) {
        & (Join-Path $PgBin "pg_isready.exe") -h localhost -p $Port -q
        if ($LASTEXITCODE -eq 0) { return }
        Start-Sleep -Seconds 1
    }
    throw "Postgres did not become ready; see $logFile"
}

switch ($Action) {
    "init" {
        if (Test-Path (Join-Path $DataDir "PG_VERSION")) { Write-Host "Already initialized: $DataDir"; break }
        New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
        # trust auth is acceptable only because the server listens on localhost only.
        & (Join-Path $PgBin "initdb.exe") -D $DataDir -U postgres -A trust -E UTF8 --no-locale | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "initdb failed" }
        Add-Content -Path (Join-Path $DataDir "postgresql.conf") -Value @(
            "listen_addresses = 'localhost'",
            "port = $Port",
            "max_connections = 200"
        )
        Start-Db
        Invoke-Psql "CREATE ROLE orchestrator LOGIN PASSWORD 'orchestrator';"
        Invoke-Psql "CREATE DATABASE agent_orchestrator OWNER orchestrator;"
        Invoke-Psql "CREATE DATABASE agent_orchestrator_test OWNER orchestrator;"

        $envFile = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
        $existing = @()
        if (Test-Path $envFile) {
            $existing = Get-Content $envFile | Where-Object { $_ -notmatch "^(DATABASE_URL|TEST_DATABASE_URL|LIBPQ_DIR)=" }
        }
        $lines = $existing + @(
            "DATABASE_URL=postgresql://orchestrator:orchestrator@localhost:$Port/agent_orchestrator",
            "TEST_DATABASE_URL=postgresql://orchestrator:orchestrator@localhost:$Port/agent_orchestrator_test",
            "LIBPQ_DIR=$PgBin"
        )
        Set-Content -Path $envFile -Value $lines -Encoding ascii
        Write-Host "Initialized $DataDir on port $Port; updated .env"
    }
    "start" { Start-Db }
    "stop" { & $pgCtl -D $DataDir -m fast -w stop }
    "status" { & $pgCtl -D $DataDir status }
}
