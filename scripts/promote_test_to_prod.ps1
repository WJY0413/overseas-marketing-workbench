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
$testDb = Join-Path $dataDir "workbench_test.db"
$prodDb = Join-Path $dataDir "workbench_prod.db"

if (!(Test-Path $testDb)) {
  throw "Test database not found: $testDb"
}

& (Join-Path $PSScriptRoot "backup_prod_db.ps1")

Copy-Item -LiteralPath $testDb -Destination $prodDb -Force
Write-Host "Test database promoted to production:"
Write-Host $prodDb
