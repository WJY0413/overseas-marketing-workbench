param(
  [Parameter(Mandatory = $true)]
  [string]$TargetRoot,
  [string]$SourceRoot = "",
  [switch]$Apply,
  [switch]$Yes
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $SourceRoot) {
  $SourceRoot = $projectRoot
}
$SourceRoot = [IO.Path]::GetFullPath($SourceRoot)
$TargetRoot = [IO.Path]::GetFullPath($TargetRoot)

if ($SourceRoot.TrimEnd("\") -eq $TargetRoot.TrimEnd("\")) {
  throw "SourceRoot and TargetRoot must be different."
}
foreach ($root in @($SourceRoot, $TargetRoot)) {
  if (-not (Test-Path -LiteralPath (Join-Path $root "VERSION"))) {
    throw "Workbench VERSION marker is missing: $root"
  }
  if (-not (Test-Path -LiteralPath (Join-Path $root "app\main.py"))) {
    throw "Workbench application marker is missing: $root"
  }
}

$sourceVersion = (Get-Content -LiteralPath (Join-Path $SourceRoot "VERSION") -Raw).Trim()
$targetVersion = (Get-Content -LiteralPath (Join-Path $TargetRoot "VERSION") -Raw).Trim()
$preserveNames = @(
  ".env",
  ".venv",
  "data",
  "reports",
  "backups",
  "code_backups",
  "releases"
)
$copyItems = Get-ChildItem -LiteralPath $SourceRoot -Force | Where-Object {
  $_.Name -notin $preserveNames
}

Write-Host "WORKBENCH_UPDATE: PREVIEW"
Write-Host "Source: $SourceRoot (version $sourceVersion)"
Write-Host "Target: $TargetRoot (version $targetVersion)"
Write-Host "Preserved: $($preserveNames -join ', ')"
Write-Host "Program items to update: $($copyItems.Name -join ', ')"

if (-not $Apply) {
  Write-Host "No files changed. Re-run with -Apply after approving this exact target."
  exit 0
}

if (-not $Yes) {
  $answer = Read-Host "Update this existing Workbench in place? Type YES to continue"
  if ($answer -cne "YES") {
    Write-Host "Cancelled. No files changed."
    exit 1
  }
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path $TargetRoot "code_backups\package-upgrade-before-$stamp"
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

foreach ($item in $copyItems) {
  $existing = Join-Path $TargetRoot $item.Name
  if (Test-Path -LiteralPath $existing) {
    Copy-Item -LiteralPath $existing -Destination $backupRoot -Recurse -Force
  }
}

foreach ($item in $copyItems) {
  Copy-Item -LiteralPath $item.FullName -Destination $TargetRoot -Recurse -Force
}

$updatedVersion = (Get-Content -LiteralPath (Join-Path $TargetRoot "VERSION") -Raw).Trim()
if ($updatedVersion -ne $sourceVersion) {
  throw "Version verification failed after update. Backup: $backupRoot"
}

Write-Host "WORKBENCH_UPDATE: COMPLETE"
Write-Host "Version: $targetVersion -> $updatedVersion"
Write-Host "Rollback backup: $backupRoot"
Write-Host "Preserved local data and credentials. Run start.cmd to refresh dependencies and start the app."
