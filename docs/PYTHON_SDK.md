# Python SDK (Robot Voice Agent)

This document describes the Python SDK that mirrors the features exposed by
`UserTestControlPanel`. The SDK talks to the existing HTTP TTS endpoint and the
MQTT topics already used by the Unity client.

## What it covers

- **TTS (audible)**: `POST /api/speak` via Unity `UserTestControlPanel` (requires Unity running).
- **TTS (synthesis only)**: `GET/POST /speak` (Piper HTTP returns WAV bytes; no playback).
- **Face presets**: publish to `robot/pi/face/cmd`.
- **Servo / flower control**: publish to `robot/pi/servo/cmd`.
- **LED control**: publish to `robot/pi/led/cmd`.
- **LLM prompt**: get/set/reset runtime `/respond` system prompt via `UserTestControlPanel` (`/api/llm/prompt`).
- **TTS options**: publish to `robot/tts/options`.
- **Game launch / exit**: publish to `robot/intent`.

These topics match the Unity client expectations and the integration guide.

## Install

```bash
pip install -r python_sdk/requirements.txt
```

## Usage

```python
from voice_agent_sdk import VoiceAgentClient

client = VoiceAgentClient(host="10.0.0.1")

# Speak (audible) via Unity UserTestControlPanel (/api/speak)
# model should be an absolute .onnx path, matching the User Panel dropdown.
client.speak("hi", voice="en_US", model=r"D:\piper\models\en_US-amy-medium.onnx", speed=1.0, volume=1.0)

# Or synthesize only (returns WAV bytes) by calling Piper HTTP directly (/speak)
wav_bytes = client.synthesize_wav("Hello Rachel", model=r"D:\piper\models\en_US-amy-medium.onnx")

# Face presets
client.face_happy(duration=3)
client.face_idle()

# LED
client.led_breathe(color="#00BFFF", brightness=0.8, period=2.5)
client.led_off()

# Servo / flower
client.servo_open_hold()
client.servo_close_slow()

# TTS options
client.set_tts_options(voice="en_US", model=r"D:\piper\models\en_US-amy-medium.onnx")

# Runtime LLM system prompt (real-time via UserTestControlPanel)
cfg = client.get_llm_prompt()
client.set_llm_prompt("You are a concise rehab coach. Keep replies to 1-2 sentences.")
# client.reset_llm_prompt()

# Game intents
client.launch_game("cornhole")
client.exit_game()
```

## Notes

- The LED/servo payloads follow the same format that the Unity
  `PiMessageHub` publishes (colon-delimited values inside the MQTT payload).
- TTS options are sent on dedicated MQTT topics used by the
  Unity `VoiceGameLauncher`.
