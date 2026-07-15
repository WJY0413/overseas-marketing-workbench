param(
  [Parameter(Mandatory = $true)]
  [string]$BackupFile
)

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
$backupRoot = (Resolve-Path (Join-Path $dataDir "backups\prod_db")).Path
$resolvedBackup = (Resolve-Path $BackupFile).Path
$prodDb = Join-Path $dataDir "workbench_prod.db"

if (!$resolvedBackup.StartsWith($backupRoot)) {
  throw "Backup file must be inside $backupRoot"
}

& (Join-Path $PSScriptRoot "backup_prod_db.ps1")

Copy-Item -LiteralPath $resolvedBackup -Destination $prodDb -Force
Write-Host "Production database rolled back from:"
Write-Host $resolvedBackup
