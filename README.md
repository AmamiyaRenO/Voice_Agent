# Voice Agent (Unity + Python Services + SDK)

Voice Agent is a Unity-based voice interaction client for rehabilitation/game scenarios.  
It integrates speech recognition, intent routing, MQTT-based robot control, and HTTP TTS playback.

The repository also includes:
- A Python speech service (`python_voice_service/`) for ASR + LLM reply generation.
- A Python SDK (`python_sdk/`) for non-Unity control and automation.
- Local orchestration scripts for multi-process development.

## SDK Spotlight (Start Here for Integration)

If your goal is to control the robot/agent from Python (without editing Unity scenes first), use:

- **SDK guide:** [`python_sdk/README.md`](python_sdk/README.md)

This SDK mirrors the Unity `UserTestControlPanel` capabilities (TTS, face/LED/servo commands, game intents, and runtime LLM prompt control).
It also includes a browser-based SDK Visualizer at `http://<host>:8787/sdk` for interactive API testing and flow prototyping.

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Repository Layout](#repository-layout)
- [Features](#features)
- [Quick Start Paths](#quick-start-paths)
- [Environment Requirements](#environment-requirements)
- [Detailed Setup](#detailed-setup)
- [Service Endpoints and MQTT Topics](#service-endpoints-and-mqtt-topics)
- [Python SDK](#python-sdk)
- [SDK Visualizer](#sdk-visualizer)
- [Python Voice Service (ASR + LLM)](#python-voice-service-asr--llm)
- [TTS Backends (Piper and Qwen)](#tts-backends-piper-and-qwen)
- [Troubleshooting](#troubleshooting)
- [Development and Tests](#development-and-tests)
- [License](#license)

## Architecture Overview

```text
Microphone / External Transcript Source
        |
        v
Unity Voice Layer (VoskSpeechToText / VoiceGameLauncher / VoiceGameWiring)
        |                                \
        |                                 \ (optional HTTP)
        |                                  -> python_voice_service (ASR /respond)
        |
        +--> MQTT broker (robot/intent, robot/pi/*, robot/dialog/*, robot/tts/*)
                   |
                   v
            Robot side services / orchestrator / hardware daemons

Unity TTS playback path:
Unity -> HTTP GET/POST /speak (port 5005 by default) -> Piper or Qwen wrapper
```

The maintained recognition path in this repo is `python_voice_service/main.py` (`/transcribe` with Faster-Whisper), or any custom transcript source integrated into Unity.

## Repository Layout

```text
Assets/                   Unity scenes, scripts, prefabs, runtime components
python_sdk/               Python client SDK for robot/voice controls
python_voice_service/     FastAPI services (ASR, /respond, Piper/Qwen TTS wrappers)
scripts/                  Multi-service launcher + intent/dialog helper services
docs/                     Integration guides (SDK, integration, and supporting notes)
tests/                    Python SDK tests
native/                   Native audio processing helpers
```

## Features

- Unity-first voice workflow with wake-word and intent routing.
- Optional Faster-Whisper transcription backend through FastAPI.
- MQTT command publish for face/servo/LED and game intents.
- Embedded remote control panel (`UserTestControlPanel`) over HTTP (default port `8787`).
- Pluggable TTS backend on stable endpoint (`/speak`) with Piper or Qwen wrapper.
- Python SDK parity with Unity panel actions.
- SDK Visualizer (`/sdk`) with step-by-step flow building, execution, and JSON import/export.
- Local multi-process launcher (`scripts/start_local_services.py`).

## Quick Start Paths

### Path A (Recommended): Unity + Python Voice Service (Faster-Whisper)

1. Set up `python_voice_service` virtual environment.
2. Run `uvicorn main:app --host 0.0.0.0 --port 8000`.
3. In Unity, set Python transcription URL to `http://127.0.0.1:8000/transcribe`.
4. Keep MQTT broker running if you need robot intents/hardware controls.

### Path B: Python SDK Only (Automation / Integration)

1. Install SDK dependencies from `python_sdk/requirements.txt`.
2. Import `voice_agent_sdk` from `python_sdk/`.
3. Connect to broker and call SDK methods (TTS, face, LED, servo, intents).
4. See [`python_sdk/README.md`](python_sdk/README.md).

## Environment Requirements

- **Unity:** `2022.3.56f1c1` (see `ProjectSettings/ProjectVersion.txt`)
- **OS:** Any OS supported by your Unity/Python deployment (Windows is common for this setup)
- **Python:** 3.10+ (3.12 recommended for service environments)
- **MQTT broker:** typically on `1883`
- **Audio:** microphone permission enabled

Optional dependencies:
- Faster-Whisper model files (for Python ASR route)
- Ollama runtime (for `/respond` endpoint)
- Piper executable + ONNX model (for Piper TTS wrapper)
- Qwen TTS environment (separate venv recommended)

## Detailed Setup

### 1) Clone and Open

```bash
git clone <your-voice-agent-repo-url>
cd Voice_Agent
```

Open `Voice_Agent` via Unity Hub.

### 2) Unity Configuration Notes

- Confirm scene references for:
  - `VoskSpeechToText`
  - `VoiceGameLauncher`
  - `VoiceGameWiring`
  - `PiMessageHub` (if used in your scene)
  - `UserTestControlPanel` (optional but strongly recommended for remote testing)
- Enable scripting define `ROBOTVOICE_USE_MQTT` if your build relies on MQTT intent publishing.
- If using wake prompt audio, assign clip references in launcher components.

### 3) Start Local Services

The main orchestrator script is:
- `scripts/start_local_services.py`

Important:
- `--hub-cmd` (or env `VOICE_AGENT_HUB_CMD`) is required.
- By default, the script starts voice service + Piper HTTP + Qwen HTTP + intent service + dialog service.
- If one managed process exits, the launcher shuts down the remaining processes.

Example (PowerShell):

```powershell
$env:VOICE_AGENT_HUB_CMD = "<command that starts your MQTT/message hub>"
python scripts/start_local_services.py --hub-cmd $env:VOICE_AGENT_HUB_CMD
```

You can also pass `--env-file` to preload environment variables.

### 4) Optional helper.bat

`helper.bat` is a machine-specific convenience launcher and contains hardcoded local paths.  
Adjust paths before reuse on another machine.

## Service Endpoints and MQTT Topics

### HTTP endpoints

| Service | Default Port | Key Endpoints |
|---|---:|---|
| Unity UserTestControlPanel | `8787` | `/`, `/sdk`, `/api/speak`, `/api/llm/prompt`, `/api/face`, `/api/flower`, `/api/led`, `/api/game` |
| Python Voice Service | `8000` | `/healthz`, `/transcribe`, `/respond`, `/respond/config`, `/respond/metrics` |
| Piper wrapper | `5005` | `/speak` (GET/POST), `/speak_stream` |
| Qwen wrapper | `5006` | `/speak` (GET/POST), `/metrics` |

### MQTT topics (core)

| Topic | Direction | Purpose |
|---|---|---|
| `robot/intent` | publish | Launch/exit game intents |
| `robot/pi/face/cmd` | publish | Face expression commands |
| `robot/pi/servo/cmd` | publish | Servo/flower commands |
| `robot/pi/led/cmd` | publish | LED commands |
| `robot/dialog/query` | publish/subscribe | Dialog query path |
| `robot/dialog/answer` | publish/subscribe | Dialog answer path |
| `robot/tts/options` | publish/subscribe | TTS voice/model/speaker options |
| `robot/voice/text` | subscribe (intent service) | Raw recognized text input |

For payload examples, see [`docs/INTEGRATION.md`](docs/INTEGRATION.md).

## Python SDK

Primary link:
- [`python_sdk/README.md`](python_sdk/README.md)

### What the SDK controls

- Audible speech through Unity panel API (`/api/speak`)
- WAV synthesis through TTS wrapper (`/speak`)
- Face presets/custom commands
- Servo/flower actions
- LED modes
- Game launch/exit intents
- Runtime LLM prompt get/set/reset via Unity panel API (`/api/llm/prompt`)

### Install

```bash
pip install -r python_sdk/requirements.txt
```

### Import notes

The SDK package lives under `python_sdk/voice_agent_sdk`.  
Run scripts from repo root with `python_sdk` on `PYTHONPATH`, or run from inside `python_sdk`.

Example (PowerShell):

```powershell
$env:PYTHONPATH = "python_sdk"
python .\your_script.py
```

### Minimal usage

```python
from voice_agent_sdk import VoiceAgentClient

client = VoiceAgentClient(host="10.0.0.1")

# Audible TTS via Unity panel API
client.speak("Hello Rachel", voice="en_US", speed=1.0, volume=1.0)

# MQTT controls
client.face_happy(duration=3)
client.led_breathe(color="#00BFFF", brightness=0.8, period=2.5)
client.servo_open_hold()

# Intents
client.launch_game("cornhole")
client.exit_game()

# Runtime LLM prompt through /api/llm/prompt
cfg = client.get_llm_prompt()
client.set_llm_prompt("You are a concise rehab coach. Keep replies short.")
# client.reset_llm_prompt()
```

### Key method groups

- Speech:
  - `speak(...)` -> Unity panel `/api/speak` (audible)
  - `synthesize_wav(...)` -> TTS wrapper `/speak` (WAV bytes)
- LLM prompt:
  - `get_llm_prompt()`
  - `set_llm_prompt(prompt)`
  - `reset_llm_prompt()`
- Face:
  - `face_happy()`, `face_neutral()`, `face_sad()`, `face_very_sad()`, `face_excited()`, `face_idle()`, `face_custom(...)`
- Servo:
  - `servo_open()`, `servo_close()`, `servo_open_hold()`, `servo_close_hold()`, `servo_center_hold()`, `servo_stop()`, `servo_open_slow()`, `servo_close_slow()`
- LED:
  - `led_breathe()`, `led_solid()`, `led_random()`, `led_off()`
- Intents/options:
  - `launch_game(...)`, `exit_game()`, `set_tts_options(...)`, `set_dialog_style(...)`

## SDK Visualizer

The SDK Visualizer is served by Unity `UserTestControlPanel` and is designed for rapid integration testing.

Access:
- Start Unity with `UserTestControlPanel` listening on port `8787`.
- Open `http://<host-ip>:8787/sdk` (or `/sdk.html`) in a browser.

Core capabilities:
- SDK method sandbox:
  - Select a mapped SDK method (for example `speak`, `set_llm_prompt`, `launch_game`).
  - Auto-fill endpoint and payload templates.
  - Send request and inspect HTTP status + response body directly in the UI.
- Flow Builder:
  - Drag method templates into a canvas and run steps sequentially.
  - Add utility nodes: `delay(ms)`, `condition(expr)`, and `wait_keyword(keyword)`.
  - Reorder, delete, and edit steps with per-step configuration.
  - Configure API step behavior such as HTTP method, JSON payload, and `continueOnError`.
- Condition and context controls:
  - Condition expressions can use flow context values such as `ctx.lastStatus`, `ctx.lastJson`, `ctx.lastRaw`, and `ctx.lastRecognized`.
  - Keyword wait nodes support source filtering (`user` / `coach` / `any`), timeout, poll interval, case sensitivity, and "only new text" matching.
- Debug and sharing:
  - Live run log and per-step state (`running`, `ok`, `error`).
  - Export flow definitions to JSON and import them later for repeatable test scenarios.

This page calls the same `/api/*` routes used by `python_sdk/voice_agent_sdk/client.py`, so it is useful for validating payload shape and backend behavior before writing automation scripts.

### Built-in method templates

Current built-ins in the visualizer method list:

- `speak(text,voice,model,speed,volume)` -> `/api/speak`
- `qwen_speak(text,speaker,instruct)` -> `/api/qwen/speak`
- `set_tts_options(voice,model)` -> `/api/voice`
- `set_tts_model(model)` -> `/api/voice`
- `get_llm_prompt()` -> `/api/llm/prompt` (GET)
- `set_llm_prompt(prompt)` -> `/api/llm/prompt`
- `reset_llm_prompt()` -> `/api/llm/prompt`
- `launch_game(name)` -> `/api/game`
- `exit_game()` -> `/api/game`
- `face_preset(mode,seconds)` -> `/api/face`
- `flower_open()` -> `/api/flower`
- `led_breathe(color,brightness,period,duration)` -> `/api/led`

### Flow node types and fields

- `api` node:
  - Fields: `endpoint`, `method`, `payload`, `continueOnError`.
  - Behavior: sends HTTP request; flow stops on failure unless `continueOnError=true`.
- `delay` node:
  - Field: `delayMs`.
  - Behavior: sleeps for the specified milliseconds.
- `condition` node:
  - Field: `expression` (JavaScript expression).
  - Context: can reference `ctx.lastStatus`, `ctx.lastJson`, `ctx.lastRaw`, `ctx.lastRecognized`.
  - Behavior: node passes only when expression evaluates truthy; otherwise flow fails.
- `keyword_wait` node:
  - Fields: `keyword`, `timeoutMs`, `pollMs`, `source`, `caseSensitive`, `onlyNew`.
  - Behavior: polls conversation logs until keyword appears or timeout expires.
  - `source` options: `user`, `coach`, `any`.

### Execution model

- Flow runs strictly in order from step 1 to N.
- Per-step visual state:
  - `running` while executing
  - `ok` on success
  - `error` on failure
- Stop behavior:
  - `Stop` requests cancellation of the active run token.
  - Next loop boundary or poll cycle exits with `Stopped`.
- Import/export behavior:
  - Export writes an array of step objects (no binary data).
  - Import validates known `type` values and skips unsupported entries.

### Flow JSON example

```json
[
  {
    "type": "api",
    "name": "Set concise prompt",
    "endpoint": "/api/llm/prompt",
    "method": "POST",
    "payload": {
      "prompt": "You are a concise rehab coach. Keep replies under 2 sentences."
    },
    "continueOnError": false
  },
  {
    "type": "api",
    "name": "Speak intro",
    "endpoint": "/api/speak",
    "method": "POST",
    "payload": {
      "text": "Hello, let's begin today's session.",
      "voice": "en_US",
      "speed": 1.0,
      "volume": 1.0
    },
    "continueOnError": false
  },
  {
    "type": "keyword_wait",
    "name": "Wait for thanks",
    "keyword": "thanks",
    "timeoutMs": 12000,
    "pollMs": 350,
    "source": "user",
    "caseSensitive": false,
    "onlyNew": true
  },
  {
    "type": "api",
    "name": "Happy face",
    "endpoint": "/api/face",
    "method": "POST",
    "payload": {
      "mode": "happy",
      "seconds": 3
    },
    "continueOnError": false
  }
]
```

## Python Voice Service (ASR + LLM)

Service file: `python_voice_service/main.py`

### Main endpoints

- `POST /transcribe` -> ASR output (Vosk-like JSON fields + metadata)
- `POST /respond` -> LLM reply generation
- `GET /respond/config` -> current runtime/system prompt
- `POST /respond/config` -> set/reset runtime prompt
- `GET /respond/metrics` -> reply latency metrics
- `GET /healthz` -> health check

### Quick run

```bash
cd python_voice_service
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Useful environment variables

- Whisper model/runtime:
  - `WHISPER_MODEL_PATH`
  - `WHISPER_DEVICE`
  - `WHISPER_COMPUTE_TYPE`
- Wake word normalization:
  - `WAKE_WORD`
  - `WAKE_WORD_ALIASES`
  - `WAKE_WORD_PREFIXES`
- Ollama:
  - `OLLAMA_BASE_URL`
  - `OLLAMA_MODEL`
  - `OLLAMA_SYSTEM_PROMPT`

More details are documented in [`python_voice_service/README.md`](python_voice_service/README.md).

## TTS Backends (Piper and Qwen)

Both wrappers expose compatible `/speak` routes:
- Piper wrapper: `python_voice_service/piper_http.py`
- Qwen wrapper: `python_voice_service/qwen_tts_http.py`

Typical defaults:
- Piper on `5005`
- Qwen on `5006`

Override commands explicitly with launcher arguments:

```powershell
python scripts/start_local_services.py `
  --hub-cmd "<your hub command>" `
  --piper-http-cmd "uvicorn piper_http:app --host 0.0.0.0 --port 5005" `
  --qwen-http-cmd "uvicorn qwen_tts_http:app --host 0.0.0.0 --port 5006"
```

If you use Qwen TTS and Faster-Whisper together, keep separate virtual environments to avoid dependency conflicts.

## Troubleshooting

- No speech recognized:
  - Check microphone permission.
  - Verify Unity is configured to use the expected recognizer endpoint (`/transcribe` or your custom source).
  - Confirm Unity component references are not null.
- MQTT commands not received:
  - Verify broker host/port and credentials.
  - Confirm topic names match your robot-side subscribers.
- `/api/speak` fails:
  - Ensure Unity is running and `UserTestControlPanel` server is started on `8787`.
- `/transcribe` fails:
  - Check `WHISPER_MODEL_PATH` and model availability.
  - Check Python service logs for load/runtime errors.
- `/respond` is slow or failing:
  - Confirm Ollama is reachable and model exists.
  - Use `/respond/metrics` for latency visibility.

## Development and Tests

SDK tests:

```bash
pip install -r python_sdk/requirements-dev.txt
python -m pytest tests/test_voice_agent_sdk.py
```

When updating SDK behavior, keep `tests/test_voice_agent_sdk.py` aligned with API expectations.

## License

MIT License. See [`LICENSE`](LICENSE).
