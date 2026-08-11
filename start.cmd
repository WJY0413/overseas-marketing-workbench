@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "APP_PORT=8001"
set "PY_CMD="
set "NEEDS_INSTALL=0"

if not exist ".env" (
  if exist ".env.example" (
    copy ".env.example" ".env" >nul
    echo Created .env from .env.example.
  ) else (
    echo Missing .env and .env.example. Cannot start safely.
    pause
    exit /b 1
  )
)

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)" >nul 2>nul
  if errorlevel 1 (
    echo Existing .venv uses an unsupported Python version. Recreating .venv...
    rmdir /s /q ".venv"
  )
)

if not exist ".venv\Scripts\python.exe" (
  call :find_python
  if not defined PY_CMD (
    echo Python 3.11, 3.12, or 3.13 was not found.
    echo Install Python 3.12 from https://www.python.org/downloads/windows/
    echo Then reopen this folder and run start.cmd again.
    pause
    exit /b 1
  )

  echo First-time setup: creating local Python environment with !PY_CMD!...
  !PY_CMD! -m venv .venv
  if errorlevel 1 (
    echo Failed to create .venv. Check that Python is installed correctly.
    pause
    exit /b 1
  )
  set "NEEDS_INSTALL=1"
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn, sqlmodel, pandas, openpyxl, apscheduler, docx, cryptography" >nul 2>nul
if errorlevel 1 (
  set "NEEDS_INSTALL=1"
)

if "%NEEDS_INSTALL%"=="1" (
  echo Installing or repairing dependencies from requirements.txt...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  if errorlevel 1 (
    echo Failed to upgrade pip.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Failed to install dependencies.
    echo Check network access, proxy settings, or run this command manually:
    echo .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
  )
)

call :check_test_port
if errorlevel 1 (
  echo Test port 8001 is already in use.
  echo Open http://127.0.0.1:8001 or close the existing test service window.
  pause
  exit /b 1
)

set "APP_URL=http://127.0.0.1:%APP_PORT%"
set "PUBLIC_BASE_URL=%APP_URL%"
echo Starting Overseas Marketing Workbench at %APP_URL%
start "Overseas Marketing Workbench" cmd /k ""%~dp0.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port %APP_PORT%"
powershell -NoProfile -Command "Start-Sleep -Milliseconds 1800"
start "" "%APP_URL%"
endlocal
exit /b 0

:find_python
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PY_CMD=py -3.12"
    exit /b 0
  )
  py -3.11 -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PY_CMD=py -3.11"
    exit /b 0
  )
  py -3.13 -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PY_CMD=py -3.13"
    exit /b 0
  )
)

where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PY_CMD=python"
    exit /b 0
  )
)
exit /b 0

:check_test_port
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }" >nul 2>nul
exit /b %errorlevel%
