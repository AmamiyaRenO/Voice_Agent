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
   # Optional decoding/audio guards against repeated phrases when using smaller models
   export WHISPER_NO_REPEAT_NGRAM_SIZE=4
   export WHISPER_REPETITION_PENALTY=1.15
   export WHISPER_LENGTH_PENALTY=1.0
   export WHISPER_MAX_AUDIO_SECONDS=3.5
   export WHISPER_VAD_SILENCE_MS=180
   export WHISPER_VAD_MIN_SPEECH_MS=150
   export WHISPER_RECENT_WINDOW_PAD_MS=60
   export WHISPER_CONDITION_ON_PREVIOUS_TEXT=false
   ```

   On Windows PowerShell replace `export` with `$env:VAR = "value"`.

   The service trims each request down to the most recent window of speech by
   running a lightweight voice-activity detector before invoking Whisper. The
   tighter defaults (3.5 s window, 60 ms pad, 180 ms silence gate) cut most
   trailing quiet frames so the decoder only sees the fresh command. You can
   still relax these limits with the `WHISPER_MAX_AUDIO_SECONDS`,
   `WHISPER_VAD_SILENCE_MS`, `WHISPER_VAD_MIN_SPEECH_MS`, and
   `WHISPER_RECENT_WINDOW_PAD_MS` variables if a particular microphone keeps
   chopping words. Conditioning on earlier decoder text remains disabled by
   default to stop Whisper from reinforcing its own repeats; set
   `WHISPER_CONDITION_ON_PREVIOUS_TEXT=true` if you want the old behaviour back.

   When loops are detected in the recognised text the service performs at most a
   single repetition-aware retry with stronger penalties. The retry now only
   kicks in for longer phrases or extremely compressed outputs so quick wake
   commands do not trigger an expensive second decode. If the decoder still
   insists on echoing the same short wake phrase after the retry, the response
   is collapsed down to a single occurrence so Unity never receives a long "hi
   rachael" chain.

   Confidence-based retries also respect a tighter threshold: the CPU-friendly
   default only runs the higher-temperature pass when both the average
   log-probability and the language probability fall well below normal ranges.
   This keeps the latency closer to the 3–4 second mark on Ryzen-class laptops
   while leaving the environment variables in place if you need to revert to
   the more aggressive fallback behaviour.

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

