# Voice Agent for Robot OPR

## Primary Speech Pipeline (Windows Live Captions)

本项目的默认/主语音识别路径为 Windows 11 实时字幕（Live Captions）。由 `scripts/LiveCaptionsListener/Program.cs` 持续监听系统 Live Captions 的识别结果，并将句子推送进 Unity 进行后续意图解析与发布，无需本地模型下载，低延迟、稳定可靠，适合作为生产默认方案。

当需要完全离线或更换识别后端时，可启用可选的 `python_voice_service`（Faster-Whisper）作为备用方案。

## Python Speech Service (Faster-Whisper) Environment Variables

> The Windows Live Captions bridge is the default speech-to-text pipeline. The
> following settings only apply when you enable the optional Faster-Whisper
> fallback described later in this document.

- WHISPER_MODEL_PATH (default `large-v3`), optional values include `medium.en`, `small.en`, etc.
- WHISPER_COMPUTE_TYPE (default `int8_float16`; use `float16` if sufficient GPU memory is available)
- WAKE_WORD (default `rachel`)
- WAKE_WORD_ALIASES (default `rachel, rachael, richel, richelle, rachal, raychel, ra chel, rach el`; Python service normalizes recognition results based on these aliases, eliminating the need for duplicate synonym configuration in Unity)
- WAKE_WORD_PREFIXES (default `hey, hi`; prompts the recognizer to prioritize detecting phrases like “Hey Rachel” while standardizing these prefixes on the server)

Example (Windows PowerShell):

Translated with DeepL.com (free version)

```powershell
pip install -r python_voice_service/requirements.txt
$env:WHISPER_MODEL_PATH="medium.en"
$env:WHISPER_COMPUTE_TYPE="float16"
$env:WAKE_WORD="rachel"
$env:WAKE_WORD_ALIASES="rachel, richel, richelle"
$env:WAKE_WORD_PREFIXES="hey, hi"
python python_voice_service/main.py
```

