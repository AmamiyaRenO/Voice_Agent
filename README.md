# Voice Agent for Robot OPR

This repository contains the Unity client that powers the spoken interface for
the [Robot_opr](https://github.com/AmamiyaRenO/Robot_opr) rehabilitation
robot. The current speech pipeline is built around Windows 11 **Live Captions**:
the `LiveCaptionsListener` utility taps into the system captions surface and
publishes each completed sentence to MQTT, allowing the Unity experience to
react without running a heavyweight ASR model in-process. Legacy Vosk and
Faster-Whisper components remain available as optional fallbacks, but the Live
Captions bridge fully replaces them for the default deployment.

## Live Captions MQTT bridge (default pipeline)

`scripts/live_captions/LiveCaptionsListener.cs` compiles into a small Windows
console app that watches the Live Captions UI Automation tree. Configure the
MQTT target via environment variables (or equivalent `--mqtt-*` command-line
switches):

- `LIVE_CAPTIONS_MQTT_HOST` – broker address (default `127.0.0.1`).
- `LIVE_CAPTIONS_MQTT_PORT` – broker port (default `1883`).
- `LIVE_CAPTIONS_MQTT_TOPIC` – topic for published sentences (default
  `robot/live_captions`). Set to an empty string to disable MQTT output.
- `LIVE_CAPTIONS_MQTT_CLIENT_ID` – MQTT client identifier (default
  `live-captions-{machine}`).
- `LIVE_CAPTIONS_MQTT_USERNAME` / `LIVE_CAPTIONS_MQTT_PASSWORD` – optional
  credentials.
- `LIVE_CAPTIONS_SOURCE_LABEL` – value written to the `source` field in the
  JSON payload (default `live_captions`).

Example (Windows PowerShell):

```powershell
$env:LIVE_CAPTIONS_MQTT_HOST="192.168.1.10"
$env:LIVE_CAPTIONS_MQTT_TOPIC="robot/live_captions"
Start-Process -FilePath "scripts/live_captions/StartLiveCaptions.bat"
Start-Process -FilePath "C:\\Tools\\LiveCaptionsListener\\LiveCaptionsListener.exe"
```

Every finalised sentence produces console output (`[Sentence] hello world`) and
an MQTT message:

```json
{
  "type": "LIVE_CAPTION",
  "text": "hello world",
  "source": "live_captions",
  "timestamp": "2024-05-17T11:05:30.1234567Z"
}
```

## Optional Faster-Whisper service environment variables

The Python speech service can still be enabled as a fallback recogniser. When
running `python_voice_service/main.py`, the following environment variables are
respected:

- `WHISPER_MODEL_PATH` (default `large-v3`), optional values include
  `medium.en`, `small.en`, etc.
- `WHISPER_COMPUTE_TYPE` (default `int8_float16`; use `float16` if sufficient
  GPU memory is available)
- `WAKE_WORD` (default `rachel`)
- `WAKE_WORD_ALIASES` (default `rachel, rachael, richel, richelle, rachal,
  raychel, ra chel, rach el`; the Python service normalises recognition results
  based on these aliases, eliminating the need for duplicate synonym
  configuration in Unity)
- `WAKE_WORD_PREFIXES` (default `hey, hi`; prompts the recogniser to prioritise
  detecting phrases like “Hey Rachel” while standardising these prefixes on the
  server)

Example (Windows PowerShell):

```powershell
pip install -r python_voice_service/requirements.txt
$env:WHISPER_MODEL_PATH="medium.en"
$env:WHISPER_COMPUTE_TYPE="float16"
$env:WAKE_WORD="rachel"
$env:WAKE_WORD_ALIASES="rachel, richel, richelle"
$env:WAKE_WORD_PREFIXES="hey, hi"
python python_voice_service/main.py
```

## Features

* **Live Captions integration** – The default speech recogniser is Windows 11
  Live Captions. The standalone listener streams captioned sentences over MQTT
  so Unity can focus on intent routing rather than transcription.
* **Unity-first voice experience** – Prefab components (`VoiceGameLauncher`,
  `VoiceGameWiring`) take care of wake-word detection, microphone capture and
  MQTT publishing inside the scene.
* **Legacy offline recognisers** – Vosk and Faster-Whisper integrations are
  still available for deployments that cannot rely on Live Captions.
* **Focus-aware webcam hand-off** – MediaPipe runners stop their
  `WebCamTexture` feeds when the agent is backgrounded, allowing another Unity
  project to claim the camera while the voice agent keeps control of the
  microphone.
* **Built-in MQTT publisher** – When the `ROBOTVOICE_USE_MQTT` scripting define
  is enabled, the agent publishes launch/exit messages to the `robot/intent`
  topic using a lightweight client that ships with the project, so no external
  DLLs are required.
* **One-command local tooling** – `scripts/start_local_services.py` can boot the
  MQTT hub, Python voice service and an optional orchestrator together.
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

* **Unity** 2020.3.48f1 or newer.
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

2. **Start Live Captions and the listener**
   * On Windows 11, press `Win + Ctrl + L` and enable "Include microphone audio".
   * Run the published `LiveCaptionsListener.exe` with your MQTT settings so it
     forwards completed sentences to the broker.
   * Verify that the console prints `[Sentence] ...` lines while the listener is
     active – each entry is mirrored on the MQTT topic.

3. **Open the Unity project**
   * Launch Unity Hub and add the `Voice_Agent` folder as a project.
   * Load the provided scene and ensure the `VoiceGameLauncher` component has a
     `MqttIntentPublisher` assigned so voice intents propagate through MQTT.
   * If you plan to publish intents, enable the `ROBOTVOICE_USE_MQTT` scripting
     define (Project Settings → Player → Scripting Define Symbols).
   * Assign the `wakeWordPromptClip` on `VoiceGameLauncher` to `Assets/Voice/help.mp3`
     (optionally routing it through a dedicated `AudioSource`) and hook up the
     wake listening indicator UI (root GameObject, progress `Image`, countdown
     `Text`) so patients can see the five-second capture window.
   * **Optional (legacy pipeline):** If you still require offline recognition,
     place a Vosk model inside `Assets/StreamingAssets/` and configure the
     `VoskSpeechToText` component as before.

4. **Configure the MQTT publisher**
   * Add the `MqttIntentPublisher` component to the same GameObject as the
     `VoiceGameLauncher` or assign it through the inspector.
   * Point the `Host`, `Port` and credentials fields at the Robot_opr message
     hub. The default topic (`robot/intent`) and payload schema matches the
     Robot_opr subscriber expectations.

5. **(Optional) Run the Python voice service**
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
   Use the helper script if you frequently start the hub and voice model
   together:
   ```bash
   python scripts/start_local_services.py \
       --hub-cmd "<command to start Robot_opr hub>" \
       --orchestrator-cmd "<command to start Robot_opr orchestrator>"
   ```
   The script watches the processes, forwards Ctrl+C and stops the remaining
   services if one exits. You can also supply `--env-file` to preload
   environment variables.

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
for full build instructions. The guide covers the bundled MQTT publisher,
sentence assembly logic, Unity ingestion examples and startup automation tips so
captioned sentences flow straight into the voice agent runtime.

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
