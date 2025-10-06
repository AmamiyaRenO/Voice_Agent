# Voice Agent for Robot OPR

This repository contains the Unity client that powers the spoken interface
for the [Robot_opr](https://github.com/AmamiyaRenO/Robot_opr) rehabilitation
robot. It wraps the [Vosk](https://alphacephei.com/vosk/) offline speech
recogniser, forwards recognised intents to the robot control stack over MQTT
and can optionally delegate transcription to a Python service that runs the
[Faster-Whisper](https://github.com/guillaumekln/faster-whisper) model. The
Unity scenes included here were used to drive the coach-style voice assistant
seen in the project demos.

## Features

* **Unity-first voice experience** – Prefab components (`VoskSpeechToText`,
  `VoiceGameLauncher`, `VoiceGameWiring`) take care of microphone capture,
  wake-word detection and intent routing.
* **Built-in MQTT publisher** – When the `ROBOTVOICE_USE_MQTT` scripting
  define is enabled, the agent publishes launch/exit messages to the
  `robot/intent` topic using a lightweight client that ships with the project,
  so no external DLLs are required.
* **Python transcription fallback** – Stream microphone audio to the
  `python_voice_service` FastAPI application if you prefer Faster-Whisper over
  the bundled Vosk models.
* **One-command local tooling** – `scripts/start_local_services.py` can boot
  the MQTT hub, Python voice service and an optional orchestrator together.
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

2. **Open the Unity project**
   * Launch Unity Hub and add the `Voice_Agent` folder as a project.
   * Load the provided scene and locate the `VoskSpeechToText` component.
   * Place a zipped Vosk model inside `Assets/StreamingAssets/` and set the
     `ModelPath` field to its filename (the archive will be extracted on first
     run).
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

4. **(Optional) Run the Python voice service**
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

5. **Launch the full local stack (optional)**
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
