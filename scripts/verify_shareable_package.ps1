param(
  [Parameter(Mandatory = $true)]
  [string]$Path
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Path)) {
  throw "Package path not found: $Path"
}

Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-PackageEntries {
  param([string]$PackagePath)
  if ((Get-Item -LiteralPath $PackagePath).PSIsContainer) {
    return Get-ChildItem -LiteralPath $PackagePath -Recurse -Force | ForEach-Object {
      $_.FullName.Substring($PackagePath.Length).TrimStart("\", "/") -replace "\\", "/"
    }
  }

  $zip = [System.IO.Compression.ZipFile]::OpenRead($PackagePath)
  try {
    return $zip.Entries | ForEach-Object { $_.FullName -replace "\\", "/" }
  } finally {
    $zip.Dispose()
  }
}

$entries = @(Get-PackageEntries -PackagePath $Path)
$problems = New-Object System.Collections.Generic.List[string]

$forbiddenPatterns = @(
  '(^|/)\.env$',
  '(^|/)\.env\.production$',
  '(^|/)\.env\.test$',
  '(^|/)\.env\.production\.example$',
  '(^|/)\.env\.test\.example$',
  '(^|/)\.venv(/|$)',
  '(^|/)data(/|$)',
  '(^|/)reports(/|$)',
  '(^|/)output(/|$)',
  '(^|/)tests(/|$)',
  '(^|/)skill_sources(/|$)',
  '(^|/)backups(/|$)',
  '(^|/)code_backups(/|$)',
  '(^|/)releases(/|$)',
  '(^|/)__pycache__(/|$)',
  '(^|/)\.pytest_cache(/|$)',
  '(^|/)AGENTS\.md$',
  '(^|/)BUILD_REPORT\.md$',
  '(^|/)UPDATE_WORKFLOW\.md$',
  '(^|/)CHANGELOG\.md$',
  '(^|/)start_test\.cmd$',
  '(^|/)\.secret_key$',
  '\.db$',
  '\.sqlite$',
  '\.pyc$',
  '\.log$'
)

foreach ($entry in $entries) {
  foreach ($pattern in $forbiddenPatterns) {
    if ($entry -match $pattern) {
      $problems.Add("Clean package must not include: $entry")
    }
  }
}

if ($problems.Count -gt 0) {
  $problems | ForEach-Object { Write-Error $_ }
  throw "Clean package verification failed."
}

Write-Host "Clean package verification passed: $Path"
