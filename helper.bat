@echo off
setlocal
cd /d "%~dp0"

set "VOICE_AGENT_DEFAULT_CONFIG=%CD%\scripts\local_services.default.json"
set "VOICE_AGENT_LAUNCHER_CONFIG=%CD%\scripts\local_services.user.json"

echo [voice-agent] clearing existing service processes...
for %%P in (
  service_launcher.exe
  voice_service.exe
  piper_http.exe
  qwen_tts_http.exe
  intent_service.exe
  dialog_service.exe
  telemetry_service.exe
  game_launcher.exe
) do (
  taskkill /f /t /im "%%P" >nul 2>nul
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = [System.IO.Path]::GetFullPath('%CD%').ToLowerInvariant();" ^
  "$targets = @('start_local_services.py','intent_service\main.py','dialog_service\main.py','game_launcher\main.py','telemetry_service\main.py','python_voice_service\main.py','python_voice_service\piper_http.py','python_voice_service\qwen_tts_http.py');" ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.Name -match '^(python|py)(\.exe)?$' } | ForEach-Object { $cmd = $_.CommandLine.ToLowerInvariant(); if($cmd.Contains($root)){ foreach($t in $targets){ if($cmd.Contains($t.ToLowerInvariant())){ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; break } } } }" >nul 2>nul

timeout /t 1 /nobreak >nul

if exist "%CD%\runtime\services\service_launcher.exe" (
  start "" "%CD%\runtime\services\service_launcher.exe"
  exit /b 0
)

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
