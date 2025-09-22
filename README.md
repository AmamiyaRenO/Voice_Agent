# Vosk Unity Package

Offline speech recognition using the [Vosk](https://github.com/alphacep/vosk-api) library.

## Requirements

- **Unity** `2020.3.48f1` or newer
- Microphone access on the target platform

The project contains third‑party libraries inside the `Assets/ThirdParty` folder:

- **Ionic.Zip** – used to decompress model archives
- **SimpleJSON** for JSON parsing
- **Vosk** native libraries for Windows, macOS and Android

No additional packages are required beyond the dependencies included in this repository.

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
