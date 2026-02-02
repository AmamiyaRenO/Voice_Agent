# webrtc_apm_unity (Windows native plugin)

This folder contains a small native wrapper around **WebRTC AudioProcessing (APM/AEC3)** to enable
real acoustic echo cancellation (AEC) in Unity by feeding:

- **render reference** audio (what Unity is playing to speakers), and
- **capture** audio (what the microphone is recording),

and receiving an **echo-cancelled capture** stream suitable for ASR.

## Output artifact

Build produces a DLL:

- `webrtc_apm_unity.dll`

Copy it into Unity at:

- `Assets/Plugins/x86_64/webrtc_apm_unity.dll`

## Dependencies

You need a build of WebRTC AudioProcessing providing:

- headers such as `modules/audio_processing/include/audio_processing.h`
- a linkable library commonly named `webrtc_audio_processing`

### Option A (recommended): MSYS2 package (if available on your setup)

Install MSYS2 and then (UCRT64 shell):

```bash
pacman -S --needed mingw-w64-ucrt-x86_64-toolchain mingw-w64-ucrt-x86_64-pkgconf
pacman -S --needed mingw-w64-ucrt-x86_64-webrtc-audio-processing
```

### Option B: build `webrtc-audio-processing` from source

Canonical upstream is published via freedesktop/pulseaudio. A Windows-friendly mirror is:

- `https://github.com/cross-platform/webrtc-audio-processing`

Build it (typically via meson/ninja) and ensure pkg-config can find it.

## Build (CMake)

From this directory:

```bash
cmake -S . -B build -G "Ninja" -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
```

If pkg-config cannot find `webrtc-audio-processing`, set:

- `WEBRTC_APM_INCLUDE_DIR` (directory containing `modules/`)
- `WEBRTC_APM_LIBRARY` (full path to the library file)

Example:

```bash
cmake -S . -B build -G "Ninja" -DCMAKE_BUILD_TYPE=Release ^
  -DWEBRTC_APM_INCLUDE_DIR="C:/path/to/webrtc-audio-processing/include" ^
  -DWEBRTC_APM_LIBRARY="C:/path/to/webrtc-audio-processing/lib/webrtc_audio_processing.lib"
```

## C API exported

See `src/webrtc_apm_unity.h` for the exported functions used by Unity via `DllImport`.

## AEC configuration (important)

This wrapper enables several options commonly recommended in WebRTC APM documentation:

- **EchoCancellation** enabled with high suppression
- **NoiseSuppression** enabled (high)
- **Drift compensation** enabled
- **ExtendedFilter** enabled (more robust to imperfect delay)
- **DelayAgnostic** enabled (internal delay estimation)

If you change these options, rebuild `webrtc_apm_unity.dll` and re-copy it to `Assets/Plugins/x86_64/`.