This repository contains the Unity client that powers the spoken interface
for the [Robot_opr](https://github.com/AmamiyaRenO/Robot_opr) rehabilitation
robot. It forwards recognised intents to the robot control stack over MQTT
and can listen to multiple transcription sources. The current production
workflow captures Windows 11 Live Captions output via
[`scripts/LiveCaptionsListener`](scripts/LiveCaptionsListener) and feeds those
sentences into the Unity scene. You can also delegate transcription to the
optional Python service that runs the
[Faster-Whisper](https://github.com/guillaumekln/faster-whisper) model. The
Unity scenes included here were used to drive the coach-style voice assistant
seen in the project demos.

## Features

* **Unity-first voice experience** – Prefab components (`VoskSpeechToText`,
  `VoiceGameLauncher`, `VoiceGameWiring`) take care of microphone capture,
  wake-word detection and intent routing.
* **Wi-Fi test panel** – `UserTestControlPanel` exposes an in-editor HTTP UI so
  therapists can trigger expressions, lighting and the wake flow from a phone
  connected to the same network.
* **Focus-aware webcam hand-off** – MediaPipe runners stop their `WebCamTexture`
  feeds when the agent is backgrounded, allowing another Unity project to claim
  the camera while the voice agent keeps control of the microphone.
* **Built-in MQTT publisher** – When the `ROBOTVOICE_USE_MQTT` scripting
  define is enabled, the agent publishes launch/exit messages to the
  `robot/intent` topic using a lightweight client that ships with the project,
  so no external DLLs are required.
* **Windows Live Captions bridge** – A lightweight UI Automation listener
  harvests Windows 11 Live Captions output and forwards the sentences straight
  into Unity so you can reuse the OS-level speech recogniser without custom
  ASR.
* **Python transcription fallback** – Stream microphone audio to the
`python_voice_service` FastAPI application if you prefer Faster-Whisper or
need an offline alternative to Live Captions.
* **One-command local tooling** – `helper.bat` (Windows) and
  `scripts/start_local_services.py` can boot the MQTT hub, Live Captions
  listener, Python voice service and optional orchestrators together.
* **Robot_opr ready** – Intent payloads mirror the schema expected by the
  Robot_opr orchestration layer, enabling voice controlled exercise launch and
  shutdown without additional glue code.

## Repository layout

```
Assets/                # Unity scenes, prefabs and C# scripts for the voice agent
python_voice_service/  # FastAPI wrapper around Faster-Whisper
scripts/               # Local development helpers (MQTT/voice orchestrator launcher)
ProjectSettings/       # Unity project configuration
```

## Requirements

* **Unity** 2022.3.56f1c1 (matches `ProjectSettings/ProjectVersion.txt`).
* Microphone access on the target platform.
* (Optional) Python 3.10+ if you want to use the Faster-Whisper service.
* A running MQTT broker (Robot_opr ships a message hub suitable for local
  testing).

## Getting started

1. **Clone the projects**
   ```bash
   git clone https://github.com/AmamiyaRenO/Robot_opr.git
   git clone https://github.com/AmamiyaRenO/Voice_Agent.git
   ```
   Start the Robot_opr messaging hub according to its documentation – the
   Unity agent will connect to the same broker.

2. **Open the Unity project**
   * Launch Unity Hub and add the `Voice_Agent` folder as a project.
   * Load the provided scene and locate the `VoskSpeechToText` component.
   * If you plan to publish intents, enable the `ROBOTVOICE_USE_MQTT` scripting
     define (Project Settings → Player → Scripting Define Symbols).
   * Assign the `wakeWordPromptClip` on `VoiceGameLauncher` to `Assets/Voice/help.mp3`
     (optionally routing it through a dedicated `AudioSource`) and hook up the
     wake listening indicator UI (root GameObject, progress `Image`, countdown
     `Text`) so patients can see the five-second capture window.

3. **Configure the MQTT publisher**
   * Add the `MqttIntentPublisher` component to the same GameObject as the
     `VoiceGameLauncher` or assign it through the inspector.
   * Point the `Host`, `Port` and credentials fields at the Robot_opr message
     hub. The default topic (`robot/intent`) and payload schema matches the
     Robot_opr subscriber expectations.

4. **Capture speech via Windows Live Captions**
   * Live Captions is available on Windows 11 (press `Win + Ctrl + L`). Enable
     the **Include microphone audio** option and keep the floating window on
     top of Unity.
   * Build or use the provided
     [`scripts/LiveCaptionsListener`](scripts/LiveCaptionsListener) project and
     run `EnableLcMic.exe`/`LiveCaptionsListener.exe` to mirror the reference
     setup. The listener subscribes to the Live Captions UI Automation tree and
     prints completed sentences with the `[Sentence]` prefix.
   * Forward each sentence into Unity (e.g. through a pipe, MQTT topic or the
     helper batch file) so `VoskSpeechToText`/`VoiceGameLauncher` can treat the
     transcript as if it came from a traditional recogniser. See
     [`docs/LIVE_CAPTIONS_BRIDGE.md`](docs/LIVE_CAPTIONS_BRIDGE.md) for detailed
     wiring instructions.

5. **(Optional) Run the Python voice service fallback**
   ```bash
   cd Voice_Agent/python_voice_service
   python -m venv .venv
   source .venv/bin/activate  # On Windows use .venv\Scripts\activate
   pip install -r requirements.txt
   export WHISPER_MODEL_PATH="/path/to/faster-whisper-large-v3"
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```
   Toggle **Use Python Service** on the `VoskSpeechToText` component and point
   `PythonServiceUrl` to `http://127.0.0.1:8000/transcribe`.

6. **Launch the full local stack (optional)**
   * Windows users can double-click `helper.bat` to start the MQTT hub,
     Live Captions bridge, Faster-Whisper fallback and Unity voice client
     together.
   * For cross-platform setups or when you want to customise the command list,
     use the Python helper:
     ```bash
     python scripts/start_local_services.py \
         --hub-cmd "<command to start Robot_opr hub>" \
         --orchestrator-cmd "<command to start Robot_opr orchestrator>"
     ```
     The script watches the processes, forwards Ctrl+C and stops the remaining
     services if one exits. You can also supply `--env-file` to preload
     environment variables.

## Remote user test panel

The Unity scene now ships with an embedded HTTP server that exposes a browser
panel tailored for therapist / patient trials.

1. Add the `UserTestControlPanel` component (found under `Assets/Scripts`) to a
   convenient GameObject – for example the one that already holds
   `VoiceGameLauncher`.
2. Assign the existing `PiMessageHub` and `VoiceGameLauncher` references in the
   inspector. The default TCP port is **8787**; change it if the machine has a
   conflicting service.
3. Enter Play Mode (or build the scene). The console will print
   `http://<host-ip>:8787/` after the listener starts. Devices on the same Wi-Fi
   can open that address to reach the panel.
4. Use the controls to drive five facial presets (happy/neutral/angry/sad/
   surprised), adjust LED color/brightness/period, open/close the flower servo,
   pick a TTS voice, switch between installed Piper/Coqui models, enter text
   for the robot to speak, or trigger game launch / exit intents. The “Start
   Wake Flow” button still performs the wake-word choreography without
   requiring speech. Every action goes through
   `PiMessageHub` / `VoiceGameLauncher` so the robot reacts immediately.

If you prefer to start/stop the listener manually, untick **Auto Start** on the
component and call `StartServer`/`StopServer` from the inspector’s context menu
or another script.

## Working with Robot_opr

When the Unity client detects the wake phrase ("hi rachel" by default) it plays
the `help.mp3` prompt from `Assets/Voice` and highlights a short five-second
capture window using the configured UI indicator. If that follow-up instruction
contains an exercise command, `VoiceGameLauncher` publishes a JSON payload
describing the request.
The Robot_opr hub consumes the `LAUNCH_GAME` and `BACK_HOME` intent messages to
start or exit the corresponding rehabilitation experience. You can customise
wake words, synonyms and keyword lists through the inspector or by editing the
JSON configuration asset assigned to the launcher component.

For richer interactions (e.g. free-form questions for the virtual coach) enable
responses via the `/respond` endpoint exposed by the Python voice service. The
Robot_opr text-to-speech or speaker pipeline can read the generated replies
back to the patient, keeping the voice-first flow inside a single MQTT/
HTTP-based loop.

## Windows Live Captions bridge

If you need to surface Windows 11 Live Captions inside the Unity experience—for
example when running the agent on a kiosk that already uses Live Captions for
system speech—refer to [`docs/LIVE_CAPTIONS_BRIDGE.md`](docs/LIVE_CAPTIONS_BRIDGE.md)
for a UI Automation listener, sentence assembly logic and Unity ingestion
examples. The guide also includes startup automation tips and transport options
for forwarding captioned sentences into the voice agent runtime.

## Troubleshooting

* **No speech detected** – Confirm the microphone permissions are granted and
  that `VoiceGameWiring` is attached so transcription results reach the
  launcher.
* **MQTT not connecting** – Ensure the broker address matches the Robot_opr hub
  and that any TLS or credential settings line up with your deployment.
* **Python service 404** – Verify the FastAPI app is running and the
  `PythonServiceUrl` includes `/transcribe`.

## License

This project is released under the MIT License. See [LICENSE](LICENSE) for
full terms.
