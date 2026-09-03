@echo off
setlocal
cd /d "%~dp0"

set "PANEL_HEALTH_URL=http://127.0.0.1:8787/healthz"
set "PANEL_URL=http://127.0.0.1:8787/index.html"

echo [voice-agent] starting helper/services...
set "VOICE_AGENT_OPEN_PANEL=0"
start "" "%CD%\helper.bat"
set "VOICE_AGENT_OPEN_PANEL="

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = [System.IO.Path]::GetFullPath('%CD%');" ^
  "$unityDir = [string]$env:VOICE_AGENT_UNITY_BUILD_DIR;" ^
  "if (-not $unityDir) { if (Test-Path 'D:\unityproject\agent') { $unityDir = 'D:\unityproject\agent' } elseif (Test-Path (Join-Path $root 'dist\unity')) { $unityDir = Join-Path $root 'dist\unity' } };" ^
  "$unityExe = $null;" ^
  "if ($unityDir -and (Test-Path $unityDir)) { $unityExe = Get-ChildItem -Path $unityDir -Filter *.exe -File | Where-Object { $_.Name -notin @('UnityCrashHandler64.exe','UnityCrashHandler32.exe') } | Select-Object -First 1 };" ^
  "if ($unityExe) { $procName = [System.IO.Path]::GetFileNameWithoutExtension($unityExe.Name); if (-not (Get-Process -Name $procName -ErrorAction SilentlyContinue)) { Write-Host '[voice-agent] starting Unity:' $unityExe.FullName; Start-Process -FilePath $unityExe.FullName -WorkingDirectory $unityExe.DirectoryName | Out-Null } else { Write-Host '[voice-agent] Unity already running:' $procName } } else { Write-Warning '[voice-agent] Unity executable not found under VOICE_AGENT_UNITY_BUILD_DIR, D:\unityproject\agent, or dist\unity.' };" ^
  "$deadline = (Get-Date).AddSeconds(60);" ^
  "while ((Get-Date) -lt $deadline) { try { $resp = Invoke-WebRequest -Uri '%PANEL_HEALTH_URL%' -UseBasicParsing -TimeoutSec 2 -Method GET; if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300) { Start-Process '%PANEL_URL%' | Out-Null; exit 0 } } catch {}; Start-Sleep -Milliseconds 700 };" ^
  "Start-Process '%PANEL_URL%' | Out-Null; exit 0"

exit /b %errorlevel%
