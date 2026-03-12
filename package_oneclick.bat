@echo off
setlocal
cd /d "%~dp0"

set "UNITY_BUILD_DIR=%~1"
if "%UNITY_BUILD_DIR%"=="" if defined VOICE_AGENT_UNITY_BUILD_DIR set "UNITY_BUILD_DIR=%VOICE_AGENT_UNITY_BUILD_DIR%"
if "%UNITY_BUILD_DIR%"=="" if exist "D:\unityproject\agent" set "UNITY_BUILD_DIR=D:\unityproject\agent"
if "%UNITY_BUILD_DIR%"=="" set "UNITY_BUILD_DIR=%CD%\dist\unity"

if not exist "%UNITY_BUILD_DIR%" (
  echo [voice-agent] Unity build directory not found: %UNITY_BUILD_DIR%
  echo [voice-agent] Pass a path as the first argument, or set VOICE_AGENT_UNITY_BUILD_DIR.
  exit /b 1
)

echo [voice-agent] packaging with Unity build: %UNITY_BUILD_DIR%
powershell -NoProfile -ExecutionPolicy Bypass -File "%CD%\scripts\packaging\build_release_oneclick.ps1" -UnityBuildDir "%UNITY_BUILD_DIR%"
exit /b %errorlevel%
