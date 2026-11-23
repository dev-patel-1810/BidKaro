param(
    [int]$Port = 55432,
    [string]$DataDir = (Join-Path $PSScriptRoot "..\.data\pg"),
    [string]$PgBin = "E:\postgres\bin"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path (Join-Path $PgBin "initdb.exe"))) {
    $pgCtl = Get-Command pg_ctl -ErrorAction SilentlyContinue
    if (-not $pgCtl) {
        throw "Postgres binaries not found. Set -PgBin to the folder containing initdb.exe and pg_ctl.exe."
    }
    $PgBin = Split-Path $pgCtl.Source -Parent
}

New-Item -ItemType Directory -Force (Split-Path $DataDir -Parent) | Out-Null

if (-not (Test-Path (Join-Path $DataDir "PG_VERSION"))) {
    Write-Host "Initialising Postgres cluster in $DataDir ..."
    & (Join-Path $PgBin "initdb.exe") -D $DataDir -U postgres -A trust --encoding=UTF8
    if ($LASTEXITCODE -ne 0) { throw "initdb failed." }
}

& (Join-Path $PgBin "pg_ctl.exe") -D $DataDir status *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Starting Postgres on 127.0.0.1:$Port ..."
    & (Join-Path $PgBin "pg_ctl.exe") -D $DataDir -o "-p $Port -h 127.0.0.1" `
        -l (Join-Path $DataDir "..\pg.log") -w start
    if ($LASTEXITCODE -ne 0) { throw "pg_ctl start failed." }
}

foreach ($db in @("bidkaro_v2", "bidkaro_v2_test")) {
    & (Join-Path $PgBin "createdb.exe") -w -h 127.0.0.1 -p $Port -U postgres $db 2>$null
}

Write-Host "Postgres is ready on 127.0.0.1:$Port."
Write-Host "Next: .\.venv\Scripts\python.exe -m alembic upgrade head"
