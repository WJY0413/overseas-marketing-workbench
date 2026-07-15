$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $root "data"
$envPath = Join-Path $root ".env"
if (Test-Path $envPath) {
  foreach ($line in Get-Content $envPath) {
    if ($line -match "^\s*WORKBENCH_DATA_DIR\s*=\s*(.+)\s*$") {
      $dataDir = $Matches[1].Trim().Trim('"').Trim("'")
      break
    }
  }
}
if (-not [System.IO.Path]::IsPathRooted($dataDir)) {
  $dataDir = Join-Path $root $dataDir
}
$source = Join-Path $dataDir "workbench_test.db"
$backupDir = Join-Path $dataDir "backups\test_db"
$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$target = Join-Path $backupDir "workbench_test_$timestamp.db"

New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

if (!(Test-Path $source)) {
  Write-Host "No test database found at $source"
  exit 0
}

Copy-Item -LiteralPath $source -Destination $target -Force
Write-Host "Test database backup created:"
Write-Host $target
