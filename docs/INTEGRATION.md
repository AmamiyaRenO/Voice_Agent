## Robot Agent Integration Guide (HTTP TTS + MQTT)

This guide shows how any game/app can integrate with the Robot Agent using two simple channels:

- HTTP for Text-to-Speech (TTS)
- MQTT for intents and Raspberry Pi hardware commands (face/servo/LED)

Everything is language-agnostic with minimal dependencies.

---

### 1) Network & Services

- TTS HTTP service: `http://<HOST>:8000`
  - Endpoint used below: `POST /tts`
  - Replace `<HOST>` with the Agent PC IP (e.g., `10.0.0.1` or `127.0.0.1`).

- MQTT Broker: `<HOST>:1883`
  - We recommend QoS 1 for important messages; do not set retain unless needed.
  - Topics listed below follow the `robot/...` namespace.

Assumptions:
- Your game runs on the same PC or the same LAN as the Agent.
- Firewall allows TCP 1883 (MQTT) and TCP 8000 (HTTP) inbound within your LAN.

---

### 2) Quick Start (TL;DR)

1) Speak a line of text (TTS):

```bash
curl -X POST "http://10.0.0.1:8000/tts" \
  -H "Content-Type: application/json" \
  -d '{"text":"Hello, world!","voice":"zh_CN","play":true}'
```

2) Show a face expression on the Pi display via MQTT:

Topic: `robot/pi/face/cmd`

Payload:

```json
{"action":"face","value":"happy","duration":3,"fade":0.5}
```

3) Open the flower (servo) at 40% speed:

Topic: `robot/pi/servo/cmd`

Payload:

```json
{"action":"servo","value":"open","speed":40}
```

4) Breathe LED in cyan, 2.5s period:

Topic: `robot/pi/led/cmd`

Payload:

```json
{"action":"led","value":"on","color":"#00BFFF","brightness":0.8,"effect":"breathe","period":2.5}
```

5) Ask the orchestrator to launch a game (Agent usually sends this, but games can observe/emit if needed):

Topic: `robot/intent`

Payload:

```json
{"type":"LAUNCH_GAME","game_name":"cornhole","source":"ui_dwell"}
```

---

### 3) HTTP API – TTS

- Endpoint
  - `POST http://<HOST>:8000/tts`

- Request (JSON)
  - `text` (string, required): text to speak
  - `voice` (string, optional): voice/language code, e.g., `zh_CN`
  - `speed` (float, optional, default 1.0): speech rate multiplier
  - `volume` (float, optional, default 1.0): volume multiplier
  - `play` (bool, optional, default true): whether the Agent should auto-play

Example:

```json
{
  "text": "你好，我是机器人。",
  "voice": "zh_CN",
  "speed": 1.0,
  "volume": 1.0,
  "play": true
}
```

- Response
  - Implementation-specific. Typical return: `{ "ok": true }` or an audio URL/ID.
  - On error: 4xx/5xx with a message.

---

### 4) MQTT – Topics & Payloads

Connect to `mqtt://<HOST>:1883`. For most SDKs, defaults are fine. Recommended:
- QoS 1 for commands; `retain=false` unless you know you need retained.
- UTF-8 JSON payloads.

Core topics:

- Intents (Agent side; orchestration messages)
  - Publish: `robot/intent`
  - Examples:
    - Launch a game
      ```json
      {"type":"LAUNCH_GAME","game_name":"cornhole","source":"unity_ui"}
      ```
    - Exit a game
      ```json
      {"type":"EXIT_GAME","game_name":"cornhole","source":"game_ui"}
      ```

- Game state (optional, game -> Agent)
  - Publish: `robot/game/<game_name>/state`
  - Examples:
    - Started
      ```json
      {"event":"started"}
      ```
    - Result/Score
      ```json
      {"event":"result","score":123}
      ```

- Raspberry Pi – Face (display)
  - Command topic: `robot/pi/face/cmd`
  - Payload:
    - `action`: "face"
    - `value`: expression name or file name (e.g., `happy`, `angry`, `neutral`)
    - `duration` (seconds, float, optional): how long to display; 0 = until next command
    - `fade` (seconds, float, optional): fade in/out duration
  - Example:
    ```json
    {"action":"face","value":"happy","duration":3,"fade":0.5}
    ```
  - State topic (emitted by Pi daemon): `robot/pi/face/state`

- Raspberry Pi – Servo (flower)
  - Command topic: `robot/pi/servo/cmd`
  - Payload:
    - `action`: "servo"
    - `value`: `open` | `close` | `center` | `stop` | specific angle (0–180 as string/number)
    - `speed` (1–100, optional): speed percentage
  - Examples:
    ```json
    {"action":"servo","value":"open","speed":40}
    {"action":"servo","value":"close","speed":30}
    {"action":"servo","value":"center","speed":60}
    {"action":"servo","value":"stop"}
    ```
  - State topic: `robot/pi/servo/state`

