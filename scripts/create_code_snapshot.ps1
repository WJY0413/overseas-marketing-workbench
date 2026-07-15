param(
  [string]$Version = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Version)) {
  $Version = (Get-Content (Join-Path $root "VERSION") -TotalCount 1).Trim()
}

$target = Join-Path $root "releases\v$Version"
New-Item -ItemType Directory -Force -Path $target | Out-Null

$excludeDirs = @(".venv", "data", "reports", "backups", "releases", "__pycache__", ".pytest_cache")
$excludeFiles = @(".env", ".env.production", ".env.test", "*.db", "*.sqlite", "*.pyc")

robocopy $root $target /E /XD $excludeDirs /XF $excludeFiles | Out-Null

Write-Host "Code snapshot created:"
Write-Host $target
