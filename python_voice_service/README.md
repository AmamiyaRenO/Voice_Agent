# Python Voice Service

This folder contains a FastAPI application that combines
[Vosk](https://alphacephei.com/vosk/) wake-word detection with the
[Faster-Whisper](https://github.com/guillaumekln/faster-whisper) model
so Unity can offload speech recognition to Python. Incoming audio first
flows through a wake detector that is limited to pronunciation variants
of the keyword “richel”. Only when a match is detected does the API
issue a short-lived wake token. The follow-up transcription endpoint
requires that token and returns a Vosk-compatible JSON payload, allowing
the existing `VoiceGameLauncher` logic to keep publishing intents to the
message hub without any changes. A companion `/respond` endpoint can
forward the transcribed text to a local [Ollama](https://ollama.com/)
instance that runs Meta's Llama 3.1 model, enabling short spoken replies
from the coach voice agent.

## Requirements

* Python 3.10 or newer
* The Faster-Whisper model weights downloaded to your machine. The
  screenshot in the task corresponds to a folder such as
  `D:/Data/unityproject/faster-whisper-large-v3` on Windows. Set the
  `WHISPER_MODEL_PATH` environment variable to that directory before
  starting the service.
* A Vosk model folder for wake-word detection. Download one of the
  English models (for example `vosk-model-small-en-us-0.15`) and set the
  `VOSK_MODEL_PATH` environment variable to that directory.

Install dependencies with:

```bash
python -m venv .venv
.venv\\Scripts\\activate  # On PowerShell / cmd use .venv\Scripts\activate.bat
pip install -r requirements.txt
```

> **Tip:** On macOS/Linux activate the virtual environment with
> `source .venv/bin/activate`.

## Running the service

1. Export the environment variables that control model loading:

   ```bash
   export WHISPER_MODEL_PATH="/path/to/faster-whisper-large-v3"
   export WHISPER_DEVICE=cpu          # or "cuda" if you have GPU support
   export WHISPER_COMPUTE_TYPE=int8   # tweak if you use CUDA (e.g. float16)
   ```

   On Windows PowerShell replace `export` with `$env:VAR = "value"`.

2. Start the API:

   ```bash
   # Optional: customise the Ollama integration
   export OLLAMA_BASE_URL="http://127.0.0.1:11434"
   export OLLAMA_MODEL="llama3.1:8b-instruct"
   export OLLAMA_SYSTEM_PROMPT="You are the Coach Voice Agent..."

   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

   The Unity scene first calls `POST /wake` with continuous microphone
   frames. When the response contains `"wake_word_detected": true`, play
   `helper.mp3`, record five seconds of audio and send that recording to
   `POST /transcribe` together with the `wake_token` value from the wake
   response. The transcribe endpoint runs Faster-Whisper and returns the
   familiar Vosk-style payload.

3. Use the `/healthz` endpoint to confirm the service is ready.

When Unity detects speech the `VoskSpeechToText` component should
continuously serialise microphone frames to `/wake`. Once a wake token is
issued, play `helper.mp3`, capture the follow-up clip and send it to
`/transcribe` with the token. The JSON payload matches what the existing
intent pipeline expects, so no downstream MQTT integration changes are
required.

## Generating coach replies with Ollama

The `/respond` endpoint relays recognised text to a local Ollama
deployment. By default it targets `http://127.0.0.1:11434/api/generate`
with the `llama3.1:8b-instruct` model and the coach system prompt. Send
a POST request with a JSON body containing the `text` field:

```bash
curl -X POST "http://127.0.0.1:8000/respond" \
     -H "Content-Type: application/json" \
     -d '{"text": "Start the balance exercise"}'
```

The response contains the generated `text`, ready to be spoken by the
Unity client. The `VoiceGameLauncher` script forwards both launch/exit
intents and general wake-word commands to `/respond`, so the coach can
answer free-form questions alongside the existing keyword workflows.

