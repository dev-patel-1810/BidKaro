param(
    [int]$Port = 8000,
    [switch]$Reload
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Create the virtual environment first: python -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}

Push-Location $root
try {
    $args = @("-m", "uvicorn", "app.main:app", "--port", "$Port")
    if ($Reload) { $args += "--reload" }
    & $python @args
}
finally {
    Pop-Location
}
