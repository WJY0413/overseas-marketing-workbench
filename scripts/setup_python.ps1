param(
  [ValidateSet('Setup', 'Detect', 'Install')][string]$Mode = 'Setup',
  [string]$Root = (Split-Path -Parent $PSScriptRoot),
  [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'

function Get-PythonInfo {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
  # Do not invoke Microsoft Store aliases, which can open the Store instead.
  if ($Path -match '\\WindowsApps\\') { return }
  try {
    $result = & $Path -I -c "import sys,json,struct,venv,ssl; print(json.dumps(dict(path=sys.executable,version='.'.join(map(str,sys.version_info[:3])),stable=sys.version_info.releaselevel=='final',bits=struct.calcsize('P')*8,implementation=sys.implementation.name)))" 2>$null
    if ($LASTEXITCODE -ne 0) { return }
    $info = ($result -join '') | ConvertFrom-Json
    if ($info.stable -and $info.implementation -eq 'cpython' -and
        [version]$info.version -ge [version]'3.11' -and [version]$info.version -lt [version]'4.0') {
      return $info
    }
  } catch { return }
}

function Get-PythonCandidates {
  $paths = New-Object 'System.Collections.Generic.List[string]'
  foreach ($name in @('python.exe', 'python3.exe')) {
    Get-Command $name -All -ErrorAction SilentlyContinue | ForEach-Object { $paths.Add($_.Source) }
  }
  $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($launcher) {
    # -0p works with both the legacy launcher and the Python install manager.
    $listing = & $launcher.Source -0p 2>$null
    foreach ($line in $listing) {
      if ($line -match '([A-Za-z]:\\.+?python(?:w)?\.exe)\s*$') { $paths.Add($Matches[1]) }
    }
  }
  foreach ($key in @('HKCU:\Software\Python', 'HKLM:\Software\Python', 'HKLM:\Software\WOW6432Node\Python')) {
    Get-ChildItem $key -Recurse -ErrorAction SilentlyContinue |
      Where-Object { $_.PSChildName -eq 'InstallPath' } | ForEach-Object {
        $exe = $_.GetValue('ExecutablePath')
        if (-not $exe -and $_.GetValue('')) { $exe = Join-Path $_.GetValue('') 'python.exe' }
        if ($exe) { $paths.Add($exe) }
      }
  }
  foreach ($pattern in @("$env:LOCALAPPDATA\Programs\Python\Python*\python.exe", "$env:LOCALAPPDATA\Python\*\python.exe", "$env:ProgramFiles\Python*\python.exe")) {
    Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | ForEach-Object { $paths.Add($_.FullName) }
  }
  $paths | Sort-Object -Unique | ForEach-Object { Get-PythonInfo $_ } |
    Sort-Object @{Expression = { [version]$_.version }; Descending = $true}, @{Expression = { $_.bits }; Descending = $true}
}

function Get-OfficialInstaller {
  param([string]$Html, [string]$Architecture)
  $suffix = switch ($Architecture) { 'ARM64' { '-arm64' }; 'AMD64' { '-amd64' }; 'x86' { '' }; default { throw "Unsupported architecture: $Architecture" } }
  $pattern = 'https://www\.python\.org/ftp/python/(?<version>3\.\d+\.\d+)/python-\k<version>' + $suffix + '\.exe'
  $releases = [regex]::Matches($Html, $pattern) | ForEach-Object {
    [pscustomobject]@{ Version = [version]$_.Groups['version'].Value; Url = $_.Value }
  }
  $latest = $releases | Where-Object { $_.Version -ge [version]'3.11' } | Sort-Object Version -Descending | Select-Object -First 1
  if (-not $latest) { throw 'No stable installer found. Open https://www.python.org/downloads/windows/ and install Python 3.11 or newer.' }
  return $latest
}

function Install-LatestPython {
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $page = Invoke-WebRequest 'https://www.python.org/downloads/windows/' -UseBasicParsing -TimeoutSec 45
  $arch = $env:PROCESSOR_ARCHITEW6432
  if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
  $release = Get-OfficialInstaller $page.Content $arch
  Write-Host "Download Python $($release.Version) from $($release.Url)"
  Write-Host 'Install for the current Windows user and add Python to PATH.'
  if ((Read-Host 'Continue? [y/N]') -notmatch '^(y|yes)$') { throw 'Python installation cancelled.' }
  $installer = Join-Path ([IO.Path]::GetTempPath()) ("workbench-python-" + [guid]::NewGuid().ToString('N') + '.exe')
  try {
    Invoke-WebRequest $release.Url -UseBasicParsing -OutFile $installer -TimeoutSec 300
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
      throw 'Python installer signature could not be verified. Installation stopped.'
    }
    $target = Join-Path $env:LOCALAPPDATA ("Programs\Python\Python" + $release.Version.Major + $release.Version.Minor)
    $process = Start-Process -FilePath $installer -ArgumentList @('/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_test=0', 'Include_launcher=0', "TargetDir=`"$target`"") -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -notin @(0, 3010)) { throw "Python installer failed: $($process.ExitCode)" }
    $info = Get-PythonInfo (Join-Path $target 'python.exe')
    if (-not $info) { throw 'Python installed but could not be executed. Restart the terminal and retry.' }
    return $info
  } finally {
    if (Test-Path -LiteralPath $installer) { Remove-Item -LiteralPath $installer -Force }
  }
}

function Initialize-WorkbenchPython {
  param([string]$ProjectRoot, [string]$ExplicitPython = '', [switch]$Download)
  $ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
  $venv = Join-Path $ProjectRoot '.venv'
  $venvPython = Join-Path $venv 'Scripts\python.exe'
  $requirements = Join-Path $ProjectRoot 'requirements.txt'
  if (-not (Test-Path -LiteralPath $requirements)) { throw 'requirements.txt is missing.' }
  $existing = Get-PythonInfo $venvPython
  $selected = $null
  if ($ExplicitPython) {
    $selected = Get-PythonInfo $ExplicitPython
    if (-not $selected) { throw 'Selected Python must be a working stable CPython 3.11+ executable.' }
  } elseif ($Download) {
    $selected = Install-LatestPython
  } elseif ($existing) {
    $selected = $existing
  } else {
    $selected = Get-PythonCandidates | Select-Object -First 1
    if (-not $selected) { $selected = Install-LatestPython }
  }
  Write-Host "Python $($selected.version): $($selected.path)"
  if (-not $existing -or $ExplicitPython -or $Download) {
    # Preserve the old environment for recovery. Never recursively delete it.
    if (Test-Path -LiteralPath $venv) {
      $saved = Join-Path $ProjectRoot ('.venv.previous-' + [guid]::NewGuid().ToString('N'))
      foreach ($movePath in @($venv, $saved)) {
        if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($movePath)).TrimEnd('\') -ne $ProjectRoot.TrimEnd('\')) {
          throw 'Environment backup path must remain directly inside the Workbench folder.'
        }
      }
      Move-Item -LiteralPath $venv -Destination $saved
      Write-Host "Previous environment saved to $saved"
    }
    & $selected.path -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create .venv. Previous environment remains preserved.' }
  }
  $stamp = Join-Path $venv '.workbench-requirements'
  $wanted = (Get-Content -LiteralPath $requirements -Raw)
  $installed = if (Test-Path -LiteralPath $stamp) { Get-Content -LiteralPath $stamp -Raw } else { '' }
  $importsOk = $false
  try {
    & $venvPython -c 'import fastapi,uvicorn,sqlmodel,pandas,openpyxl,apscheduler,docx,cryptography,pydantic_settings,multipart,jinja2,email_validator' 2>$null
    $importsOk = $LASTEXITCODE -eq 0
  } catch { $importsOk = $false }
  if ($installed -cne $wanted -or -not $importsOk) {
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed. Check network/proxy and retry.' }
    & $venvPython -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check network/proxy or select another Python with -PythonPath.' }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Dependency compatibility check failed.' }
    & $venvPython -c 'import fastapi,uvicorn,sqlmodel,pandas,openpyxl,apscheduler,docx,cryptography,pydantic_settings,multipart,jinja2,email_validator'
    if ($LASTEXITCODE -ne 0) { throw 'Dependency import check failed.' }
    [IO.File]::WriteAllText($stamp, $wanted)
  }
  Write-Host 'Workbench Python environment ready.'
}

if ($MyInvocation.InvocationName -ne '.') {
  try {
    if ($Mode -eq 'Detect') { @(Get-PythonCandidates) | ConvertTo-Json -Depth 3; exit 0 }
    Initialize-WorkbenchPython -ProjectRoot $Root -ExplicitPython $PythonPath -Download:($Mode -eq 'Install')
    exit 0
  } catch { Write-Host "Setup failed: $($_.Exception.Message)" -ForegroundColor Red; exit 1 }
}
