# VoiceAgent Unity SDK

This package is intentionally minimal.

It gives you:

- `VoiceAgentClient`: a plain C# client for the external `voice-agent` HTTP runtime
- `VoiceAgentComponent`: a `MonoBehaviour` you can drop on any GameObject
- `VoiceAgentComponentEditor`: inspector buttons for calling the SDK directly from the right panel

## Recommended usage

1. Import the package.
2. Optionally import the `Inspector Starter` sample from Package Manager if you want a prefab with the component already attached and prefilled.
3. Otherwise create an empty GameObject and add `VoiceAgentComponent`.
4. Fill in host, port, text, voice, backend, face, LED, and other fields in the Inspector.
5. Click the buttons in the Inspector to call the runtime directly.

## Google Cloud TTS

Google Cloud TTS is opt-in. The runtime never falls back to a local voice when it is selected.

1. Enable the Google Cloud Text-to-Speech API for the API-key project.
2. Start the local services, select `Google Cloud TTS`, enter the API key in the Inspector, and click `Refresh Options`.
3. Choose a Google voice and click `Speak`.

The API key is serialized into the scene or prefab. Do not commit a scene or prefab containing a real key to a shared repository. Server-side `GOOGLE_APPLICATION_CREDENTIALS` remains available as an optional alternative.

## Runtime scripting

Developers can either:

- call `component.Client` directly from code
- or reuse the `VoiceAgentComponent` and its serialized fields in their own scenes

Example:

```csharp
using UnityEngine;
using VoiceAgent.Unity;

public sealed class DemoCaller : MonoBehaviour
{
    [SerializeField] private VoiceAgentComponent voiceAgent;

    public async void SayHello()
    {
        await voiceAgent.Client.SpeakAsync(new VoiceAgentSpeechRequest
        {
            text = "Hello from Unity.",
            backend = "piper",
        });
    }
}
```

## Inspector groups

The component inspector exposes direct buttons for:

- runtime and logs
- TTS and Kokoro
- ASR
- vision and game launch
- face
- LED
- flower

This package no longer includes flow graphs, validators, playtest windows, or sample UI systems.
