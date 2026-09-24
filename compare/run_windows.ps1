# One-shot Nereus (SQL) vs Tethys (XML) comparison on Windows.
#
#   1. Start Tethys as usual (double-click databases\demodb\tethys.bat).
#   2. In PowerShell, from the Nereus folder:
#        powershell -ExecutionPolicy Bypass -File compare\run_windows.ps1 -TethysDb C:\path\to\Tethys\databases\demodb
#
# Sets up a Python environment and a local PostgreSQL cluster the first time,
# then runs python -m compare.run. Any extra arguments are passed straight to
# compare.run (e.g. -Extra "--questions","q4_effort" or "--skip-download").
# With several -Extra values, run via -Command, not -File: -File passes
# "a","b" through as one argument.
#   powershell -ExecutionPolicy Bypass -Command "& .\compare\run_windows.ps1 -Extra @('--skip-download','--skip-build')"

param(
    [string]$Tethys = "http://localhost:9779",
    [string]$TethysDb = "",           # e.g. C:\Tethys\databases\demodb (disk use + XSD)
    [switch]$NoUpload,                # skip the write tests (they add, then remove, NEREUSBENCH_* docs)
    [switch]$Quick,                   # 2 repeats, 60 s budget: a fast smoke test
    [string[]]$Extra = @()
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# ---- Python 3.10+ (not the Python 3.9 bundled with Tethys)
$Py = $null
$ErrorActionPreference = "Continue"   # py prints to stderr for missing versions
foreach ($v in @("3.13", "3.12", "3.11", "3.10")) {
    & py "-$v" -c "import sys" *> $null
    if ($LASTEXITCODE -eq 0) { $Py = @("py", "-$v"); break }
}
$ErrorActionPreference = "Stop"
if (-not $Py) { throw "Python 3.10 or newer is needed (python.org installer, tick 'py launcher')." }

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "Creating Python environment (.venv)..."
    & $Py[0] $Py[1] -m venv .venv
    & $VenvPy -m pip install --quiet --upgrade pip
    & $VenvPy -m pip install --quiet -r requirements.txt -r compare\requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

# ---- PostgreSQL cluster
if (-not (Test-Path (Join-Path $Root ".pgdata"))) {
    Write-Host "Creating PostgreSQL cluster (.pgdata, port 5439)..."
    & (Join-Path $Root "scripts\pg.ps1") init
} else {
    & (Join-Path $Root "scripts\pg.ps1") start
}

# ---- Tethys reachable?
try {
    $ping = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 "$Tethys/Tethys/ping"
} catch { throw "Tethys is not answering at $Tethys. Start it with tethys.bat first." }

# ---- Run
$argsList = @("-m", "compare.run", "--tethys", $Tethys)
if ($TethysDb) {
    $argsList += @("--tethys-db", $TethysDb)
    $xsd = Join-Path $TethysDb "lib\schema\tethys.xsd"
    if (Test-Path $xsd) { $argsList += @("--xsd", $xsd) }
}
if ($NoUpload) { $argsList += @("--skip-upload") }
if ($Quick) {
    $argsList += @("--repeats", "2", "--budget", "60",
                   "--upload-sets", "3", "--upload-size", "5000",
                   "--append-base", "5000", "--append-batches", "3", "--append-size", "500")
}
$argsList += $Extra

Write-Host "Running: python $($argsList -join ' ')"
& $VenvPy @argsList
