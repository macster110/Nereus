# Local PostgreSQL cluster for Nereus on Windows (same job as scripts/pg.sh).
# Data lives in .\.pgdata; nothing is installed as a Windows service.
#   scripts\pg.ps1 init | start | stop | reset | psql [args]
#
# Needs PostgreSQL 17 with PostGIS (EDB installer + StackBuilder > PostGIS).
# Set $env:PGBIN if PostgreSQL isn't under C:\Program Files\PostgreSQL\<version>\bin.

param(
    [Parameter(Position = 0)][string]$Command = "",
    [Parameter(ValueFromRemainingArguments = $true)]$Rest
)
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Data = Join-Path $Root ".pgdata"
$Log  = Join-Path $Root ".pg.log"
$Port = if ($env:NEREUS_PGPORT) { $env:NEREUS_PGPORT } else { "5439" }

if ($env:PGBIN) {
    $PgBin = $env:PGBIN
} else {
    $base = "C:\Program Files\PostgreSQL"
    if (-not (Test-Path $base)) { throw "PostgreSQL not found under $base. Install it or set `$env:PGBIN." }
    $ver = Get-ChildItem $base -Directory | Sort-Object { [int]($_.Name -replace '\D', '') } -Descending | Select-Object -First 1
    $PgBin = Join-Path $ver.FullName "bin"
}

function Run([string]$exe, [string[]]$argv) {
    & (Join-Path $PgBin "$exe.exe") @argv
    if ($LASTEXITCODE -ne 0) { throw "$exe failed (exit $LASTEXITCODE)" }
}

switch ($Command) {
    "init" {
        Run initdb @("-D", $Data, "-U", "postgres", "--auth=trust", "--encoding=UTF8", "--no-locale")
        # Same settings as the macOS script, so results are comparable.
        Add-Content -Path (Join-Path $Data "postgresql.conf") -Value @"
port = $Port
listen_addresses = 'localhost'
shared_buffers = 512MB
work_mem = 64MB
maintenance_work_mem = 512MB
max_wal_size = 4GB
"@
        & $PSCommandPath start
        Run createdb @("-h", "localhost", "-p", $Port, "-U", "postgres", "nereus")
        Run psql @("-q", "-h", "localhost", "-p", $Port, "-U", "postgres", "-d", "nereus",
                   "-v", "ON_ERROR_STOP=1", "-f", (Join-Path $Root "sql\001_schema.sql"))
        Write-Host "Database ready: postgresql://postgres@localhost:$Port/nereus"
    }
    "start" {
        # pg_ctl status: exit 0 = running, 3 = not running.
        & (Join-Path $PgBin "pg_ctl.exe") -D $Data status | Out-Null
        if ($LASTEXITCODE -eq 0) { Write-Host "already running" }
        else { Run pg_ctl @("-D", $Data, "-l", $Log, "-w", "start"); Write-Host "started" }
    }
    "stop"  { Run pg_ctl @("-D", $Data, "-w", "stop"); Write-Host "stopped" }
    "reset" {
        if (Test-Path $Data) {
            # Not running is fine here. (Redirecting a native program's stderr
            # under ErrorActionPreference=Stop is fatal in Windows PowerShell 5.1.)
            $ErrorActionPreference = "Continue"
            & (Join-Path $PgBin "pg_ctl.exe") -D $Data -w stop | Out-Null
            $ErrorActionPreference = "Stop"
            Remove-Item -Recurse -Force $Data
        }
        & $PSCommandPath init
    }
    "psql"  { & (Join-Path $PgBin "psql.exe") -h localhost -p $Port -U postgres -d nereus @Rest }
    default { Write-Host "usage: scripts\pg.ps1 init|start|stop|reset|psql"; exit 1 }
}
