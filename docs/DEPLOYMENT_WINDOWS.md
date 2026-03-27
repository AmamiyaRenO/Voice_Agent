# Windows Deployment (EXE + Installer)

This guide targets non-programmer end users:
- No manual Python path setup.
- No virtual environment setup on user machines.
- Runtime changes done from User Panel pages.

## 1) Build Service EXEs

From repository root:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\packaging\build_services_exe.ps1
```

Tip:
- Prefer Python 3.11 for packaging (`av` wheels are more stable than 3.12 in many environments).

Output:
- `dist/services/voice_service.exe`
- `dist/services/piper_http.exe`
- `dist/services/desktop_runtime.exe`
- `dist/services/intent_service.exe`
- `dist/services/dialog_service.exe`
- `dist/services/telemetry_service.exe`
- `dist/services/game_launcher.exe`
- `dist/services/service_launcher.exe`

Notes:
- `service_launcher.exe` is built from `scripts/start_local_services.py`.
- The packaged service set now includes `kokoro_tts_http.exe` instead of the removed legacy alternate TTS wrapper.
- Installed one-click mode should not require system Python when the packaged services are present.

## 2) Prepare Unity Build

Build Unity Windows player and place files under:

- `dist/unity/`

The installer script copies this folder to `{app}\app`.

## 3) Build Installer

Install Inno Setup 6 first, then run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\packaging\build_installer.ps1
```

Optional (bundle Piper runtime + default voice model so TTS works out of the box):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\packaging\build_installer.ps1 `
  -PiperRuntimeDir "D:\runtime\piper"
```

`PiperRuntimeDir` should include:
- `piper.exe`
- `models\*.onnx`
- matching model config (`.onnx.json` or `.json`)

Output:
- `dist/installer/VoiceAgentSetup.exe`

Single command release build (service exe + installer):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\packaging\build_release_oneclick.ps1 `
  -UnityBuildDir "D:\builds\voice_agent_unity"
```

If multiple Python versions exist, pin to 3.11 explicitly:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\packaging\build_release_oneclick.ps1 `
  -UnityBuildDir "D:\builds\voice_agent_unity" `
  -PythonExe "C:\Users\<you>\AppData\Local\Programs\Python\Python311\python.exe"
```

If Inno Setup is not installed in default path:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\packaging\build_release_oneclick.ps1 `
  -UnityBuildDir "D:\builds\voice_agent_unity" `
  -IsccPath "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
```

If your folders are different:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\packaging\build_installer.ps1 `
  -UnityBuildDir "D:\builds\voice_agent_unity" `
  -ServicesDir "D:\builds\voice_agent_services" `
  -PiperRuntimeDir "D:\runtime\piper"
```

## 4) First Run (User Workflow)

After installation:

1. Start `start_voice_agent.bat` (desktop icon points to this).
2. Browser opens setup page:
   - `http://127.0.0.1:8787/setup.html`
3. Configure:
   - Prerequisites page shows Piper/Ollama status
   - `Install Ollama` button (winget)
   - `Pull Ollama Model` button (default `qwen3.5:0.8b`)
   - ASR mode (`offline` / `api`)
   - Agent listening start/pause
   - OpenAI API Key / model
   - Launch triggers / exit keywords
   - Manifest paths (if needed)
4. Configure game rows in:
   - `http://127.0.0.1:8787/games.html`

One-click behavior:
- Starts packaged service launcher (`runtime/services/service_launcher.exe`).
- Starts packaged desktop runtime (`runtime/services/desktop_runtime.exe`) for the panel/audio shell.
- Waits for voice service health (`http://127.0.0.1:8000/healthz`).
- Starts Unity client executable under `app/`.
- Waits for User Panel health (`http://127.0.0.1:8787/healthz`).
- First run opens `/setup.html`; later runs open `/index.html`.
- Start Menu includes `Open Voice Agent Setup` to force-open setup wizard again.

## 5) Config Files (Installed)

- Default config:
  - `scripts/local_services.default.json`
- User override config (editable by panel):
  - `scripts/local_services.user.json`

Resolution priority:
1. `local_services.user.json` (User Panel writes here)
2. `local_services.default.json`
3. Environment variables (fallback only)

## 6) Troubleshooting

- Installer build fails with missing Unity dir:
  - Ensure `dist/unity` exists or pass `-UnityBuildDir`.
- Installer build fails with missing services dir:
  - Run `build_services_exe.ps1` first or pass `-ServicesDir`.
- Runtime still asks for Python:
  - Confirm both `runtime/services/service_launcher.exe` and `runtime/services/desktop_runtime.exe` exist in the installed folder.
- Setup page not reachable:
  - Confirm the packaged desktop runtime is listening on `127.0.0.1:8787`.
  - Unity is no longer the primary owner of the setup panel in packaged installs.
