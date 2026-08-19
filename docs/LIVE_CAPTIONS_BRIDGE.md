# Live Captions Bridge for Unity Voice Agent

This guide documents the Windows 11 Live Captions integration used by Voice Agent. The listener is
a separate deployment artifact and is **not built or included in this repository**. A working
station needs a supplied `EnableLcMic.exe` (older builds may be named
`LiveCaptionsListener.exe`), or it should use another recognition mode such as Gemini Live.

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

## 2. Supplying the listener

Obtain the listener executable from the team that maintains the separate Live Captions Listener
project. Do not look for `scripts/live_captions/LiveCaptionsListener.cs` in this repository; that
source file is not checked in here.

Set `LIVE_CAPTIONS_LISTENER_EXE` to the executable's absolute path through Rachel Settings or the
`env` object in `scripts/local_services.user.json`, for example:

```json
{
  "env": {
    "LIVE_CAPTIONS_LISTENER_EXE": "D:\\unityproject\\LiveCaptionsListener\\temp_build\\win-x64-single\\EnableLcMic.exe"
  }
}
```

Restart `helper.bat` after changing the path. If the executable is missing, the launcher reports
`Live Captions listener not found`; use Gemini Live or install the external listener before
continuing.

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

The Voice Agent launcher starts the configured listener when Live Captions recognition is selected.
There is no `scripts/live_captions/StartLiveCaptions.bat` in this repository. Windows Live Captions
itself can be opened with `Win + Ctrl + L`.

## 5. Operational tips

- Ensure "Always on top" is checked in Live Captions so the automation tree stays stable.
- If the Live Captions window is recreated (e.g., language change), restart the listener to
  reattach.
- Running the listener alongside Unity in windowed fullscreen preserves UI Automation access while
  keeping an immersive experience for the player.

With these components in place, the Voice Agent project can consume the same subtitles that Live
Captions generates for system-wide speech, making it possible to drive gameplay or UI from Windows
speech recognition without custom ASR integrations.
