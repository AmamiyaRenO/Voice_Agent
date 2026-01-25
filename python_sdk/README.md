# Python SDK for Robot Voice Agent

This SDK wraps the Robot Voice Agent's HTTP TTS and MQTT control channels in a
single Python client. It mirrors the same controls available in the Unity
`UserTestControlPanel`, including face presets, servo/flower actions, LED
effects, TTS options, dialog style, and game launch/exit.

## Installation

```bash
pip install -r python_sdk/requirements.txt
```

## Quick start

```python
from voice_agent_sdk import VoiceAgentClient

client = VoiceAgentClient(host="10.0.0.1")
client.speak("Hello Rachel")
client.face_happy(duration=3)
client.led_breathe(color="#00BFFF", brightness=0.8, period=2.5)
client.servo_open_hold()
client.launch_game("cornhole")
```

## Supported features (parity with UserTestControlPanel)

- **Face presets**: happy/neutral/sad/verySad/excited/idle/custom
- **Servo actions**: open/close/open_hold/close_hold/center/stop/open_slow/close_slow
- **LED effects**: breathe/solid/random/off
- **TTS**: speak text, set voice/model options
- **Dialog style**: publish LLM style changes
- **Game**: launch/exit intents

## Tests

```bash
pip install -r python_sdk/requirements-dev.txt
python -m pytest
```
