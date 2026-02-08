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
   export WHISPER_MAX_AUDIO_SECONDS=5.0
   export WHISPER_VAD_SILENCE_MS=250
   export WHISPER_VAD_MIN_SPEECH_MS=150
   export WHISPER_RECENT_WINDOW_PAD_MS=100
   export WHISPER_CONDITION_ON_PREVIOUS_TEXT=false
   ```

   On Windows PowerShell replace `export` with `$env:VAR = "value"`.

   The service trims each request down to the most recent window of speech by
   running a lightweight voice-activity detector before invoking Whisper. This
   keeps short commands from being swamped by long histories and makes it easier
   for smaller checkpoints to avoid looping. You can tweak the trimming window
   and VAD behaviour with the `WHISPER_MAX_AUDIO_SECONDS`,
   `WHISPER_VAD_SILENCE_MS`, `WHISPER_VAD_MIN_SPEECH_MS`, and
   `WHISPER_RECENT_WINDOW_PAD_MS` variables shown above. Conditioning on earlier
   decoder text is now disabled by default to stop Whisper from reinforcing its
   own repeats; set `WHISPER_CONDITION_ON_PREVIOUS_TEXT=true` if you want the old
   behaviour back.

   When loops are detected in the recognised text the service still performs an
   automatic retry with stronger repetition penalties. Keeping the baseline
   values moderate preserves normal accuracy while letting the retry clamp
   runaway phrases from smaller Whisper checkpoints. If the decoder still
   insists on echoing the same short wake phrase after those retries, the
   response is collapsed down to a single occurrence so Unity never receives a
   long "hi rachael" chain.

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

## TTS backends on port 5005 (Piper vs Qwen3-TTS)

Unity fetches speech audio via `GET http://127.0.0.1:5005/speak?text=...` (WAV).
This repo ships two compatible HTTP wrappers:

- `piper_http.py`: wraps the Piper CLI (`piper.exe`) and ONNX voice models.
- `qwen_tts_http.py`: wraps **Qwen3-TTS 0.6B** (`Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice`) via the `qwen-tts` Python package.

Both expose:

- `GET /speak?text=...&voice=...&instruct=...` -> `audio/wav`
- `POST /speak` -> base64 WAV JSON (optional)

### Switching quickly (no Unity changes)

The local launcher (`scripts/start_local_services.py`) reads `VOICE_AGENT_PIPER_HTTP_CMD`.
Point it at whichever wrapper you want to run on port 5005.

Piper:

```powershell
$env:VOICE_AGENT_PIPER_HTTP_CMD="uvicorn piper_http:app --host 0.0.0.0 --port 5005"
```

Qwen (CPU-only friendly):

```powershell
$env:VOICE_AGENT_PIPER_HTTP_CMD="uvicorn qwen_tts_http:app --host 0.0.0.0 --port 5005"
$env:QWEN_TTS_MODEL="Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
$env:QWEN_TTS_DEVICE_MAP="cpu"
$env:QWEN_TTS_DTYPE="float32"
$env:QWEN_TTS_CACHE_SIZE="8"
$env:QWEN_TTS_NUM_THREADS="4"
$env:QWEN_TTS_NUM_INTEROP="1"
```

Performance knobs (optional):

- `QWEN_TTS_CACHE_SIZE`: cache last N request results (set `0` to disable).
- `QWEN_TTS_NUM_THREADS` / `QWEN_TTS_NUM_INTEROP`: control Torch CPU thread usage.
- `QWEN_TTS_MATMUL_PRECISION`: e.g. `high`, `medium`, `highest` (Torch matmul precision).
- `QWEN_TTS_TF32`: set to `1` to enable TF32 on CUDA GPUs.

#### Important: Qwen TTS uses a separate venv (dependency conflict)

`qwen-tts` depends on a recent `transformers/tokenizers` stack, while `faster-whisper`
pins an older `tokenizers` version. Install them into **separate virtual environments**:

- **ASR/LLM venv** (port 8000): uses `requirements.txt`
- **TTS venv** (port 5005): uses `requirements_qwen_tts.txt`

Example:

```powershell
cd D:\unityproject\Voice_Agent\python_voice_service

# ASR + /respond
py -3.12 -m venv .venv_asr
.\.venv_asr\Scripts\python.exe -m pip install -U pip
.\.venv_asr\Scripts\python.exe -m pip install -r requirements.txt

# Qwen TTS (/speak on 5005)
py -3.12 -m venv .venv_tts
.\.venv_tts\Scripts\python.exe -m pip install -U pip
.\.venv_tts\Scripts\python.exe -m pip install -r requirements_qwen_tts.txt
```

Then point the launcher to the right Python executables:

```powershell
$asrPy="D:\unityproject\Voice_Agent\python_voice_service\.venv_asr\Scripts\python.exe"
$ttsPy="D:\unityproject\Voice_Agent\python_voice_service\.venv_tts\Scripts\python.exe"
$env:VOICE_AGENT_VOICE_CMD="$asrPy -m uvicorn main:app --host 0.0.0.0 --port 8000"
$env:VOICE_AGENT_PIPER_HTTP_CMD="$ttsPy -m uvicorn qwen_tts_http:app --host 0.0.0.0 --port 5005"
```

### Benchmark

```powershell
python python_voice_service/bench_tts_http.py --url "http://127.0.0.1:5005/speak" --runs 3
```
