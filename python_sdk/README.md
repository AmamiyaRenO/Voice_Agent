# Python SDK for Robot Voice Agent

This SDK wraps the Robot Voice Agent's HTTP TTS and MQTT control channels in a
single Python client. It mirrors the same controls available in the Unity
`UserTestControlPanel`, including face presets, servo/flower actions, LED
effects, TTS options, runtime LLM prompt editing, and game launch/exit.

The project also provides an in-browser **SDK Visualizer** (`/sdk`) that hits
the same panel APIs and helps you validate payloads before writing Python code.

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

# Runtime LLM system prompt (real-time, via UserTestControlPanel /api/llm/prompt)
cfg = client.get_llm_prompt()
client.set_llm_prompt("You are a concise rehab coach. Keep replies to 1-2 sentences.")
# client.reset_llm_prompt()
```

## Supported features (parity with UserTestControlPanel)

- **Face presets**: happy/neutral/sad/verySad/excited/idle/custom
- **Servo actions**: open/close/open_hold/close_hold/center/stop/open_slow/close_slow
- **LED effects**: breathe/solid/random/off
- **TTS**: speak text, set voice/model options
- **LLM system prompt**: get/set/reset runtime `/respond` prompt through UserTestControlPanel
- **Game**: launch/exit intents

## SDK Visualizer (Unity UserTestControlPanel)

When Unity is running with `UserTestControlPanel` enabled (default port `8787`),
open:

- `http://<host-ip>:8787/sdk`
- `http://<host-ip>:8787/sdk.html`

What the Visualizer provides:

- **SDK Method Sandbox**
  - Select a method template (for example `speak`, `set_llm_prompt`, `launch_game`).
  - Auto-load endpoint + payload JSON.
  - Invoke request and inspect HTTP status / response quickly.
- **Flow Builder**
  - Drag-and-drop API templates into a sequence.
  - Add utility nodes: `delay(ms)`, `condition(expr)`, `wait_keyword(keyword)`.
  - Configure each step (method, endpoint, payload, continue-on-error).
  - Run/stop flow with per-step states and execution log.
  - Export/import flows as JSON for repeatable testing.
- **Condition/Keyword Debugging**
  - Condition expressions can reference context values like
    `ctx.lastStatus`, `ctx.lastJson`, `ctx.lastRaw`, `ctx.lastRecognized`.
  - Keyword wait supports source filters (`user`/`coach`/`any`), timeout,
    poll interval, case-sensitivity, and "only new text" matching.

This is useful for rapid API and behavior verification before automating with
`VoiceAgentClient`.

## Tests

```bash
pip install -r python_sdk/requirements-dev.txt
python -m pytest
```
