param(
  [string]$SourceRoot = "",
  [string]$TargetRoot = "",
  [switch]$Apply,
  [switch]$Yes
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $SourceRoot) {
  $SourceRoot = Join-Path $projectRoot "skills"
}
if (-not $TargetRoot) {
  if (-not $env:USERPROFILE) {
    throw "USERPROFILE is unavailable; pass -TargetRoot explicitly."
  }
  $TargetRoot = Join-Path $env:USERPROFILE ".codex\skills"
}

$SourceRoot = [IO.Path]::GetFullPath($SourceRoot)
$TargetRoot = [IO.Path]::GetFullPath($TargetRoot)
$expectedSkills = @(
  "workbench-first-run-check",
  "workbench-batch-planner",
  "workbench-email-daily-runbook",
  "workbench-send-mail",
  "workbench-inbox-check",
  "email-bounce-suppression",
  "workbench-queue-builder",
  "workbench-parameter-tuner"
)

foreach ($name in $expectedSkills) {
  $sourceSkill = Join-Path $SourceRoot $name
  if (-not (Test-Path -LiteralPath (Join-Path $sourceSkill "SKILL.md"))) {
    throw "Bundled Skill is missing: $sourceSkill"
  }
}

$duplicates = New-Object System.Collections.Generic.List[string]
if (Test-Path -LiteralPath $TargetRoot) {
  foreach ($name in $expectedSkills) {
    $pattern = "^" + [regex]::Escape($name) + "(?:[ _.-]*(?:copy|old|backup)|\s*\(\d+\)|[ _.-]+\d+)$"
    Get-ChildItem -LiteralPath $TargetRoot -Directory -Force | Where-Object {
      $_.Name -ne $name -and $_.Name -match $pattern
    } | ForEach-Object {
      $duplicates.Add($_.FullName)
    }
  }
}

if ($duplicates.Count -gt 0) {
  Write-Host "SKILL_INSTALL: BLOCKED"
  $duplicates | Sort-Object -Unique | ForEach-Object { Write-Host "Duplicate Skill: $_" }
  throw "Resolve duplicate Skill folders before installation. Nothing was changed."
}

$actions = foreach ($name in $expectedSkills) {
  $targetSkill = Join-Path $TargetRoot $name
  [pscustomobject]@{
    Skill = $name
    Action = if (Test-Path -LiteralPath $targetSkill) { "update-in-place" } else { "install" }
    Target = $targetSkill
  }
}

Write-Host "SKILL_INSTALL: PREVIEW"
$actions | Format-Table -AutoSize
if (-not $Apply) {
  Write-Host "No files changed. Re-run with -Apply after reviewing the target."
  exit 0
}

if (-not $Yes) {
  $answer = Read-Host "Install/update these seven Skills in the listed paths? Type YES to continue"
  if ($answer -cne "YES") {
    Write-Host "Cancelled. No files changed."
    exit 1
  }
}

New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path $TargetRoot ".workbench-skill-backups\$stamp"
$updated = New-Object System.Collections.Generic.List[string]

foreach ($action in $actions) {
  $name = $action.Skill
  $sourceSkill = Join-Path $SourceRoot $name
  $targetSkill = Join-Path $TargetRoot $name
  $backupSkill = Join-Path $backupRoot $name
  $hadExisting = Test-Path -LiteralPath $targetSkill
  try {
    if ($hadExisting) {
      New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backupSkill) | Out-Null
      Move-Item -LiteralPath $targetSkill -Destination $backupSkill
    }
    Copy-Item -LiteralPath $sourceSkill -Destination $targetSkill -Recurse -Force
    if (-not (Test-Path -LiteralPath (Join-Path $targetSkill "SKILL.md"))) {
      throw "Installed Skill validation marker is missing: $name"
    }
    $updated.Add($name)
  }
  catch {
    if (Test-Path -LiteralPath $targetSkill) {
      Remove-Item -LiteralPath $targetSkill -Recurse -Force
    }
    if ($hadExisting -and (Test-Path -LiteralPath $backupSkill)) {
      Move-Item -LiteralPath $backupSkill -Destination $targetSkill
    }
    throw
  }
}

Write-Host "SKILL_INSTALL: COMPLETE"
Write-Host "Installed/updated: $($updated.Count)"
if (Test-Path -LiteralPath $backupRoot) {
  Write-Host "Rollback backup: $backupRoot"
}
Write-Host 'Restart Codex, then invoke: $workbench-first-run-check'
