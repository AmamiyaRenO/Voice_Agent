@echo off
setlocal
cd /d "%~dp0"

set "VOICE_AGENT_DEFAULT_CONFIG=%CD%\scripts\local_services.default.json"
set "VOICE_AGENT_LAUNCHER_CONFIG=%CD%\scripts\local_services.user.json"
set "VOICE_AGENT_LAUNCH_ARGS="
set "VOICE_AGENT_MQTT_PORT=1883"

for /f %%I in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$port='1883'; $cfgPath='%VOICE_AGENT_LAUNCHER_CONFIG%'; if (Test-Path $cfgPath) { try { $cfg = Get-Content -Path $cfgPath -Raw | ConvertFrom-Json; $candidate = [string]$cfg.env.MQTT_PORT; if ($candidate) { $port = $candidate.Trim() } } catch {} }; Write-Output $port"') do (
  set "VOICE_AGENT_MQTT_PORT=%%I"
)

echo [voice-agent] clearing existing service processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = [System.IO.Path]::GetFullPath('%CD%').ToLowerInvariant();" ^
  "$targets = @('start_local_services.py','intent_service\main.py','dialog_service\main.py','game_launcher\main.py','telemetry_service\main.py','python_voice_service\main.py','python_voice_service\piper_http.py','python_voice_service\kokoro_tts_http.py','python_voice_service\desktop_runtime.py','uvicorn main:app','uvicorn piper_http:app','uvicorn kokoro_tts_http:app','uvicorn desktop_runtime:app');" ^
  "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.Name -match '^(python|py)(\.exe)?$' } | ForEach-Object { $cmd = $_.CommandLine.ToLowerInvariant(); if($cmd.Contains($root)){ foreach($t in $targets){ if($cmd.Contains($t.ToLowerInvariant())){ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; break } } } }" >nul 2>nul

timeout /t 1 /nobreak >nul

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ports = @(8000,5005,5007,8787);" ^
  "foreach($port in $ports){" ^
  "  Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {" ^
  "    try { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue } catch {}" ^
  "  }" ^
  "}" >nul 2>nul

timeout /t 1 /nobreak >nul

if exist "%CD%\native\mosquitto\windows-x64\mosquitto.exe" (
  set "VOICE_AGENT_MOSQUITTO_EXE=%CD%\native\mosquitto\windows-x64\mosquitto.exe"
)
if not defined VOICE_AGENT_MOSQUITTO_EXE if exist "C:\Program Files\mosquitto\mosquitto.exe" (
  set "VOICE_AGENT_MOSQUITTO_EXE=C:\Program Files\mosquitto\mosquitto.exe"
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$port = [int]('%VOICE_AGENT_MQTT_PORT%'); $c = New-Object System.Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1',$port); exit 0 } catch { exit 1 } finally { $c.Dispose() }" >nul 2>nul
if %errorlevel%==0 (
  echo [voice-agent] detected existing MQTT broker on %VOICE_AGENT_MQTT_PORT%, using --no-hub.
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
  set "VOICE_AGENT_KOKORO_HTTP_CMD=%TTS_VENV_PY% -m uvicorn kokoro_tts_http:app --host 0.0.0.0 --port 5007"
  set "VOICE_AGENT_PIPER_HTTP_CWD=%CD%\python_voice_service"
  set "VOICE_AGENT_KOKORO_HTTP_CWD=%CD%\python_voice_service"
)
if exist "%ASR_VENV_PY%" (
  set "VOICE_AGENT_DESKTOP_RUNTIME_CMD=%ASR_VENV_PY% -m uvicorn desktop_runtime:app --host 0.0.0.0 --port 8787"
  set "VOICE_AGENT_DESKTOP_RUNTIME_CWD=%CD%\python_voice_service"
)

if /I "%VOICE_AGENT_OPEN_PANEL%"=="0" goto :skip_panel_open
echo [voice-agent] the Rachel Console will open when http://127.0.0.1:8787 is ready.
start "" /b powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command ^
  "$deadline = (Get-Date).AddSeconds(90);" ^
  "while ((Get-Date) -lt $deadline) {" ^
  "  try { $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8787/healthz' -UseBasicParsing -TimeoutSec 2; if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) { Start-Process 'http://127.0.0.1:8787/index.html'; exit 0 } } catch {};" ^
  "  Start-Sleep -Milliseconds 700" ^
  "}; exit 0"
:skip_panel_open

if exist "%ASR_VENV_PY%" (
  "%ASR_VENV_PY%" scripts\start_local_services.py %VOICE_AGENT_LAUNCH_ARGS%
  exit /b %errorlevel%
)

if exist "%CD%\native\python\windows-x64\python.exe" (
  set "VOICE_AGENT_BOOTSTRAP_PYTHON=%CD%\native\python\windows-x64\python.exe"
)

if defined VOICE_AGENT_BOOTSTRAP_PYTHON (
  "%VOICE_AGENT_BOOTSTRAP_PYTHON%" scripts\start_local_services.py %VOICE_AGENT_LAUNCH_ARGS%
  exit /b %errorlevel%
)

for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "%ProgramFiles%\Python312\python.exe"
  "%ProgramFiles%\Python311\python.exe"
  "%ProgramFiles%\Python310\python.exe"
  "%ProgramFiles(x86)%\Python312\python.exe"
  "%ProgramFiles(x86)%\Python311\python.exe"
  "%ProgramFiles(x86)%\Python310\python.exe"
) do (
  if not defined VOICE_AGENT_BOOTSTRAP_PYTHON if exist "%%~fP" (
    set "VOICE_AGENT_BOOTSTRAP_PYTHON=%%~fP"
  )
)

if defined VOICE_AGENT_BOOTSTRAP_PYTHON (
  "%VOICE_AGENT_BOOTSTRAP_PYTHON%" scripts\start_local_services.py %VOICE_AGENT_LAUNCH_ARGS%
  exit /b %errorlevel%
)

where py >nul 2>nul
if %errorlevel%==0 (
  py -3.12 -c "import sys" >nul 2>nul
  if %errorlevel%==0 (
    py -3.12 scripts\start_local_services.py %VOICE_AGENT_LAUNCH_ARGS%
    exit /b %errorlevel%
  )
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
