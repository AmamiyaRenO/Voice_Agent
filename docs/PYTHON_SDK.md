# Python SDK

The Python SDK is a thin HTTP client for the desktop runtime on port `8787`.
It is intentionally a strict mirror of the non-Qwen control surface exposed by:

- `Assets/StreamingAssets/panel/panel.html`
- `Assets/StreamingAssets/panel/sdk.html`
- `Assets/StreamingAssets/panel/sdk-manifest.json`

The browser SDK Visualizer and `VoiceAgentClient` now read from the same checked-in manifest, so method add/remove/update happens in one place.

## Install

```bash
pip install -r python_sdk/requirements.txt
```

## Quick start

```python
from voice_agent_sdk import VoiceAgentClient

client = VoiceAgentClient(host="10.0.0.1")

client.speak("Hello Rachel", voice="en_US", backend="piper")
client.set_tts_backend("kokoro")
client.set_kokoro_voice("af_heart")
client.kokoro_speak("Hello from Kokoro")

client.face_happy(seconds=3)
client.led_breathe(color="#00BFFF", brightness=0.8, period=2.5)
client.flower_open()
client.launch_game("cornhole")

client.set_llm_prompt("You are a concise rehab coach. Keep replies to 1-2 sentences.")
client.set_local_model("qwen3.5:0.8b")
client.describe_current_camera("Describe what you see and whether the user is ready.")
```

## Public surface

`VoiceAgentClient` exposes the same public methods listed in `sdk-manifest.json`:

- Logs and options: `get_logs`, `get_tts_options`, `get_kokoro_options`
- TTS: `speak`, `set_voice`, `set_tts_model`, `set_tts_backend`, `set_kokoro_voice`, `kokoro_speak`
- Runtime prompt/model: `get_llm_prompt`, `set_llm_prompt`, `reset_llm_prompt`, `get_runtime_config`, `set_local_model`
- ASR: `get_asr_status`, `set_asr_mode`, `set_backend_asr_mode`, `start_listening`, `pause_listening`
- Vision/game: `describe_current_camera`, `launch_game`, `exit_game`
- Face: `face_preset`, `face_custom`, `face_happy`, `face_neutral`, `face_sad`, `face_very_sad`, `face_excited`
- LED: `led_breathe`, `led_solid`, `led_random`, `led_off`
- Flower: `flower_open`, `flower_close`, `flower_stop`, `flower_open_slow`, `flower_close_slow`

Removed from the SDK surface:

- Methods from the retired alternate TTS backend
- `synthesize_wav`
- `set_dialog_style`
- `publish_raw`
- `face_idle`
- Servo-named aliases such as `servo_open_hold`, `servo_close_hold`, and `servo_center_hold`

## Visualizer parity

Open either:

- `http://<host-ip>:8787/sdk`
- `http://<host-ip>:8787/sdk.html`

The visualizer loads `sdk-manifest.json` at runtime, so its method picker, flow templates, and the Python SDK stay aligned with the full Assets control panel.

## Tests

```bash
pip install -r python_sdk/requirements-dev.txt
python -m pytest tests/test_voice_agent_sdk.py
```
