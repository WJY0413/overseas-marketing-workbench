param(
  [switch]$KeepStaging
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$releaseRoot = Join-Path $projectRoot "releases"
New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$packageName = "bd-email-workbench-lite_clean_$stamp"
$packageDir = Join-Path $releaseRoot $packageName
$zipPath = Join-Path $releaseRoot "$packageName.zip"
New-Item -ItemType Directory -Force -Path $packageDir | Out-Null

$excludedDirs = @(
  ".venv",
  "__pycache__",
  ".pytest_cache",
  "data",
  "reports",
  "backups",
  "releases",
  "code_backups"
)

$excludedFiles = @(
  ".env",
  ".env.production",
  ".env.test",
  ".env.production.example",
  ".env.test.example",
  "*.db",
  "*.sqlite",
  "*.log",
  "*.pyc",
  "AGENTS.md",
  "BUILD_REPORT.md",
  "UPDATE_WORKFLOW.md",
  "CHANGELOG.md",
  "README.md",
  "start_test.cmd"
)

function Test-ExcludedFile {
  param([string]$Name)
  foreach ($pattern in $excludedFiles) {
    if ($Name -like $pattern) {
      return $true
    }
  }
  return $false
}

Get-ChildItem -LiteralPath $projectRoot -Force | ForEach-Object {
  $skip = $excludedDirs -contains $_.Name
  if (-not $skip -and -not $_.PSIsContainer) {
    $skip = Test-ExcludedFile -Name $_.Name
  }
  if (-not $skip) {
    Copy-Item -LiteralPath $_.FullName -Destination $packageDir -Recurse -Force
  }
}

Get-ChildItem -LiteralPath $packageDir -Recurse -Force -Directory | Where-Object {
  $_.Name -in @("__pycache__", ".pytest_cache", ".venv", "data", "reports", "backups", "code_backups", "releases")
} | Sort-Object FullName -Descending | ForEach-Object {
  Remove-Item -LiteralPath $_.FullName -Recurse -Force
}

Get-ChildItem -LiteralPath $packageDir -Recurse -Force -File | Where-Object {
  $_.Name -like "*.pyc" -or
  $_.Name -like "*.log" -or
  $_.Name -like "*.db" -or
  $_.Name -like "*.sqlite" -or
  $_.Name -like "*.db-journal" -or
  $_.Name -like "*.sqlite-journal" -or
  $_.Name -eq ".env" -or
  $_.Name -eq ".secret_key"
} | ForEach-Object {
  Remove-Item -LiteralPath $_.FullName -Force
}

$packageScripts = Join-Path $packageDir "scripts"
if (Test-Path -LiteralPath $packageScripts) {
  Get-ChildItem -LiteralPath $packageScripts -Recurse -Force | Where-Object {
    -not $_.PSIsContainer -and $_.Name -ne "verify_shareable_package.ps1"
  } | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Force
  }
  Get-ChildItem -LiteralPath $packageScripts -Recurse -Force -Directory | Where-Object {
    @(Get-ChildItem -LiteralPath $_.FullName -Force).Count -eq 0
  } | Sort-Object FullName -Descending | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Force
  }
}

$cleanReadme = @"
# BD Email Workbench Lite

This is a clean local share package. It contains app code only and does not include private data, sender secrets, reports, backups, or a Python virtual environment.

## Start

1. Install Python 3.12 if it is not already installed. Python 3.11 and 3.13 are also supported; avoid Python 3.14 for this release.
2. Unzip this package into a normal local folder, for example `bd-email-workbench-lite`.
3. Double-click `start.cmd`.
4. Open `http://127.0.0.1:8000` if the browser does not open automatically.

On first launch, `start.cmd` creates `.env` from `.env.example`, creates `.venv`, installs dependencies, creates a fresh SQLite database, and starts the local app.

## Safe Defaults

- `DRY_RUN_EMAIL=true`
- `ENABLE_OPEN_TRACKING=false`
- No real SMTP password is included.
- No customer database is included.

Configure sender accounts inside the app before any real SMTP sending.

## Signature Setup

Open `Settings` and edit the `Signature module` before sending. The recipient can change sender name, title, company, address, display email, phone numbers, country rules, sender-mailbox rules, and fixed signature images. A `.docx` signature block can also be imported. Use `Signature confirmation` on the workbench draft-generation step to preview the final signature by recipient country and sender mailbox.

## Included Docs

- `SHARE_GUIDE.md`: setup, sending configuration, and common troubleshooting.
- `PACKAGE_MANIFEST.txt`: package creation details.
"@
$cleanReadme | Set-Content -LiteralPath (Join-Path $packageDir "README.md") -Encoding UTF8

$manifestPath = Join-Path $packageDir "PACKAGE_MANIFEST.txt"
$manifest = @(
  "Package: $packageName",
  "Mode: Clean Share",
  "Created: $(Get-Date -Format s)",
  "Includes .env: False",
  "Includes local data: False",
  "Includes saved secret key: False",
  "",
  "Clean share excludes local data, reports, virtualenv, backups, internal maintenance docs, and secrets."
)
$manifest | Set-Content -LiteralPath $manifestPath -Encoding UTF8

if (Test-Path -LiteralPath $zipPath) {
  Remove-Item -LiteralPath $zipPath -Force
}

$packageItems = Get-ChildItem -LiteralPath $packageDir -Force
Compress-Archive -LiteralPath $packageItems.FullName -DestinationPath $zipPath -Force

$verifyScript = Join-Path $projectRoot "scripts\verify_shareable_package.ps1"
if (Test-Path -LiteralPath $verifyScript) {
  & $verifyScript -Path $zipPath
}

if (-not $KeepStaging) {
  Remove-Item -LiteralPath $packageDir -Recurse -Force
}

Write-Host "Clean share package created: $zipPath"
