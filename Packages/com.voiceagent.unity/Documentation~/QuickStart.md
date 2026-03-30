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