- Raspberry Pi – LED (NeoPixel)
  - Command topic: `robot/pi/led/cmd`
  - Payload:
    - `action`: "led"
    - `value`: `on` | `off`
    - `color`: `#RRGGBB` or `[r,g,b]`
    - `brightness`: 0.0–1.0
    - `effect`: `solid` | `blink` | `breathe`
    - Effect params (optional): `speed_hz` (blink), `period` (breathe)
  - Examples:
    ```json
    {"action":"led","value":"on","color":"#00BFFF","brightness":0.8,"effect":"breathe","period":2.5}
    {"action":"led","value":"off"}
    ```
  - State topic: `robot/pi/led/state`

Idempotency & cooldown:
- The Agent side already suppresses duplicate intent publishes within a short window.
- Games should avoid spamming the same command rapidly; if needed, implement a per-key cooldown (e.g., 1–2s for LEDs/face, longer for launch/exit).

---

### 5) Code Examples

#### 5.1 Unity / C# – TTS via HTTP (UnityWebRequest)

```csharp
using UnityEngine;
using UnityEngine.Networking;
using System.Text;
using System.Threading.Tasks;

public static class RobotTtsClient {
  // baseUrl example: "http://10.0.0.1:8000"
  public static async Task SayAsync(string baseUrl, string text, string voice = "zh_CN", float speed = 1f, float volume = 1f, bool play = true) {
    var url = $"{baseUrl.TrimEnd('/')}/tts";
    var payload = $"{{\"text\":\"{Escape(text)}\",\"voice\":\"{voice}\",\"speed\":{speed},\"volume\":{volume},\"play\":{play.ToString().ToLower()} }}";
    using var req = new UnityWebRequest(url, UnityWebRequest.kHttpVerbPOST);
    req.uploadHandler = new UploadHandlerRaw(Encoding.UTF8.GetBytes(payload));
    req.downloadHandler = new DownloadHandlerBuffer();
    req.SetRequestHeader("Content-Type", "application/json");
    var op = req.SendWebRequest();
    while (!op.isDone) await Task.Yield();
    if (req.result != UnityWebRequest.Result.Success) {
      Debug.LogError($"TTS failed: {req.responseCode} {req.error}");
    }
  }

  static string Escape(string s) => s?.Replace("\\", "\\\\").Replace("\"", "\\\"") ?? string.Empty;
}
```

#### 5.2 Unity / C# – MQTT (MQTTnet)

```csharp
using MQTTnet;
using MQTTnet.Client;
using MQTTnet.Protocol;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;

public static class PiMqttClient {
  static IMqttClient _client;
  static readonly object _gate = new object();

  public static async Task ConnectAsync(string host = "10.0.0.1", int port = 1883) {
    if (_client != null && _client.IsConnected) return;
    lock (_gate) {
      _client ??= new MqttFactory().CreateMqttClient();
    }
    var opts = new MqttClientOptionsBuilder().WithTcpServer(host, port).Build();
    await _client.ConnectAsync(opts);
  }

  public static async Task PublishAsync(string topic, object payload, MqttQualityOfServiceLevel qos = MqttQualityOfServiceLevel.AtLeastOnce, bool retain = false) {
    var json = JsonSerializer.Serialize(payload);
    var appMsg = new MqttApplicationMessageBuilder()
      .WithTopic(topic)
      .WithPayload(Encoding.UTF8.GetBytes(json))
      .WithQualityOfServiceLevel(qos)
      .WithRetainFlag(retain)
      .Build();
    await _client.PublishAsync(appMsg);
  }

  // Convenience helpers
  public static Task FaceHappyAsync() => PublishAsync("robot/pi/face/cmd", new { action = "face", value = "happy", duration = 3, fade = 0.5 });
  public static Task FlowerOpenAsync() => PublishAsync("robot/pi/servo/cmd", new { action = "servo", value = "open", speed = 40 });
  public static Task LedBreatheAsync() => PublishAsync("robot/pi/led/cmd", new { action = "led", value = "on", color = "#00BFFF", brightness = 0.8, effect = "breathe", period = 2.5 });
}
```

#### 5.2.1 Unity / C# – Detailed presets with RobotBridge.cs

If you prefer a single-file helper, use `Assets/Scripts/RobotBridge.cs` included in this repo. It works out-of-the-box with HTTP (TTS) and uses `mosquitto_pub` for MQTT fallback. Example usage:

