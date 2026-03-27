# Voice Agent Repository Architecture

## 1. Introduction

Voice Agent is a repository for a local-first voice interaction stack aimed at rehabilitation, exercise-game, and socially assistive interaction scenarios. The codebase combines a desktop runtime, Python speech and dialog services, an optional Unity shell, a Python SDK, orchestration scripts, packaging assets, and regression tooling into a single system that can be run either from source or from packaged executables.

This document adopts an academic-style section order inspired by the provided PDF, but its role is engineering documentation rather than research reporting. It describes the repository as a technical system, explains how the major modules fit together, and records the operational assumptions that govern current development. It intentionally avoids inventing user-study results, benchmark claims, or formal evaluations that are not directly supported by the repository.

At a high level, the maintained path in this repository is the standalone desktop runtime on `127.0.0.1:8787` combined with Python services for ASR, grounded dialog, memory, TTS, and game launching. Unity remains part of the repository, but in the present architecture it is treated as an optional shell for avatar, UI, gameplay, and camera integration rather than the primary conversation orchestrator.

## 2. Related System Context

The repository sits at the intersection of several adjacent system patterns:

- desktop operator consoles for runtime control and observability
- voice-service pipelines for speech recognition, language-model response generation, and text-to-speech
- game intent and robot-control integrations over MQTT
- optional real-time presentation shells, here implemented in Unity
- SDK-based automation layers for non-Unity clients

The project does not behave as a single monolithic application. Instead, it is a coordinated multi-process system in which different responsibilities are separated by interface boundaries. The desktop runtime provides the main operator surface. The Python voice service provides the maintained conversation entry point. Supporting services in `scripts/` contribute intent routing, dialog memory, telemetry, and packaging workflows. The SDK and Unity layers consume or extend these runtime capabilities from different integration paths.

This separation is reflected in the default port map:

| Component | Default Port | Primary Role |
| --- | ---: | --- |
| Desktop runtime and browser panel | `8787` | operator surface, runtime control, memory and game tools, camera tools, SDK visualizer |
| Python voice service | `8000` | conversation, ASR, response generation, runtime conversation config |
| Piper wrapper | `5005` | low-latency local TTS |
| Kokoro TTS wrapper | `5007` | neural TTS backend |
| Telemetry service | `8101` | exercise and usage metrics |
| MQTT broker | `1883` | robot, intent, dialog, and telemetry messaging |

The architecture therefore depends less on a single executable boundary and more on stable internal interfaces between services, assets, and control surfaces.

## 3. The Voice Agent Repository

### 3.1 Major Subsystems

The repository consists of several major subsystems that together implement the complete runtime.

#### Desktop Runtime and Browser Panel

The desktop runtime is implemented primarily in `python_voice_service/desktop_runtime.py`. It serves the browser panel, owns operator-facing routes on port `8787`, and mediates access to audio control, runtime configuration, logs, memory, game configuration, camera access, and SDK visualization. It is the maintained operator path for the project.

The browser panel assets are resolved from two locations:

- default user panel path: `Assets/StreamingAssets/panel/*`
- additional runtime pages: `runtime/panel/*`

The desktop runtime now serves the legacy user panel from `Assets/StreamingAssets/panel/panel.html` at `/`, `/index.html`, and `/panel.html`, while still using `runtime/panel/*` for pages such as `/runtime`.

#### Python Voice Service

The maintained speech and dialog entry point is `python_voice_service/main.py`, which instantiates the FastAPI application produced by `api_routes.py`. This layer handles conversation requests, ASR configuration, response generation, structured reply handling, and the runtime-facing conversation configuration surface.

The directory also contains the main speech-related support modules, including:

- `conversation_runtime.py`
- `desktop_audio_agent.py`
- `streaming_asr.py`
- `transcription_service.py`
- `reply_generation.py`
- `speaker_id.py`
- `game_grounding.py`
- `local_docs_rag.py`
- the Piper and Kokoro HTTP wrappers

Together, these modules implement the maintained speech stack from recognition through reply generation and spoken output.

