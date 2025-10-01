# Vosk Unity Package

Offline speech recognition using the [Vosk](https://github.com/alphacep/vosk-api) library.

## Requirements

- **Unity** `2020.3.48f1` or newer
- Microphone access on the target platform

The project contains third‑party libraries inside the `Assets/ThirdParty` folder:

- **Ionic.Zip** – used to decompress model archives
- **SimpleJSON** for JSON parsing
- **Vosk** native libraries for Windows, macOS and Android

MQTT intent publishing (`ROBOTVOICE_USE_MQTT`) now ships with a first‑party client implementation, so
no external DLLs are required for connecting to a broker, publishing intents with QoS 0/1, keeping
the session alive or performing simple TLS handshakes. Simply enable the scripting define and
configure the broker/TLS fields in `MqttIntentPublisher` to start sending intents.

If your project requires more advanced MQTT features (subscriptions, managed clients, WebSockets,
etc.) you can still drop the official [MQTTnet](https://github.com/dotnet/MQTTnet) assemblies into
`Assets/ThirdParty/Plugins/MQTTnet/` and update the publisher to use them instead of the built‑in
client.

No additional packages are required beyond the dependencies included in this repository unless you
choose to replace the bundled MQTT client with an alternative implementation.

## Importing

Clone this repository or download it as a ZIP and open it with Unity. You can also copy the `Assets` folder into an existing project if you wish to integrate the scripts and plugins manually.

After opening the project Unity will import the native plugins for your platform automatically.

## ModelPath

`VoskSpeechToText` expects a model archive inside the **StreamingAssets** folder. The `ModelPath` field contains the relative path (e.g. `vosk-model-small-en-us-0.15.zip`). On the first run the archive is extracted to `Application.persistentDataPath`.

You can assign a different model by changing `ModelPath` in the inspector or through script before calling `StartVoskStt`.

## Usage

1. Add the **VoskSpeechToText** component to a GameObject in your scene.
2. Place a Vosk model archive into `Assets/StreamingAssets/` and assign its filename to `ModelPath`.
3. A `VoiceProcessor` component is required for microphone input. If one isn't present on the same GameObject, `VoskSpeechToText` adds it automatically.
4. Call `StartVoskStt` (optionally with `startMicrophone: true`) to initialise the recogniser.
5. Subscribe to `OnTranscriptionResult` to receive the recognised text.

```csharp
using UnityEngine;

public class VoskExample : MonoBehaviour
{
    public VoskSpeechToText speech;

    void Start()
    {
        speech.ModelPath = "vosk-model-small-en-us-0.15.zip"; // relative to StreamingAssets
        speech.OnTranscriptionResult += result => Debug.Log(result);
        speech.StartVoskStt(startMicrophone: true);
    }
}
```

## Python Voice Service

The project now supports streaming microphone audio to an external Python
service that runs [Faster-Whisper](https://github.com/guillaumekln/faster-whisper).
Enable **Use Python Service** on the `VoskSpeechToText` component and set
`PythonServiceUrl` to the `transcribe` endpoint exposed by the service
(defaults to `http://127.0.0.1:8000/transcribe`). See
[`python_voice_service/README.md`](python_voice_service/README.md) for
setup instructions, including pointing the service at the downloaded
model directory shown in the screenshots.

### One-command local stack

If you frequently start the messaging hub, the orchestrator that
bootstraps Mosquitto, and the Python voice service, run them together
with:

```bash
python scripts/start_local_services.py --hub-cmd "<command to start your hub>"
```

Pass `--orchestrator-cmd "<command to start your orchestrator>"` (or set
`VOICE_AGENT_ORCH_CMD`) to include the orchestrator alongside the hub
and voice service. The helper keeps the processes alive, forwards
`Ctrl+C` to them and automatically stops the remaining services if one
of them exits. You can
customise the launch behaviour with environment variables instead of
command-line flags:

* `VOICE_AGENT_HUB_CMD` – command that starts the messaging hub.
* `VOICE_AGENT_HUB_CWD` – working directory for the hub command (defaults
  to the repository root).
* `VOICE_AGENT_VOICE_CMD` – command for the Python voice service (defaults
  to `uvicorn main:app --host 0.0.0.0 --port 8000`).
* `VOICE_AGENT_VOICE_CWD` – working directory for the voice service
  command (defaults to `python_voice_service/`).
* `VOICE_AGENT_ORCH_CMD` – command for the orchestrator that launches
  Mosquitto (optional).
* `VOICE_AGENT_ORCH_CWD` – working directory for the orchestrator
  command (defaults to the current directory).

Pass `--env-file path/to/.env` if you want to preload environment
variables (such as model locations) before the services start.

## Hello World Example

The following example shows how to detect the word `"hello"` and print `"hello world"` to the console.

```csharp
using UnityEngine;

public class HelloWorldExample : MonoBehaviour
{
    public VoskSpeechToText speech;

    void Start()
    {
        speech.OnTranscriptionResult += OnResult;
        speech.StartVoskStt(startMicrophone: true);
    }

    void OnResult(string json)
    {
        var result = new RecognitionResult(json);
        foreach (var phrase in result.Phrases)
        {
            if (phrase.Text.ToLower().Contains("hello"))
            {
                Debug.Log("hello world");
                break;
            }
        }
    }
}
```

Attach this component alongside `VoskSpeechToText`. When you say `"hello"` the console will output `"hello world"`.

For more information on models and Vosk itself see the [Vosk documentation](https://github.com/alphacep/vosk-api).

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
