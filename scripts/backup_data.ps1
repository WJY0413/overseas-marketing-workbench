$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$envPath = Join-Path $projectRoot ".env"
$dataDir = Join-Path $projectRoot "data"

if (Test-Path $envPath) {
  foreach ($line in Get-Content $envPath) {
    if ($line -match "^\s*WORKBENCH_DATA_DIR\s*=\s*(.+)\s*$") {
      $dataDir = $Matches[1].Trim().Trim('"').Trim("'")
      break
    }
  }
}
if (-not [System.IO.Path]::IsPathRooted($dataDir)) {
  $dataDir = Join-Path $projectRoot $dataDir
}

$resolvedDataDir = Resolve-Path -LiteralPath $dataDir
$backupRoot = Join-Path $resolvedDataDir "backups"
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$backupPath = Join-Path $backupRoot "workbench_data_$stamp"
New-Item -ItemType Directory -Force -Path $backupPath | Out-Null

Get-ChildItem -LiteralPath $resolvedDataDir -Force | Where-Object { $_.Name -ne "backups" } | ForEach-Object {
  Copy-Item -LiteralPath $_.FullName -Destination $backupPath -Recurse -Force
}
Write-Host "Data backup created: $backupPath"
