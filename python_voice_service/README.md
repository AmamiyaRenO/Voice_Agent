# Python Voice Service

This folder contains a lightweight FastAPI application that wraps the
[Faster-Whisper](https://github.com/guillaumekln/faster-whisper) model
so Unity can offload speech recognition to Python. The REST endpoint
returns Vosk-compatible JSON payloads, allowing the existing
`VoiceGameLauncher` logic to keep publishing intents to the message hub
without any changes.

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
   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

   The Unity scene expects the default URL `http://127.0.0.1:8000/transcribe`.

3. Use the `/healthz` endpoint to confirm the service is ready.

When Unity detects speech the `VoskSpeechToText` component serialises the
PCM samples, posts them to `/transcribe` and reuses the JSON response to
update the intent pipeline. No scene changes are required to keep
publishing to the MQTT message hub.
