# Python Voice Service

This folder contains a lightweight FastAPI application that wraps the
[Faster-Whisper](https://github.com/guillaumekln/faster-whisper) model
so Unity can offload speech recognition to Python. The REST endpoint
returns speech JSON payloads (including legacy compatibility fields),
allowing the existing `VoiceGameLauncher` logic to keep publishing
intents to the message hub without any changes. A companion `/respond` endpoint can forward the
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
   # Optional: ASR mode switch
   export TRANSCRIBE_MODE=whisper-large-v3  # whisper-large-v3 | moonshine-small | moonshine-medium | api
   # Optional: when TRANSCRIBE_MODE=api (OpenAI STT)
   export OPENAI_API_KEY="sk-..."
   export OPENAI_TRANSCRIBE_MODEL="gpt-4o-mini-transcribe"
   # Optional: prompt bias for OpenAI STT (recommended empty to avoid prompt leakage)
   export OPENAI_TRANSCRIBE_PROMPT=""
   # API language control (defaults already enforce English)
   export ASR_API_LANGUAGE="en"
   export ASR_API_FORCE_LANGUAGE="1"
   # Optional: Moonshine model selection (advanced)
   export MOONSHINE_LANGUAGE="en"
   export MOONSHINE_SMALL_MODEL_PATH=""    # optional override for moonshine-small
   export MOONSHINE_MEDIUM_MODEL_PATH=""   # optional override for moonshine-medium
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
   export OLLAMA_MODEL="gemma3:4b"
   export OLLAMA_SYSTEM_PROMPT="You are the Coach Voice Agent..."

   uvicorn main:app --host 0.0.0.0 --port 8000
   ```

   The Unity scene expects the default URL `http://127.0.0.1:8000/transcribe`.

3. Use the `/healthz` endpoint to confirm the service is ready.

When Unity detects speech the `VoskSpeechToText` component serialises the
PCM samples, posts them to `/transcribe` and reuses the JSON response to
update the intent pipeline. No scene changes are required to keep
publishing to the MQTT message hub.

### Runtime ASR mode switch (Whisper vs OpenAI API vs Moonshine)

You can switch ASR mode without restarting the service:

```bash
# Read current mode
curl "http://127.0.0.1:8000/transcribe/config"

# Switch to OpenAI API mode
curl -X POST "http://127.0.0.1:8000/transcribe/config" \
     -H "Content-Type: application/json" \
     -d '{"mode":"api"}'

# Switch to Whisper large-v3 mode
curl -X POST "http://127.0.0.1:8000/transcribe/config" \
     -H "Content-Type: application/json" \
     -d '{"mode":"whisper-large-v3"}'

# Switch to Moonshine small
curl -X POST "http://127.0.0.1:8000/transcribe/config" \
     -H "Content-Type: application/json" \
     -d '{"mode":"moonshine-small"}'

# Switch to Moonshine medium
curl -X POST "http://127.0.0.1:8000/transcribe/config" \
     -H "Content-Type: application/json" \
     -d '{"mode":"moonshine-medium"}'
```

### OpenAI prompt behavior (important)

- `OPENAI_TRANSCRIBE_PROMPT` is optional and defaults to empty.
- This service does not auto-inject a default OpenAI prompt anymore.
- Prompt is only a soft decoding bias; if you see prompt leakage/hallucination, clear this value first.
- If you change prompt from launcher/runtime config, restart `service_launcher` and `voice_service` so env changes take effect.

### English enforcement and game-term normalization

- In API mode, language is forced to English by default (`ASR_API_FORCE_LANGUAGE=1`, `ASR_API_LANGUAGE=en`).
- The service also normalizes common game terms to improve intent matching:
  - `corn hole` -> `cornhole`
  - `discgolf` / `disc-golf` -> `disc golf`
  - `pickle ball` -> `pickleball`

## Generating coach replies with Ollama

The `/respond` endpoint relays recognised text to a local Ollama
deployment. By default it targets `http://127.0.0.1:11434/api/generate`
with the `gemma3:4b` model and the coach system prompt. Send
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

You can also edit the runtime system prompt without restarting the service:

```bash
# Read current effective prompt
curl "http://127.0.0.1:8000/respond/config"

# Set runtime override
curl -X POST "http://127.0.0.1:8000/respond/config" \
     -H "Content-Type: application/json" \
     -d '{"system_prompt":"You are a concise rehab coach. Keep replies under 2 sentences."}'

# Reset runtime override back to env/default
curl -X POST "http://127.0.0.1:8000/respond/config" \
     -H "Content-Type: application/json" \
     -d '{"reset":true}'
```

## TTS backends (Piper main + Qwen test)

Unity can fetch speech from:

- Piper main: `GET http://127.0.0.1:5005/speak?text=...`
- Qwen test: `GET http://127.0.0.1:5006/speak?text=...`
This repo ships two compatible HTTP wrappers:

- `piper_http.py`: wraps the Piper CLI (`piper.exe`) and ONNX voice models.
- `qwen_tts_http.py`: wraps **Qwen3-TTS 0.6B** (`Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice`) via the `qwen-tts` Python package.

Both expose:

- `GET /speak?text=...&voice=...&instruct=...` -> `audio/wav`
- `POST /speak` -> base64 WAV JSON (optional)

Piper wrapper additionally exposes true streaming:

- `GET /speak_stream?text=...&voice=...` -> chunked `audio/L16` (PCM s16le mono) with `X-Audio-Sample-Rate` header.

### Start both backends together (recommended)

The local launcher now supports two independent commands:

- `VOICE_AGENT_PIPER_HTTP_CMD` -> Piper on `5005`
- `VOICE_AGENT_QWEN_HTTP_CMD` -> Qwen on `5006`

Example:

```powershell
$env:VOICE_AGENT_PIPER_HTTP_CMD="uvicorn piper_http:app --host 0.0.0.0 --port 5005"
$env:VOICE_AGENT_QWEN_HTTP_CMD="uvicorn qwen_tts_http:app --host 0.0.0.0 --port 5006"
$env:QWEN_TTS_MODEL="Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
$env:QWEN_TTS_DEVICE_MAP="cpu"
$env:QWEN_TTS_DTYPE="float32"
$env:QWEN_TTS_CACHE_SIZE="8"
$env:QWEN_TTS_NUM_THREADS="4"
$env:QWEN_TTS_NUM_INTEROP="1"
$env:QWEN_TTS_SPEED_PROFILE="fast"
$env:QWEN_TTS_MAX_TEXT_CHARS="0"
$env:QWEN_TTS_FAST_SHORT_MAX_NEW_TOKENS="240"
python scripts/start_local_services.py
```

ASR on NucBox M6 (no NVIDIA, CPU-only recommended):

```powershell
$env:WHISPER_DEVICE="cpu"
$env:WHISPER_COMPUTE_TYPE="int8"
$env:WHISPER_MODEL_PATH="Systran/faster-whisper-large-v3-turbo"
$env:WHISPER_CPU_THREADS="6"
```

Performance knobs (optional):

- `QWEN_TTS_CACHE_SIZE`: cache last N request results (set `0` to disable).
- `QWEN_TTS_NUM_THREADS` / `QWEN_TTS_NUM_INTEROP`: control Torch CPU thread usage.
- `QWEN_TTS_MATMUL_PRECISION`: e.g. `high`, `medium`, `highest` (Torch matmul precision).
- `QWEN_TTS_TF32`: set to `1` to enable TF32 on CUDA GPUs.
- `QWEN_TTS_WARMUP_TEXT`: optional startup warmup sentence (reduces first-request latency).
- `QWEN_TTS_MIN_NEW_TOKENS` / `QWEN_TTS_MAX_NEW_TOKENS`: clamp generation length.
- `QWEN_TTS_NEW_TOKENS_BASE` / `QWEN_TTS_NEW_TOKENS_PER_CHAR`: dynamic token cap by text length.
- `QWEN_TTS_DO_SAMPLE`: `0` (faster, deterministic) or `1` (more expressive, usually slower).
- `QWEN_TTS_TOP_P` / `QWEN_TTS_TOP_K` / `QWEN_TTS_TEMPERATURE`: used when sampling is enabled.
- `QWEN_TTS_SPEED_PROFILE`: `fast` / `balanced` / `quality` (changes token budget defaults).
- `QWEN_TTS_MAX_TEXT_CHARS`: truncate overly long text before synthesis (latency guardrail, `0` disables).
- `QWEN_TTS_FAST_SHORT_TEXT_LIMIT` / `QWEN_TTS_FAST_SHORT_MAX_NEW_TOKENS`: extra token cap for short texts in `fast` profile.

Metrics:

- `GET /metrics` on port `5005` returns recent latency stats (`avg/p50/p95`) plus
  `avg_sem_wait_ms` and `avg_synth_ms` to distinguish queue wait vs model time.
- `GET /respond/metrics` on port `8000` returns LLM `/respond` latency stats.

Dialog latency knobs (dialog_service):

- `DIALOG_REPLY_COMPRESS`: `1`/`0`
- `DIALOG_MAX_REPLY_SENTENCES`: default `1`
- `DIALOG_MAX_REPLY_WORDS`: default `14`
- `DIALOG_MAX_REPLY_CHARS`: default `0` (disable hard char truncation to avoid cut-off endings)

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

Quick microphone ASR test (records from your default input device and sends to `/transcribe`):

```powershell
python python_voice_service/bench_asr_mic.py --mode moonshine-medium --seconds 4 --runs 3
```

List audio devices:

```powershell
python python_voice_service/bench_asr_mic.py --list-devices
```
