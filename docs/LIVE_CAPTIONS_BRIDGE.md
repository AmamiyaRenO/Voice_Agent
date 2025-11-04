# Live Captions Bridge for Unity Voice Agent

This guide documents a complete workflow for harvesting Windows 11 Live Captions output and
forwarding finalized sentences to the Unity voice agent experience. The approach mirrors the
reference implementation that accompanies this repository and is ready to compile and run on a
Windows workstation.

## 1. Overview

| Component | Description |
| --- | --- |
| **LiveCaptionsListener.exe** | Headless UI Automation listener that watches the Live Captions text control. |
| **Sentence assembly** | Coalesces partial caption updates into full utterances using newline and silence detection. |
| **Unity transport** | Publish each completed sentence to Unity by logging, piping, or forwarding over MQTT/IPC. |

The workflow requires **Windows 11 22H2+** with the Live Captions feature enabled (press
`Win + Ctrl + L`). Enable the "Include microphone audio" option and keep the Live Captions window
floating above Unity. Unity should run in `MaximizedWindow` mode rather than exclusive fullscreen so
that Desktop Window Manager can expose the caption surface to UI Automation.

## 2. Building the listener

Compile `scripts/live_captions/LiveCaptionsListener.cs` as a .NET 6 console application (or .NET
Framework 4.8 if preferred). The entry point waits for the Live Captions window, subscribes to
`TextPattern.TextChangedEvent`, buffers incremental text, and emits finalized sentences whenever a
newline is observed or 1.2 seconds of silence pass without changes.

```bash
# Example build command (requires .NET SDK)
dotnet new console -n LiveCaptionsBridge --framework net6.0
# Replace the generated Program.cs with scripts/live_captions/LiveCaptionsListener.cs
# Then build:
dotnet publish -c Release -r win-x64 --self-contained false
```

When running, the console writes each completed sentence in the format `[Sentence] hello world`. The
`FinalizeSentence` method is the appropriate place to forward utterances to Unity through MQTT,
Named Pipes, stdin/stdout relays, or any other integration point that suits your deployment.

## 3. Unity side ingestion

A minimal Unity receiver can read lines from a pipe, MQTT subscription, or other channel and update
UI state or trigger voice actions. A reference snippet is provided below:

```csharp
using UnityEngine;
using System.IO;
using System.Threading;

public class LiveCaptionReceiver : MonoBehaviour
{
    void Start()
    {
        Thread thread = new Thread(ReadLoop) { IsBackground = true };
        thread.Start();
    }

    private void ReadLoop()
    {
        using StreamReader reader = new StreamReader("LiveCaptionsPipe.txt");
        while (true)
        {
            string line = reader.ReadLine();
            if (!string.IsNullOrEmpty(line))
            {
                Debug.Log($"[Subtitle] {line}");
            }
        }
    }
}
```

Swap the file reader with your chosen transport. For MQTT, reuse the existing broker connection; for
Named Pipes, create a companion Windows service that forwards the listener output.

## 4. Automating Live Captions startup

Include `scripts/live_captions/StartLiveCaptions.bat` in the Windows Startup folder (`shell:startup`)
so that the Live Captions application launches automatically at login. Alternatively, have the
listener trigger this script when it cannot locate the caption window.

## 5. Operational tips

- Ensure "Always on top" is checked in Live Captions so the automation tree stays stable.
- If the Live Captions window is recreated (e.g., language change), restart the listener to
  reattach.
- Running the listener alongside Unity in windowed fullscreen preserves UI Automation access while
  keeping an immersive experience for the player.

With these components in place, the Voice Agent project can consume the same subtitles that Live
Captions generates for system-wide speech, making it possible to drive gameplay or UI from Windows
speech recognition without custom ASR integrations.