#### Unity Optional Shell

The Unity project is still present under `Assets/`, `Packages/`, and `ProjectSettings/`. It includes scenes, scripts, browser panel assets, and optional shell integration. In the current repository direction, Unity is used to host avatar, subtitles, gameplay, and camera-linked experiences while delegating core conversation orchestration to the desktop runtime and Python services.

This is an architectural distinction rather than a repository accident. The codebase still supports Unity workflows, but the README and runtime layout make clear that Unity is secondary to the desktop runtime for end-to-end speech testing.

#### Python SDK

The Python SDK under `python_sdk/` mirrors the desktop runtime API surface so external clients can control speech, face, flower, LED, game intents, ASR state, and runtime prompt/model behavior without editing Unity scenes. In practice, the SDK functions as a stable automation and integration layer over the same runtime HTTP APIs surfaced by the control panel and SDK visualizer.

#### Scripts and Launcher Services

The `scripts/` directory provides orchestration and service support for the rest of the repository. Relevant subdirectories include:

- `intent_service`
- `dialog_service`
- `game_launcher`
- `telemetry_service`
- `mqtt`
- `packaging`

These support modules are critical to the runtime even though they are not the main browser-facing or speech-facing entry points. They handle intent resolution, structured memory persistence, telemetry collection, launch behavior, and deployment packaging workflows.

#### Runtime Assets, Installer, Firmware, and Tests

The `runtime/` directory stores runtime assets and outputs such as panel files, QMD and doc-grounding data, live captions, models, and evaluation artifacts under `runtime/evals/`. The `installer/` directory stores Windows installer entrypoints and related scripts. `Firmware/` stores hardware-side assets for robot or peripheral integration. `tests/` stores smoke tests, SDK tests, regression helpers, and transcript-oriented validation fixtures.

### 3.2 Repository Layout

The top-level repository structure can be summarized as follows:

| Path | Responsibility |
| --- | --- |
| `Assets/` | Unity scenes, scripts, prefabs, browser assets, and optional shell integration |
| `python_voice_service/` | FastAPI services for conversation, ASR, desktop runtime, TTS wrappers, grounding, speaker identity, and streaming audio |
| `python_sdk/` | Python client SDK for runtime and robot control |
| `scripts/` | launcher logic and supporting services such as intent, dialog memory, telemetry, and packaging |
| `runtime/` | panel assets, QMD docs, captions, models, packaged payloads, and evaluation outputs |
| `docs/` | deployment, integration, SDK, benchmark, and architecture documentation |
| `installer/` | Windows deployment scripts and launchers |
| `Firmware/` | hardware-facing assets |
| `tests/` | smoke, regression, and SDK validation |
| `native/` | native audio processing helpers |

This layout shows that the repository is best understood as a coordinated platform rather than a narrow single-service application.

### 3.3 The Agent Page as a Repository Subsystem

The browser homepage is not the repository itself, but it is still an important subsystem within the wider architecture. The homepage implementation in `Assets/StreamingAssets/panel/panel.html` is served at `/`, `/index.html`, and `/panel.html`, and it provides a top-level entry point for quick listening checks, transcript review, and navigation to supporting pages such as `/runtime`, `/memory`, `/games`, `/setup`, `/sdk`, and `/telemetry`.

Within the repository-wide architecture, the agent page should be understood as the operator-facing surface of the desktop runtime rather than as an isolated product. Its role is to make the rest of the repository observable and operable without collapsing all system complexity into a single UI page.

## 4. Repository Architecture and Core Mechanisms

### 4.1 Top-Level Runtime Flow

The maintained conversation path can be described as a pipeline with several coordinated stages.

1. User audio or text enters through the desktop runtime or a directly connected client.
2. The desktop runtime forwards conversation work to the Python voice service.
3. The voice service performs recognition, routing, grounding, and response generation.
4. Structured memory and recent contextual state are consulted when required.
5. The resulting reply is returned for spoken output, command publication, or panel display.
6. TTS playback, MQTT actions, and optional Unity shell behavior are triggered as appropriate.

