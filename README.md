# Vosk Unity Package

Offline speech recognition using the [Vosk](https://github.com/alphacep/vosk-api) library with a pluggable speech-to-text engine pipeline.

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

`VoskSpeechToText` expects a model archive inside the **StreamingAssets** folder when the built-in Vosk engine is used. The `ModelPath` field contains the relative path (e.g. `vosk-model-small-en-us-0.15.zip`). On the first run the archive is extracted to `Application.persistentDataPath`.

You can assign a different model by changing `ModelPath` in the inspector or through script before calling `StartSpeechRecognition` (or the legacy `StartVoskStt`). Custom engines may choose to interpret the model path differently, so consult the engine's documentation.

## Usage

1. Add the **VoskSpeechToText** component to a GameObject in your scene.
2. (Optional) Add a component that inherits from `SpeechToTextEngineBase` to the same GameObject and assign it to the **Speech Engine** field. If left empty the default `VoskSpeechToTextEngine` is created automatically.
3. Provide the model assets required by the engine (for Vosk this means copying the archive into `Assets/StreamingAssets/` and assigning its filename to `ModelPath`) and adjust the `Sample Rate` field if your engine expects a different capture rate.
4. A `VoiceProcessor` component is required for microphone input. If one isn't present on the same GameObject, `VoskSpeechToText` adds it automatically.
5. Call `StartSpeechRecognition` (optionally with `startMicrophone: true`) to initialise the recogniser. The legacy `StartVoskStt` method remains available for existing integrations.
6. Subscribe to `OnTranscriptionResult` to receive the recognised text.

```csharp
using UnityEngine;

public class VoskExample : MonoBehaviour
{
    public VoskSpeechToText speech;

    void Start()
    {
        speech.ModelPath = "vosk-model-small-en-us-0.15.zip"; // relative to StreamingAssets
        speech.OnTranscriptionResult += result => Debug.Log(result);
        speech.StartSpeechRecognition(startMicrophone: true);
    }
}
```

## Creating custom speech engines

`VoskSpeechToText` delegates audio processing to components that inherit from `SpeechToTextEngineBase`. The built-in
`VoskSpeechToTextEngine` is provided for backwards compatibility, but you can plug in alternative providers (including online
services or other offline SDKs) by implementing the following contract:

1. Create a new `MonoBehaviour` that inherits from `SpeechToTextEngineBase` and override the `EngineName`,
   `InitialiseAsync` and `TryRecognise` members.
2. Use the supplied `SpeechToTextEngineConfiguration` to load your model assets. The configuration includes the resolved
   model path, requested key phrases, maximum alternatives and the microphone sample rate.
3. Add your component to the same GameObject as `VoskSpeechToText` and assign it to the **Speech Engine** field in the
   inspector.
4. Call `StartSpeechRecognition` as usual. The controller will invoke your engine instead of the default Vosk
   implementation.

This design makes it possible to experiment with Hugging Face, Azure, Google Cloud or any other service without modifying
the controller. Each engine controls its own initialisation and waveform processing pipeline while the controller handles
microphone management and result dispatching.

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
        speech.StartSpeechRecognition(startMicrophone: true);
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
