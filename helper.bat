@echo off
setlocal
cd /d "%~dp0"

if exist "C:\Program Files\mosquitto\mosquitto.exe" (
  set "VOICE_AGENT_MOSQUITTO_EXE=C:\Program Files\mosquitto\mosquitto.exe"
)

where py >nul 2>nul
if %errorlevel%==0 (
  start "" py -3 scripts\start_local_services.py
  exit /b 0
)

where python >nul 2>nul
if %errorlevel%==0 (
  start "" python scripts\start_local_services.py
  exit /b 0
)

echo [voice-agent] Python launcher not found. Install Python or run scripts\start_local_services.py manually.
exit /b 1
