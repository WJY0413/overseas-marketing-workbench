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

function Get-DeclaredRuntimePorts {
  param([string]$Root)

  $ports = New-Object System.Collections.Generic.HashSet[int]
  $configFiles = @(
    ".env",
    ".env.example",
    "start.cmd",
    "start_production.cmd",
    "start_test.cmd"
  )
  $patterns = @(
    '(?im)APP_PORT\s*=\s*"?(\d+)',
    '(?im)127\.0\.0\.1:(\d+)',
    '(?im)--port(?:[\s'',"]+)(\d+)'
  )
  foreach ($name in $configFiles) {
    $path = Join-Path $Root $name
    if (-not (Test-Path -LiteralPath $path)) { continue }
    $content = Get-Content -LiteralPath $path -Raw
    foreach ($pattern in $patterns) {
      foreach ($match in [regex]::Matches($content, $pattern)) {
        $port = [int]$match.Groups[1].Value
        if ($port -ge 1 -and $port -le 65535) {
          [void]$ports.Add($port)
        }
      }
    }
  }
  return @($ports | Sort-Object)
}

function Test-LocalPortListening {
  param([int]$Port)

  $client = New-Object Net.Sockets.TcpClient
  try {
    $pending = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
    if (-not $pending.AsyncWaitHandle.WaitOne(500)) { return $false }
    $client.EndConnect($pending)
    return $true
  } catch {
    return $false
  } finally {
    $client.Dispose()
  }
}

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
$seedAssets = @(
  "linkedin_people_master.sqlite",
  "nogo_seed_library.sqlite"
)
$copyItems = Get-ChildItem -LiteralPath $SourceRoot -Force | Where-Object {
  $_.Name -notin $preserveNames -and $_.Name -notlike '.venv.previous-*'
}
$runtimePorts = @(Get-DeclaredRuntimePorts -Root $TargetRoot)

Write-Host "WORKBENCH_UPDATE: PREVIEW"
Write-Host "Source: $SourceRoot (version $sourceVersion)"
Write-Host "Target: $TargetRoot (version $targetVersion)"
Write-Host "Preserved: $($preserveNames -join ', ')"
Write-Host "Seed assets refreshed when supplied: $($seedAssets -join ', ')"
Write-Host "Program items to update: $($copyItems.Name -join ', ')"
Write-Host "Declared runtime ports: $(if ($runtimePorts) { $runtimePorts -join ', ' } else { 'none detected' })"

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

$listeningPorts = @($runtimePorts | Where-Object { Test-LocalPortListening -Port $_ })
if ($listeningPorts) {
  throw "Target Workbench runtime port(s) are still listening: $($listeningPorts -join ', '). Stop the target Workbench and run the update again. No files changed."
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

# Runtime data remains untouched. Internal packages may deliberately include
# read-only seed assets, which are refreshed independently of workbench.db.
$sourceData = Join-Path $SourceRoot "data"
$targetData = Join-Path $TargetRoot "data"
foreach ($seedAsset in $seedAssets) {
  $sourceAsset = Join-Path $sourceData $seedAsset
  if (-not (Test-Path -LiteralPath $sourceAsset)) { continue }
  New-Item -ItemType Directory -Force -Path $targetData | Out-Null
  $targetAsset = Join-Path $targetData $seedAsset
  if ($seedAsset -eq "linkedin_people_master.sqlite" -and (Test-Path -LiteralPath $targetAsset)) {
    Write-Host "Preserved operator-owned LinkedIn master: $targetAsset"
    continue
  }
  if (Test-Path -LiteralPath $targetAsset) {
    $assetBackup = Join-Path $backupRoot "seed_assets"
    New-Item -ItemType Directory -Force -Path $assetBackup | Out-Null
    Copy-Item -LiteralPath $targetAsset -Destination $assetBackup -Force
  }
  Copy-Item -LiteralPath $sourceAsset -Destination $targetAsset -Force
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