The key runtime relationship is therefore not Unity to model, but desktop runtime to voice service, with supporting services supplying intent, memory, telemetry, and external control paths.

### 4.2 Core Execution Roles

Two entry modules define the main execution surfaces.

#### `python_voice_service/main.py`

`main.py` acts as the executable wrapper for the maintained voice service. It imports the FastAPI application factory from `api_routes.py`, constructs `app = create_app()`, and serves it through Uvicorn on port `8000`. This is the canonical service-level entry point for conversation, ASR, and response behavior.

#### `python_voice_service/desktop_runtime.py`

`desktop_runtime.py` defines the desktop runtime that serves the operator panel and related APIs on port `8787`. It owns the browser routes, the runtime configuration endpoints, the ASR and log inspection surface, and local operator tools such as memory, game manifest, camera, and SDK-related pages.

This division is deliberate. The voice service owns maintained speech and dialog behavior. The desktop runtime owns local operator workflows and direct desktop integration.

### 4.3 State and Persistence

The repository uses a mixed state model consisting of in-process runtime objects plus persisted configuration and content files.

Important persisted locations include:

| Data | Default Location |
| --- | --- |
| runtime or launcher configuration | `scripts/local_services.user.json` |
| game manifest | `scripts/intent_service/manifest.json` |
| structured user memory | `scripts/dialog_service/user_memory.json` |
| grounded document and QMD root | `runtime/qmd` |
| evaluation outputs | `runtime/evals` |

In packaged mode, some state is redirected to application-specific directories, but the same core runtime logic remains responsible for resolving paths and applying config.

### 4.4 Public Interfaces

The repository exposes multiple interface layers. The following interface groups are the most important public surfaces already visible in code and README.

#### HTTP Interfaces

| Service | Key Endpoints |
| --- | --- |
| Desktop runtime on `:8787` | `/`, `/games`, `/runtime`, `/memory`, `/setup`, `/sdk`, `/api/speak`, `/api/llm/prompt`, `/api/game`, `/api/game/manifest`, `/api/memory`, `/api/qmd`, `/api/runtime/config`, `/api/runtime/prereq`, `/api/runtime/ollama`, `/api/asr`, `/api/logs/stream` |
| Voice service on `:8000` | `/healthz`, `/conversation/config`, `/conversation/turn/stream`, `/transcribe`, `/transcribe/config`, `/respond`, `/respond/config`, `/respond/metrics` |
| Piper on `:5005` | `/speak`, `/speak_stream` |
| Kokoro on `:5007` | `/healthz`, `/speak` |

#### MQTT Interfaces

Core MQTT topics include:

- `robot/intent`
- `robot/pi/face/cmd`
- `robot/pi/servo/cmd`
- `robot/pi/led/cmd`
- `robot/dialog/query`
- `robot/dialog/answer`
- `robot/tts/options`
- `robot/voice/text`
- `voiceagent/telemetry/#`

These topics connect the repository to game launch behavior, robot-control behavior, dialog exchange, TTS option changes, and telemetry ingestion.

#### SDK Interface

The SDK wraps a subset of the desktop runtime and MQTT surface for non-Unity clients. It should therefore be viewed as an interface adapter, not as an independent service with separate system semantics.

### 4.5 Browser Panel as a Subsystem

The browser panel is served as static HTML rather than as a client-side SPA. Frontend state is managed through plain JavaScript, `fetch`, timers, and `EventSource`. Server-side state is maintained by in-process runtime objects such as `audio_agent`, `log_store`, and `camera_service`. This design keeps the operator surface small and debuggable while allowing the broader repository to evolve through service and runtime layers underneath it.

## 5. Operational Workflow and Validation

This section defines the practical workflows by which the repository is operated and validated. It is intentionally framed as operational validation rather than experimental evaluation.

### 5.1 Standalone Desktop Runtime Path

The primary workflow is:

