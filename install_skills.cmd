@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_workbench_skills.ps1" -SourceRoot "%~dp0skills" -Apply
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" (
  echo Skills are ready. Restart Codex before using them.
) else (
  echo Skill installation did not complete. Review the message above.
)
pause
exit /b %EXIT_CODE%