```csharp
// TTS
await RobotBridge.Instance.SayAsync("Hello from game!", voice: "zh_CN", speed: 1.0f, volume: 1.0f, play: true);

// Face (5 presets)
await RobotBridge.Instance.FaceHappyAsync(duration: 3f, fade: 0.3f);
await RobotBridge.Instance.FaceAngryAsync(duration: 2.5f, fade: 0.2f);
await RobotBridge.Instance.FaceSadAsync(duration: 2.5f, fade: 0.2f);
await RobotBridge.Instance.FaceSurprisedAsync(duration: 1.8f, fade: 0.15f);
await RobotBridge.Instance.FaceNeutralAsync(fade: 0.2f);

// LED (5 presets)
await RobotBridge.Instance.LedOffAsync();
await RobotBridge.Instance.LedSolidWarmAsync(brightness: 0.8f);             // warm gold/orange solid
await RobotBridge.Instance.LedBreatheCyanAsync(brightness: 0.8f, period: 2.5f);
await RobotBridge.Instance.LedBlinkWhiteAsync(brightness: 1.0f, speedHz: 2f);
await RobotBridge.Instance.LedSolidColorAsync("#FF00FF", brightness: 0.7f); // any solid color

// Flower (servo) sequences
await RobotBridge.Instance.FlowerOpenThenStopAsync(speed: 40, openHoldMs: 500);     // open then stop
await RobotBridge.Instance.FlowerOpenThenCloseAsync(speedOpen: 40, openHoldMs: 1500, speedClose: 40);
await RobotBridge.Instance.FlowerPulseAsync(cycles: 3, speed: 40, openMs: 800, closeMs: 800);
await RobotBridge.Instance.FlowerCenterHoldAsync(speed: 60);
await RobotBridge.Instance.FlowerStopAsync();
```

#### 5.3 Python – Requests + Paho MQTT

```python
import requests, json
import paho.mqtt.publish as publish

HOST = "10.0.0.1"

# TTS
requests.post(f"http://{HOST}:8000/tts", json={
    "text": "Hello from Python!",
    "voice": "zh_CN",
    "play": True,
})

# Pi Face
publish.single("robot/pi/face/cmd", json.dumps({
    "action": "face",
    "value": "happy",
    "duration": 3,
    "fade": 0.5
}), hostname=HOST, qos=1, retain=False)

# Servo Open
publish.single("robot/pi/servo/cmd", json.dumps({
    "action": "servo",
    "value": "open",
    "speed": 40
}), hostname=HOST, qos=1, retain=False)
```

#### 5.4 Node.js – fetch + mqtt

```javascript
// TTS
await fetch("http://10.0.0.1:8000/tts", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({ text: "Hello from Node!", voice: "zh_CN", play: true }),
});

// MQTT
import mqtt from "mqtt";
const client = mqtt.connect("mqtt://10.0.0.1:1883");
client.on("connect", () => {
  client.publish("robot/pi/led/cmd", JSON.stringify({
    action: "led",
    value: "on",
    color: "#00BFFF",
    brightness: 0.8,
    effect: "breathe",
    period: 2.5,
  }), { qos: 1, retain: false });
});
```

---

### 6) Orchestrator Intents (for game launching)

The Agent typically publishes launch/exit intents and an external orchestrator reads them and starts/stops games according to `config/manifest.json`.

- Topic: `robot/intent`

- Launch example:

```json
{"type":"LAUNCH_GAME","game_name":"cornhole","source":"unity_vosk"}
```

- Exit example:

```json
{"type":"EXIT_GAME","game_name":"cornhole","source":"game_ui"}
```

Best practices:
- Ensure the manifest entry (id, exec, workdir, args) matches the `game_name` you publish.
- The Agent implements duplicate suppression/cooldown; games should still avoid flooding intents.

---

### 7) Troubleshooting & Tips

- TTS 404/500:
  - Verify service is running (`uvicorn` logs show `Running on http://0.0.0.0:8000`).
  - Check firewall and base URL.

- MQTT no delivery:
  - Confirm Broker is reachable from your game PC: `telnet <HOST> 1883` or try an MQTT GUI client (MQTT Explorer).
  - Check QoS/retain flags; ensure payload is valid JSON (UTF-8).

- Pi not reacting:
  - Verify the three Pi daemons are running (face/servo/led) and subscribed to topics.
  - Check their state topics: `robot/pi/<face|servo|led>/state` for errors.

- Launching a game fails:
  - Orchestrator logs will show details (FileNotFound/permissions/workdir).
  - Validate `exec` path and `workdir` exist; avoid mapped drives for services.

Security notes:
- Keep Broker within a trusted LAN; if exposing externally, require authentication/TLS.
- Sanitize/validate incoming JSON on the Pi side if allowing third-party publishers.

---

### 8) What to ship to your teammate

- This document (`docs/INTEGRATION.md`).
- A Postman/Insomnia collection (optional) with:
  - `POST /tts`
  - MQTT examples for face/servo/led
- Optionally, a small single-file SDK per language that wraps the examples above.

With the HTTP TTS endpoint and the MQTT topics above, your teammate can integrate in minutes without touching Unity internals.


