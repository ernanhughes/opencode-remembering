@echo off
setlocal
cd /d "%~dp0"

echo.
echo Starting OpenCode Remembering self-check...
echo.

set "PROJECT_ARG=%~1"

if "%PROJECT_ARG%"=="" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0remembering-self-check.ps1"
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0remembering-self-check.ps1" -Project "%PROJECT_ARG%"
)

set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo Self-check finished: READY
) else (
  echo Self-check finished: NOT READY
)
echo.
pause
exit /b %RC%
