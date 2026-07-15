$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
}
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) {
        python -m venv .venv
    }
    if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
        throw "Unable to create .venv. Install Python 3.11 or newer and retry."
    }
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
}

& ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