1. configure local settings if needed
2. start the stack through `helper.bat` or the launcher path
3. open `http://127.0.0.1:8787`
4. use `/runtime`, `/memory`, `/games`, `/setup`, and `/sdk` as needed

This path is considered successful when:

- the desktop runtime and voice service are reachable
- local or cloud conversation mode can be selected and observed
- microphone input, transcript logging, and spoken output work end to end
- memory and game tools reflect persisted state correctly

### 5.2 Unity-Assisted Path

The Unity-assisted path starts the standalone services first and then opens the Unity project as a shell around avatar, gameplay, subtitles, or camera-linked workflows.

This path is considered successful when:

- standalone services remain the source of truth for conversation behavior
- Unity can consume the runtime and MQTT integrations without replacing the maintained speech path
- expected Unity-side shell behaviors remain synchronized with runtime events

### 5.3 Python SDK Path

The SDK path supports automation and non-Unity integration. A client imports `voice_agent_sdk`, connects to the desktop runtime, and invokes control methods for speech, prompt updates, runtime model changes, ASR mode changes, vision, face, LED, flower, and game intents.

This path is considered successful when:

- the SDK can reach the documented HTTP endpoints and MQTT topics
- payload behavior matches runtime expectations
- the SDK Visualizer and Python SDK remain consistent in supported operations

### 5.4 Regression and Verification

The repository includes multiple forms of verification:

- SDK tests in `tests/test_voice_agent_sdk.py`
- smoke tests for service and panel behavior
- live panel smoke execution against `:8787`
- conversation regression through `scripts/conversation_eval.py`
- memory-focused regression scenarios
- transcript-level regression artifacts under `runtime/evals/`

The intended validation discipline is not merely that services start, but that routed conversation behavior, memory behavior, and interface contracts continue to match expectations over time.

## 6. Discussion and Limitations

The current repository architecture embodies several tradeoffs.

First, the maintained path is the desktop runtime plus Python services, not the Unity shell. This increases clarity for speech-system development, but it requires contributors to think of Unity as an integration layer rather than as the core application.

Second, the repository is intentionally multi-process. This improves separation of concerns across runtime control, dialog, telemetry, TTS, and game support, but it also increases the operational burden of coordination, startup ordering, and partial failure handling.

Third, the browser panel is intentionally simple. Its static-HTML and plain-JavaScript design makes it easy to ship and debug, but it also means that richer UI abstractions, shared frontend state tooling, and component-level reuse are limited compared with a larger SPA architecture.

Fourth, the repository still contains both `runtime/panel/*` and `Assets/StreamingAssets/panel/*`, but they no longer play identical roles. The legacy Assets panel is the default user-facing homepage, while `runtime/panel/*` continues to host additional runtime pages. Contributors therefore need to be explicit about which surface they are changing.

Fifth, several features depend on external runtimes, local models, or environment setup, including Ollama, TTS backends, MQTT, and optional model assets. As a result, the full repository behavior is broader than what can be guaranteed from code structure alone.

Finally, the project has grown to encompass runtime control, conversation logic, memory, grounding, SDK integration, Unity shell behavior, packaging, and telemetry. The benefit is a cohesive platform; the cost is that repository-level architectural understanding is necessary to make safe changes in any single subsystem.

## 7. Conclusion

The Voice Agent repository is best understood as a layered local-first voice platform rather than as a single application. Its maintained center of gravity is the combination of the desktop runtime on `:8787` and the Python voice service on `:8000`, with supporting modules for memory, intent routing, telemetry, TTS, packaging, and optional Unity-based presentation.

The repository layout reflects this architecture directly. Top-level directories correspond to distinct operational roles, while the exposed HTTP and MQTT interfaces tie those roles together into a coherent runtime. The browser panel and agent page remain important, but only as one subsystem within a broader platform.

For contributors, the central architectural principle is straightforward: treat the desktop runtime and Python voice service as the primary execution path, treat Unity as an optional shell, and interpret the rest of the repository as supporting infrastructure that enables deployment, automation, validation, and long-term maintainability.
