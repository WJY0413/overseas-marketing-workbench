@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "APP_PORT=8001"

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

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_python.ps1"
if errorlevel 1 (
  pause
  exit /b 1
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

:check_test_port
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8001 -State Listen -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }" >nul 2>nul
exit /b %errorlevel%
