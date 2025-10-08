# Python Voice Service

This folder contains a lightweight FastAPI application that wraps the
[Faster-Whisper](https://github.com/guillaumekln/faster-whisper) model
so Unity can offload speech recognition to Python. The REST endpoint
returns Vosk-compatible JSON payloads, allowing the existing
`VoiceGameLauncher` logic to keep publishing intents to the message hub
without any changes. A companion `/respond` endpoint can forward the
transcribed text to a local [Ollama](https://ollama.com/) instance that
runs Meta's Llama 3.1 model, enabling short spoken replies from the
coach voice agent.

## Requirements

* Python 3.10 or newer
* The Faster-Whisper model weights downloaded to your machine. The
  screenshot in the task corresponds to a folder such as
  `D:/Data/unityproject/faster-whisper-large-v3` on Windows. Set the
  `WHISPER_MODEL_PATH` environment variable to that directory before
  starting the service.

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

   The Unity scene expects the default URL `http://127.0.0.1:8000/transcribe`.

3. Use the `/healthz` endpoint to confirm the service is ready.

When Unity detects speech the `VoskSpeechToText` component serialises the
PCM samples, posts them to `/transcribe` and reuses the JSON response to
update the intent pipeline. No scene changes are required to keep
publishing to the MQTT message hub.

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

When [Piper](https://github.com/rhasspy/piper) is configured, `/respond`
also returns the coach reply as a base64-encoded WAV file (`audio_wav_base64`)
together with the WAV sample rate. This lets Unity or any other client play
the generated speech without calling another endpoint.

### Enabling Piper text-to-speech

1. Install Piper locally. On Windows the recommended approach is to use
   the pre-built binaries provided by the Piper project. Ensure the
   executable is available on your `PATH` or note the full path.

2. Download a voice model (e.g. `en_US-amy-medium.onnx`) together with its
   `.onnx.json` metadata file.

3. Export the environment variables before launching the service:

   ```bash
   export PIPER_MODEL_PATH="/path/to/en_US-amy-medium.onnx"
   export PIPER_EXECUTABLE="/path/to/piper"   # optional, defaults to "piper"
   export PIPER_SPEAKER="0"                   # optional, required for multi-speaker voices
   ```

   Set `PIPER_CONFIG_PATH` if the `.onnx.json` file lives in a different
   location. Otherwise the service automatically picks up the
   `<model>.onnx.json` file next to the model weights.

With these variables set, the `start_local_services.py` helper launches the
Python voice service and every `/respond` request includes both the text and
the synthesised audio.

