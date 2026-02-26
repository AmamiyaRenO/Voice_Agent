@echo off
setlocal
cd /d "%~dp0"

if exist "%CD%\start_voice_agent.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\start_voice_agent.ps1" %*
  exit /b %errorlevel%
)

echo [oneclick] missing start_voice_agent.ps1
exit /b 1
