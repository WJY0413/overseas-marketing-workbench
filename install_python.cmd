@echo off
setlocal
cd /d "%~dp0"
echo Install the latest stable Python from python.org and configure Workbench.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_python.ps1" -Mode Install
if errorlevel 1 (
  pause
  exit /b 1
)
echo Setup complete. Run start.cmd to open Workbench.
pause
exit /b 0
