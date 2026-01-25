# Python SDK (Robot Voice Agent)

This document describes the Python SDK that mirrors the features exposed by
`UserTestControlPanel`. The SDK talks to the existing HTTP TTS endpoint and the
MQTT topics already used by the Unity client.

## What it covers

- **TTS**: `GET/POST /speak` for speech playback.
- **Face presets**: publish to `robot/pi/face/cmd`.
- **Servo / flower control**: publish to `robot/pi/servo/cmd`.
- **LED control**: publish to `robot/pi/led/cmd`.
- **Dialog style**: publish to `robot/dialog/style`.
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

# TTS
client.speak("Hello Rachel", voice="en_US", model="piper-en", speed=1.0, volume=1.0)

# Face presets
client.face_happy(duration=3)
client.face_idle()

# LED
client.led_breathe(color="#00BFFF", brightness=0.8, period=2.5)
client.led_off()

# Servo / flower
client.servo_open_hold()
client.servo_close_slow()

# Dialog style + TTS options
client.set_dialog_style("Supportive")
client.set_tts_options(voice="en_US", model="piper-en")

# Game intents
client.launch_game("cornhole")
client.exit_game()
```

## Notes

- The LED/servo payloads follow the same format that the Unity
  `PiMessageHub` publishes (colon-delimited values inside the MQTT payload).
- Dialog style and TTS options are sent on dedicated MQTT topics used by the
  Unity `VoiceGameLauncher`.
