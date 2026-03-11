@echo off
setlocal
cd /d "%~dp0"

set "VOICE_AGENT_DEFAULT_CONFIG=%CD%\scripts\local_services.default.json"
set "VOICE_AGENT_LAUNCHER_CONFIG=%CD%\scripts\local_services.user.json"
set "VOICE_AGENT_LAUNCH_ARGS="

echo [voice-agent] clearing existing service processes...
for %%P in (
  service_launcher.exe
  voice_service.exe
  piper_http.exe
  qwen_tts_http.exe
  desktop_runtime.exe
  intent_service.exe
  dialog_service.exe
  telemetry_service.exe
  game_launcher.exe
) do (
  taskkill /f /t /im "%%P" >nul 2>nul
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = [System.IO.Path]::GetFullPath('%CD%').ToLowerInvariant();" ^
  "$targets = @('start_local_services.py','intent_service\main.py','dialog_service\main.py','game_launcher\main.py','telemetry_service\main.py','python_voice_service\main.py','python_voice_service\piper_http.py','python_voice_service\qwen_tts_http.py','python_voice_service\kokoro_tts_http.py','python_voice_service\desktop_runtime.py','uvicorn main:app','uvicorn piper_http:app','uvicorn qwen_tts_http:app','uvicorn kokoro_tts_http:app','uvicorn desktop_runtime:app');" ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.Name -match '^(python|py)(\.exe)?$' } | ForEach-Object { $cmd = $_.CommandLine.ToLowerInvariant(); if($cmd.Contains($root)){ foreach($t in $targets){ if($cmd.Contains($t.ToLowerInvariant())){ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; break } } } }" >nul 2>nul

timeout /t 1 /nobreak >nul

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ports = @(8000,5005,5006,5007,8787);" ^
  "foreach($port in $ports){" ^
  "  Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {" ^
  "    try { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } catch {}" ^
  "  }" ^
  "}" >nul 2>nul

timeout /t 1 /nobreak >nul

if exist "%CD%\runtime\services\service_launcher.exe" (
  start "" "%CD%\runtime\services\service_launcher.exe"
  exit /b 0
)

if exist "C:\Program Files\mosquitto\mosquitto.exe" (
  set "VOICE_AGENT_MOSQUITTO_EXE=C:\Program Files\mosquitto\mosquitto.exe"
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$c = New-Object System.Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1',1883); exit 0 } catch { exit 1 } finally { $c.Dispose() }" >nul 2>nul
if %errorlevel%==0 (
  echo [voice-agent] detected existing MQTT broker on 1883, using --no-hub.
  set "VOICE_AGENT_LAUNCH_ARGS=--no-hub"
)

set "ASR_VENV_PY=%CD%\python_voice_service\.venv_asr\Scripts\python.exe"
if exist "%ASR_VENV_PY%" (
  set "VOICE_AGENT_ASR_PYTHON=%ASR_VENV_PY%"
  set "VOICE_AGENT_VOICE_CMD=%ASR_VENV_PY% -m uvicorn main:app --host 0.0.0.0 --port 8000"
  set "VOICE_AGENT_VOICE_CWD=%CD%\python_voice_service"
)

set "TTS_VENV_PY=%CD%\python_voice_service\.venv_tts\Scripts\python.exe"
if exist "%TTS_VENV_PY%" (
  set "VOICE_AGENT_TTS_PYTHON=%TTS_VENV_PY%"
  set "VOICE_AGENT_PIPER_HTTP_CMD=%TTS_VENV_PY% -m uvicorn piper_http:app --host 0.0.0.0 --port 5005"
  set "VOICE_AGENT_QWEN_HTTP_CMD=%TTS_VENV_PY% -m uvicorn qwen_tts_http:app --host 0.0.0.0 --port 5006"
  set "VOICE_AGENT_PIPER_HTTP_CWD=%CD%\python_voice_service"
)
if exist "%ASR_VENV_PY%" (
  set "VOICE_AGENT_DESKTOP_RUNTIME_CMD=%ASR_VENV_PY% -m uvicorn desktop_runtime:app --host 0.0.0.0 --port 8787"
  set "VOICE_AGENT_DESKTOP_RUNTIME_CWD=%CD%\python_voice_service"
)

if exist "%ASR_VENV_PY%" (
  "%ASR_VENV_PY%" scripts\start_local_services.py %VOICE_AGENT_LAUNCH_ARGS%
  exit /b %errorlevel%
)

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 scripts\start_local_services.py %VOICE_AGENT_LAUNCH_ARGS%
  exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
  python scripts\start_local_services.py %VOICE_AGENT_LAUNCH_ARGS%
  exit /b %errorlevel%
)

echo [voice-agent] Python launcher not found. Install Python or run scripts\start_local_services.py manually.
exit /b 1
