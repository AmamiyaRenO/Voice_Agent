using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Reflection;
using UnityEngine;
using UnityEngine.UI;
using Process = System.Diagnostics.Process;
using ProcessStartInfo = System.Diagnostics.ProcessStartInfo;
using ProcessWindowStyle = System.Diagnostics.ProcessWindowStyle;
using PiHub = global::PiMessageHub;

namespace RobotVoice
{
    public sealed class UserTestControlPanel : MonoBehaviour
    {
        [Header("Dependencies")]
        [SerializeField] private PiHub piHub;
        [SerializeField] private VoiceGameLauncher voiceLauncher;

        [Header("Voice Service")]
        [SerializeField, Tooltip("HTTP endpoint for the Piper HTTP /speak route")]
        private string voiceServiceUrl = "http://127.0.0.1:5005/speak";
        [SerializeField, Tooltip("Default voice code passed to the TTS endpoint")]
        private string defaultVoiceCode = "en_US";
        [SerializeField, Tooltip("Additional voice codes shown in the dropdown")]
        private string[] availableVoices = new[] { "en_US" };
        [Header("Qwen TTS (Speakers)")]
        [SerializeField, Tooltip("Default Qwen speaker name used by the tester UI")]
        private string defaultQwenSpeaker = "Ryan";
        [SerializeField, Tooltip("Fixed Qwen speaking style used for all Qwen speak requests.")]
        private string fixedQwenInstruct = "friendly";
        [SerializeField, Tooltip("Speaker names shown in the Qwen dropdown (matches qwen-tts model card speaker ids)")]
        private string[] qwenSpeakers = new[]
        {
            "Ryan",
            "Aiden",
            "Vivian",
            "Serena",
            "Uncle_Fu",
            "Dylan",
            "Eric",
            "Ono_Anna",
            "Sohee",
        };
        [SerializeField, Tooltip("Default Piper/Coqui model identifier exposed in the tester UI")]
        private string defaultTtsModel = "piper-zh";
        [SerializeField, Tooltip("Additional model identifiers shown in the dropdown")]
        private string[] availableTtsModels = new[] { "piper-zh", "piper-en" };
		[SerializeField, Tooltip("Directory to scan for Piper .onnx models to populate the dropdown")]
		private string modelsDirectory = @"D:\piper\models";
		[SerializeField, Tooltip("Whether to recursively include subdirectories when scanning modelsDirectory")]
		private bool scanModelsRecursively = true;

        [Header("Server")]
        [SerializeField, Tooltip("TCP port for the built-in HTTP control panel")]
        private int httpPort = 8787;
        [SerializeField, Tooltip("Automatically start the listener when the scene loads")]
        private bool autoStart = true;

        [Header("Camera Preview")]
        [SerializeField, Tooltip("Show local PC camera on the tester web panel (/camera.jpg).")]
        private bool enableCameraPreview = true;
        [SerializeField, Tooltip("Use an external texture (e.g., from MediaPipe) instead of opening WebCamTexture")]
        private bool useExternalCameraTexture = false;
        [SerializeField, Tooltip("When useExternalCameraTexture=true, read frames from this RawImage's texture (e.g., MediaPipe Annotatable Screen)")]
        private RawImage externalCameraRawImage;
        [SerializeField, Tooltip("Fallback: If RawImage is not set, read from this Renderer.material.mainTexture")]
        private Renderer externalCameraRenderer;
        [SerializeField, Tooltip("Requested camera width for preview")]
        private int cameraWidth = 640;
        [SerializeField, Tooltip("Requested camera height for preview")]
        private int cameraHeight = 480;
        [SerializeField, Tooltip("Preferred camera device name (partial match, case-insensitive). Example: OBS Virtual Camera")]
        private string preferredCameraDeviceName = "OBS Virtual Camera";
        [SerializeField, Tooltip("Force camera by device index. -1 means auto-select.")]
        private int preferredCameraDeviceIndex = -1;
        [SerializeField, Tooltip("JPEG quality (1-100) for /camera.jpg")]
        private int cameraJpegQuality = 60;
        [SerializeField, Tooltip("Target frames per second for snapshot encoding")]
        private int cameraFps = 30;
        [SerializeField, Tooltip("Keep Unity running when window is not focused so panel camera snapshots keep updating.")]
        private bool forceRunInBackground = true;
        [SerializeField, Tooltip("Reduce panel camera cost when using external texture so MediaPipe tracking remains smooth.")]
        private bool optimizeExternalPreviewForTracking = true;
        [SerializeField, Tooltip("Max FPS for external-texture preview when optimization is enabled.")]
        private int externalPreviewMaxFps = 1;
        [SerializeField, Tooltip("Max width for external-texture JPEG snapshots when optimization is enabled.")]
        private int externalPreviewMaxWidth = 256;
        [SerializeField, Tooltip("Only keep encoding frames for this many seconds after a panel camera request.")]
        private float cameraClientActiveWindowSeconds = 6f;

        [SerializeField, Tooltip("Python voice service base URL used to get/set runtime LLM system prompt.")]
        private string llmServiceBaseUrl = "http://127.0.0.1:8000";
        [SerializeField, Tooltip("Ollama base URL used by /api/vision/describe.")]
        private string ollamaBaseUrl = "http://127.0.0.1:11434";
        [SerializeField, Tooltip("Default multimodal model for camera description.")]
        private string defaultVisionModel = "gemma3:4b";
        [SerializeField, Tooltip("Default prompt used when /api/vision/describe request has empty prompt.")]
        private string defaultVisionPrompt = "Describe what you see in this camera frame in 2-4 concise sentences.";
        [SerializeField, Tooltip("Telemetry dashboard base URL served by telemetry_service.")]
        private string telemetryDashboardUrl = "http://127.0.0.1:8101/dashboard";

        private HttpListener listener;
        private CancellationTokenSource shutdownToken;
        private Task listenLoopTask;
        private string activeVoiceCode;
        private string activeTtsModel;
        private string activeQwenSpeaker;
        private static readonly HttpClient SharedHttpClient = new HttpClient();
        private const float DefaultFaceSeconds = 3f;
        private SynchronizationContext mainThreadContext;

        // --- Camera runtime ---
        private WebCamTexture _webcam;
        private Texture2D _cameraTexture;
        private byte[] _latestJpeg;
        private readonly object _cameraLock = new object();
        private float _nextCaptureRealtime;
        private int _cameraFrameCount;
        private float _cameraLastFrameTs = -1f;
        private float _externalTextureSearchTs = -10f;
        private int _lastExternalTextureWidth;
        private int _lastExternalTextureHeight;
        private string _lastExternalTextureType = string.Empty;
        private long _cameraLastFrameUtcTicks;
        private bool _hasExternalRawImageBinding;
        private bool _hasExternalRendererBinding;
        private bool _runInBackgroundEnabled;
        private long _lastCameraClientRequestUtcTicks;
        private bool _appIsVisible = true;

        private void Awake()
        {
            mainThreadContext = SynchronizationContext.Current;
            if (forceRunInBackground && !Application.runInBackground)
            {
                Application.runInBackground = true;
            }
            _runInBackgroundEnabled = Application.runInBackground;
            ApplyModelsDirectoryEnvironmentOverride();
            activeVoiceCode = DetermineInitialVoiceCode();
            activeTtsModel = DetermineInitialTtsModel();
            activeQwenSpeaker = DetermineInitialQwenSpeaker();
            _hasExternalRawImageBinding = externalCameraRawImage != null;
            _hasExternalRendererBinding = externalCameraRenderer != null;
            if (autoStart)
            {
                StartServer();
            }
            TryStartCamera();
        }

        private void ApplyModelsDirectoryEnvironmentOverride()
        {
            try
            {
                // 优先取环境变量覆盖：VOICE_MODELS_DIR 或 PIPER_MODELS_DIR
                var keys = new[] { "VOICE_MODELS_DIR", "PIPER_MODELS_DIR" };
                foreach (var key in keys)
                {
                    var value = System.Environment.GetEnvironmentVariable(key);
                    if (!string.IsNullOrWhiteSpace(value))
                    {
                        var expanded = System.Environment.ExpandEnvironmentVariables(value.Trim());
                        modelsDirectory = expanded;
                        break;
                    }
                }
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[UserTestPanel] modelsDirectory env override failed: {ex.Message}");
            }
        }

        private void OnDestroy()
        {
            StopServer();
            StopCamera();
        }

        private void OnApplicationFocus(bool hasFocus)
        {
            _appIsVisible = hasFocus;
        }

        private void OnApplicationPause(bool pauseStatus)
        {
            _appIsVisible = !pauseStatus;
        }

        public void StartServer()
        {
            if (listener != null)
            {
                return;
            }

            if (!HttpListener.IsSupported)
            {
                Debug.LogError("[UserTestPanel] HttpListener is not supported on this platform.");
                return;
            }

            var port = Mathf.Clamp(httpPort, 1024, 65535);
            var prefix = $"http://*:{port}/";
            try
            {
                listener = new HttpListener();
                listener.Prefixes.Add(prefix);
                listener.Start();
                shutdownToken = new CancellationTokenSource();
                listenLoopTask = Task.Run(() => AcceptLoopAsync(shutdownToken.Token));
                Debug.Log($"[UserTestPanel] Listening on {prefix}. Clients on the same Wi-Fi can visit http://<host-ip>:{port}/");
            }
            catch (Exception ex)
            {
                Debug.LogError($"[UserTestPanel] Failed to start listener on {prefix}: {ex.Message}");
                StopServer();
            }
        }

        public void StopServer()
        {
            try
            {
                shutdownToken?.Cancel();
            }
            catch (Exception)
            {
            }

            if (listener != null)
            {
                try
                {
                    listener.Close();
                }
                catch (Exception)
                {
                }
                listener = null;
            }

            if (listenLoopTask != null)
            {
                try
                {
                    listenLoopTask.Wait(1000);
                }
                catch (Exception)
                {
                }
                listenLoopTask = null;
            }

            shutdownToken?.Dispose();
            shutdownToken = null;
        }

        private async Task AcceptLoopAsync(CancellationToken token)
        {
            while (!token.IsCancellationRequested && listener != null)
            {
                HttpListenerContext ctx = null;
                try
                {
                    ctx = await listener.GetContextAsync().ConfigureAwait(false);
                }
                catch (ObjectDisposedException)
                {
                    break;
                }
                catch (HttpListenerException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    Debug.LogWarning($"[UserTestPanel] Listener error: {ex.Message}");
                }

                if (ctx != null)
                {
                    _ = Task.Run(() => HandleRequestAsync(ctx));
                }
            }
        }

        private async Task HandleRequestAsync(HttpListenerContext context)
        {
            try
            {
                AddCorsHeaders(context.Response);
                if (context.Request.HttpMethod == "OPTIONS")
                {
                    context.Response.StatusCode = 204;
                    context.Response.Close();
                    return;
                }

                var path = context.Request.Url?.AbsolutePath ?? "/";
                switch (path)
                {
                    case "/":
                    case "/index.html":
                        await RespondWithHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/games":
                    case "/games.html":
                        await RespondWithGameConfigHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/runtime":
                    case "/runtime.html":
                        await RespondWithRuntimeConfigHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/setup":
                    case "/setup.html":
                        await RespondWithSetupWizardHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/sdk":
                    case "/sdk.html":
                        await RespondWithSdkHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/telemetry":
                    case "/telemetry.html":
                        await RespondWithTelemetryHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/camera.mjpg":
                        await HandleCameraMjpegAsync(context).ConfigureAwait(false);
                        return;
                    case "/healthz":
                        await WriteJsonAsync(context.Response, 200, "ok", "panel alive").ConfigureAwait(false);
                        return;
                    case "/api/face":
                        await HandleFaceAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/flower":
                        await HandleFlowerAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/led":
                        await HandleLedAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/voice":
                        await HandleVoiceAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/voice/options":
                        await HandleVoiceOptionsAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/qwen/options":
                        await HandleQwenOptionsAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/logs":
                        await HandleLogsAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/speak":
                        await HandleSpeakAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/qwen/speak":
                        await HandleQwenSpeakAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/llm/prompt":
                        await HandleLlmPromptAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/vision/describe":
                        await HandleVisionDescribeAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/game":
                        await HandleGameAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/game/manifest":
                        await HandleGameManifestAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/file/pick":
                        await HandleFilePickAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/runtime/config":
                        await HandleRuntimeConfigAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/runtime/prereq":
                        await HandleRuntimePrereqAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/runtime/ollama":
                        await HandleRuntimeOllamaAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/asr":
                        await HandleAsrAsync(context).ConfigureAwait(false);
                        return;
                    case "/camera.jpg":
                        await HandleCameraJpegAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/camera/status":
                        await HandleCameraStatusAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/camera/ping":
                        await HandleCameraPingAsync(context).ConfigureAwait(false);
                        return;
                    case "/favicon.ico":
                        context.Response.StatusCode = 204;
                        context.Response.Close();
                        return;
                    default:
                        context.Response.StatusCode = 404;
                        await WriteJsonAsync(context.Response, 404, "error", "not found").ConfigureAwait(false);
                        return;
                }
            }
            catch (Exception ex)
            {
                try
                {
                    await WriteJsonAsync(context.Response, 500, "error", ex.Message).ConfigureAwait(false);
                }
                catch (Exception)
                {
                }
            }
        }

        private void Update()
        {
            // Throttled camera encoding on main thread
            if (!enableCameraPreview)
            {
                return;
            }

            if (optimizeExternalPreviewForTracking && !IsCameraClientActive())
            {
                // No active viewer: skip camera encoding to avoid stealing CPU from tracking.
                return;
            }

            // Choose source: external texture (e.g., MediaPipe) or local webcam
            if (useExternalCameraTexture)
            {
                var effectiveFps = optimizeExternalPreviewForTracking
                    ? Mathf.Max(1, Mathf.Min(cameraFps, Mathf.Max(1, externalPreviewMaxFps)))
                    : Mathf.Max(1, cameraFps);
                if (Time.realtimeSinceStartup < _nextCaptureRealtime)
            {
                    return;
                }
                var tex = GetExternalCameraTexture();
                if (tex == null)
                {
                    lock (_cameraLock)
                    {
                        _lastExternalTextureWidth = 0;
                        _lastExternalTextureHeight = 0;
                        _lastExternalTextureType = string.Empty;
                    }
                    return;
                }
                if (tex is WebCamTexture extCam && !extCam.isPlaying && _appIsVisible)
                {
                    try { extCam.Play(); } catch { }
                }
                lock (_cameraLock)
                {
                    _lastExternalTextureWidth = tex.width;
                    _lastExternalTextureHeight = tex.height;
                    _lastExternalTextureType = tex.GetType().Name;
                }
                CaptureTextureToJpeg(tex);
                _nextCaptureRealtime = Time.realtimeSinceStartup + Mathf.Max(0.01f, 1f / effectiveFps);
                return;
            }

            if (_webcam == null || !_webcam.isPlaying)
            {
                return;
            }
            if (!_webcam.didUpdateThisFrame)
            {
                return;
            }
            if (Time.realtimeSinceStartup < _nextCaptureRealtime)
            {
                return;
            }

            if (_cameraTexture == null || _cameraTexture.width != _webcam.width || _cameraTexture.height != _webcam.height)
            {
                if (_cameraTexture != null)
                {
                    Destroy(_cameraTexture);
                }
                // Use RGB24 for faster JPG encode
                _cameraTexture = new Texture2D(_webcam.width > 0 ? _webcam.width : Mathf.Max(2, cameraWidth),
                                               _webcam.height > 0 ? _webcam.height : Mathf.Max(2, cameraHeight),
                                               TextureFormat.RGB24, false);
            }

            try
            {
                _cameraTexture.SetPixels32(_webcam.GetPixels32());
                _cameraTexture.Apply(false, false);
                var quality = Mathf.Clamp(cameraJpegQuality, 1, 100);
                if (useExternalCameraTexture && optimizeExternalPreviewForTracking)
                {
                    quality = Mathf.Min(quality, 45);
                }
                var jpg = _cameraTexture.EncodeToJPG(quality);
                lock (_cameraLock)
                {
                    _latestJpeg = jpg;
                    _cameraFrameCount++;
                    _cameraLastFrameTs = Time.realtimeSinceStartup;
                    _cameraLastFrameUtcTicks = DateTime.UtcNow.Ticks;
                }
            }
            catch (System.Exception) { }

            _nextCaptureRealtime = Time.realtimeSinceStartup + Mathf.Max(0.01f, 1f / Mathf.Max(1, cameraFps));
        }

        [Serializable]
        private struct FaceRequest
        {
            public string mode;
            public float seconds;
            public float duration;
            public float fade;
            public string value;
        }

        [Serializable]
        private struct FlowerRequest
        {
            public string action;
        }

        [Serializable]
        private struct LedRequest
        {
            public string mode;
            public string color;
            public float brightness;
            public float period;
            public float duration;
        }

        [Serializable]
        private struct VoiceRequest
        {
            public string action;
            public string voice;
            public string value;
            public string model;
        }

        [Serializable]
        private struct SpeakRequest
        {
            public string text;
            public string voice;
            public string model;
            public string speaker;   // Qwen-style speaker id (alias of voice)
            public string instruct;  // Qwen-style emotion/style instruction
            public float speed;
            public float volume;
        }

        [Serializable]
        private struct QwenSpeakRequest
        {
            public string text;
            public string speaker;
            public string voice;   // compatibility alias of speaker
            public string instruct;
        }

        [Serializable]
        private struct LlmPromptRequest
        {
            public string prompt;
            public bool reset;
        }

        [Serializable]
        private struct LlmPromptConfigResponse
        {
            public string status;
            public string system_prompt;
            public bool runtime_override_active;
            public string source;
            public string detail;
        }

        [Serializable]
        private struct VisionDescribeRequest
        {
            public string prompt;
            public string model;
        }

        [Serializable]
        private struct OllamaGenerateResponse
        {
            public string response;
            public string error;
        }

        [Serializable]
        private struct GameRequest
        {
            public string action;
            public string name;
        }

        [Serializable]
        private struct FilePickRequest
        {
            public string title;
            public string filter;
            public string initial_dir;
            public string initial_filename;
        }

        [Serializable]
        private struct AsrRequest
        {
            public string action;
            public string mode;
            public string value;
            public bool listening;
        }

        [Serializable]
        private struct AsrConfigResponse
        {
            public string status;
            public string mode;
            public string source;
            public string[] available_modes;
            public bool openai_configured;
            public string openai_model;
        }

        [Serializable]
        private struct AsrErrorResponse
        {
            public string detail;
            public string message;
        }

        [Serializable]
        private struct RuntimeActionRequest
        {
            public string action;
            public string model;
        }

        private async Task HandleFaceAsync(HttpListenerContext context)
        {
            var request = ParseJsonBody<FaceRequest>(context.Request);
            var mode = (request.mode ?? string.Empty).Trim().ToLowerInvariant();
            var duration = request.seconds > 0f ? request.seconds : request.duration;
            if (duration <= 0f)
            {
                duration = DefaultFaceSeconds;
            }
            if (piHub == null)
            {
                await WriteJsonAsync(context.Response, 503, "error", "PiMessageHub not assigned").ConfigureAwait(false);
                return;
            }

            switch (mode)
            {
                case "excited":
                    await piHub.SendFacePresetAsync("excited", duration).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "face set to excited").ConfigureAwait(false);
                    return;
                case "happy":
                    await piHub.SendFacePresetAsync("happy", duration).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "face set to happy").ConfigureAwait(false);
                    return;
                case "neutral":
                    await piHub.SendFacePresetAsync("neutral", duration).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "face set to neutral").ConfigureAwait(false);
                    return;
                case "sad":
                    await piHub.SendFacePresetAsync("sad", duration).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "face set to sad").ConfigureAwait(false);
                    return;
                case "verysad": // verySad → lower-cased
                    await piHub.SendFacePresetAsync("verySad", duration).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "face set to verySad").ConfigureAwait(false);
                    return;
                case "idle":
                    await piHub.SendFaceIdleAsync().ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "face set to idle").ConfigureAwait(false);
                    return;
                case "custom":
                    var value = string.IsNullOrWhiteSpace(request.value) ? "idle" : request.value.Trim();
                    var payload = duration > 0f ? $"{value}:{duration.ToString(CultureInfo.InvariantCulture)}" : value;
                    await piHub.SendFaceAsync(payload).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", $"face command {value}").ConfigureAwait(false);
                    return;
                default:
                    // 未知模式统一按“预设名”处理，走与 happy 等相同路径
                    // 发送所选预设名（避免多变体覆盖导致瞬时回退 neutral）
                    await piHub.SendFacePresetAsync(mode, duration).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", $"face preset set to {mode}").ConfigureAwait(false);
                    return;
            }
        }

        

        private async Task HandleFlowerAsync(HttpListenerContext context)
        {
            var request = ParseJsonBody<FlowerRequest>(context.Request);
            var action = (request.action ?? string.Empty).Trim().ToLowerInvariant();
            if (piHub == null)
            {
                await WriteJsonAsync(context.Response, 503, "error", "PiMessageHub not assigned").ConfigureAwait(false);
                return;
            }

            switch (action)
            {
                case "open":
                    await piHub.OpenFlowerAsync().ConfigureAwait(false);
                    break;
                case "close":
                    await piHub.CloseFlowerAsync().ConfigureAwait(false);
                    break;
                case "open_hold":
                    await piHub.OpenFlowerHoldAsync().ConfigureAwait(false);
                    break;
                case "close_hold":
                    await piHub.CloseFlowerHoldAsync().ConfigureAwait(false);
                    break;
                case "center":
                    await piHub.CenterFlowerHoldAsync().ConfigureAwait(false);
                    break;
                case "stop":
                    await piHub.StopFlowerAsync().ConfigureAwait(false);
                    break;
                case "open_slow":
                    await piHub.OpenFlowerSlowAsync().ConfigureAwait(false);
                    break;
                case "close_slow":
                    await piHub.CloseFlowerSlowAsync().ConfigureAwait(false);
                    break;
                default:
                    await WriteJsonAsync(context.Response, 400, "error", "unknown flower action").ConfigureAwait(false);
                    return;
            }

            await WriteJsonAsync(context.Response, 200, "ok", $"flower action {action}").ConfigureAwait(false);
        }

        private async Task HandleLedAsync(HttpListenerContext context)
        {
            var request = ParseJsonBody<LedRequest>(context.Request);
            var mode = (request.mode ?? string.Empty).Trim().ToLowerInvariant();
            var brightness = request.brightness > 0f ? Mathf.Clamp01(request.brightness) : 1f;
            var period = request.period > 0f ? request.period : 1.5f;
            var duration = request.duration > 0f ? request.duration : 0f;
            if (piHub == null)
            {
                await WriteJsonAsync(context.Response, 503, "error", "PiMessageHub not assigned").ConfigureAwait(false);
                return;
            }

            switch (mode)
            {
                case "breathe":
                    var color = string.IsNullOrWhiteSpace(request.color) ? "#00BFFF" : NormalizeHex(request.color);
                    if (!IsHexColor(color))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "invalid color").ConfigureAwait(false);
                        return;
                    }
                    if (duration > 0f)
                    {
                        await piHub.SendLedBreathAsync(color, brightness, period, duration).ConfigureAwait(false);
                    }
                    else
                    {
                        await piHub.SendLedBreathAsync(color, brightness, period).ConfigureAwait(false);
                    }
                    await WriteJsonAsync(context.Response, 200, "ok", $"led breathe {color}").ConfigureAwait(false);
                    return;
                case "solid":
                    var solidColor = string.IsNullOrWhiteSpace(request.color) ? "#FFFFFF" : NormalizeHex(request.color);
                    if (!IsHexColor(solidColor))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "invalid color").ConfigureAwait(false);
                        return;
                    }
                    if (duration > 0f)
                    {
                        await piHub.SendLedSolidAsync(solidColor, brightness, duration).ConfigureAwait(false);
                    }
                    else
                    {
                        await piHub.SendLedSolidAsync(solidColor, brightness).ConfigureAwait(false);
                    }
                    await WriteJsonAsync(context.Response, 200, "ok", $"led solid {solidColor}").ConfigureAwait(false);
                    return;
                case "random":
                    if (duration > 0f)
                    {
                        await piHub.SendLedRandomAsync(duration).ConfigureAwait(false);
                    }
                    else
                    {
                        await piHub.SendLedRandomAsync().ConfigureAwait(false);
                    }
                    await WriteJsonAsync(context.Response, 200, "ok", "led random").ConfigureAwait(false);
                    return;
                case "off":
                    await piHub.SendLedOffAsync().ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "led off").ConfigureAwait(false);
                    return;
                default:
                    await WriteJsonAsync(context.Response, 400, "error", "unknown led mode").ConfigureAwait(false);
                    return;
            }
        }
        
        private void ScheduleLedOff(float seconds)
        {
            if (seconds <= 0f || piHub == null)
            {
                return;
            }
            _ = Task.Run(async () =>
            {
                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(seconds)).ConfigureAwait(false);
                    await piHub.SendLedOffAsync().ConfigureAwait(false);
                }
                catch (Exception) { }
            });
        }

        private async Task HandleVoiceAsync(HttpListenerContext context)
        {
            var request = ParseJsonBody<VoiceRequest>(context.Request);
            var action = (request.action ?? string.Empty).Trim().ToLowerInvariant();

            switch (action)
            {
                case "set":
                case "set_voice":
                    var newVoice = string.IsNullOrWhiteSpace(request.voice) ? request.value : request.voice;
                    newVoice = (newVoice ?? string.Empty).Trim();
                    if (string.IsNullOrEmpty(newVoice))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "voice code required").ConfigureAwait(false);
                        return;
                    }
                    activeVoiceCode = newVoice;
                    if (voiceLauncher != null)
                    {
                        var voiceToSend = activeVoiceCode;
                        var modelToSend = activeTtsModel;
                        PostToMainThread(() => voiceLauncher.SetTtsOptionsForTester(voiceToSend, modelToSend));
                    }
                    await WriteJsonAsync(context.Response, 200, "ok", $"voice set to {activeVoiceCode}").ConfigureAwait(false);
                    return;
                case "set_model":
                case "model":
                    var newModel = string.IsNullOrWhiteSpace(request.model) ? request.value : request.model;
                    newModel = (newModel ?? string.Empty).Trim();
                    if (string.IsNullOrEmpty(newModel))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "model identifier required").ConfigureAwait(false);
                        return;
                    }
                    activeTtsModel = newModel;
                    if (voiceLauncher != null)
                    {
                        var voiceToSend2 = activeVoiceCode;
                        var modelToSend2 = activeTtsModel;
                        PostToMainThread(() => voiceLauncher.SetTtsOptionsForTester(voiceToSend2, modelToSend2));
                    }
                    await WriteJsonAsync(context.Response, 200, "ok", $"tts model set to {activeTtsModel}").ConfigureAwait(false);
                    return;
                default:
                    await WriteJsonAsync(context.Response, 400, "error", "unknown voice action").ConfigureAwait(false);
                    return;
            }
        }

        private async Task HandleSpeakAsync(HttpListenerContext context)
        {
            var request = ParseJsonBody<SpeakRequest>(context.Request);
            var text = (request.text ?? string.Empty).Trim();
            if (string.IsNullOrEmpty(text))
            {
                await WriteJsonAsync(context.Response, 400, "error", "text required").ConfigureAwait(false);
                return;
            }

            var requestedVoice = string.IsNullOrWhiteSpace(request.speaker) ? request.voice : request.speaker;
            requestedVoice = string.IsNullOrWhiteSpace(requestedVoice) ? activeVoiceCode : requestedVoice.Trim();
            if (string.IsNullOrWhiteSpace(requestedVoice))
            {
                requestedVoice = DetermineInitialVoiceCode();
            }

            var requestedModel = string.IsNullOrWhiteSpace(request.model) ? activeTtsModel : request.model.Trim();
            if (string.IsNullOrWhiteSpace(requestedModel))
            {
                requestedModel = DetermineInitialTtsModel();
            }

            var requestedSpeed = request.speed > 0f ? request.speed : 1f;
            var requestedVolume = request.volume > 0f ? request.volume : 1f;
            var requestedInstruct = string.IsNullOrWhiteSpace(request.instruct) ? string.Empty : request.instruct.Trim();
            if (string.IsNullOrWhiteSpace(requestedInstruct))
            {
                requestedInstruct = string.Empty;
            }

            // 优先让 Unity 侧直接播放（经由 VoiceGameLauncher → Piper /speak）
            if (voiceLauncher != null)
            {
                ConversationLog.AddEntry(ConversationRole.Wizard, text, "tester_panel");
                var voiceToSend = requestedVoice;
                var modelToSend = requestedModel;
                PostToMainThread(() => voiceLauncher.TriggerManualTesterSpeak(text, voiceToSend, modelToSend, requestedInstruct));
                await WriteJsonAsync(context.Response, 200, "ok", "playing locally").ConfigureAwait(false);
                return;
            }

            // 回退：如果未绑定 VoiceGameLauncher，则仍向语音服务发送请求（但不会在本机播放）
            var url = (voiceServiceUrl ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(url))
            {
                await WriteJsonAsync(context.Response, 503, "error", "voice service URL not configured").ConfigureAwait(false);
                return;
            }

            try
            {
                var payload = BuildSpeakPayload(text, requestedVoice, requestedModel, requestedSpeed, requestedVolume);
                using (var content = new StringContent(payload, Encoding.UTF8, "application/json"))
                {
                    var response = await SharedHttpClient.PostAsync(url, content).ConfigureAwait(false);
                    var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                    if (!response.IsSuccessStatusCode)
                    {
                        await WriteJsonAsync(context.Response, (int)response.StatusCode, "error", $"voice service error: {body}").ConfigureAwait(false);
                        return;
                    }
                }
                ConversationLog.AddEntry(ConversationRole.Wizard, text, "tester_panel");
                await WriteJsonAsync(context.Response, 200, "ok", "synthesis complete (no local playback)").ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 500, "error", $"voice request failed: {ex.Message}").ConfigureAwait(false);
            }
        }

        private async Task HandleQwenSpeakAsync(HttpListenerContext context)
        {
            var request = ParseJsonBody<QwenSpeakRequest>(context.Request);
            var text = (request.text ?? string.Empty).Trim();
            if (string.IsNullOrEmpty(text))
            {
                await WriteJsonAsync(context.Response, 400, "error", "text required").ConfigureAwait(false);
                return;
            }

            // Respect tester-selected speaker first, then fall back to remembered/default values.
            var speaker = string.IsNullOrWhiteSpace(request.speaker) ? request.voice : request.speaker;
            speaker = string.IsNullOrWhiteSpace(speaker) ? activeQwenSpeaker : speaker.Trim();
            speaker = string.IsNullOrWhiteSpace(speaker) ? defaultQwenSpeaker : speaker;
            if (string.IsNullOrWhiteSpace(speaker))
            {
                speaker = DetermineInitialQwenSpeaker();
            }
            speaker = string.IsNullOrWhiteSpace(speaker) ? string.Empty : speaker.Trim();

            activeQwenSpeaker = speaker;

            // Force a fixed style prompt for Qwen requests.
            var instruct = string.IsNullOrWhiteSpace(fixedQwenInstruct) ? "friendly" : fixedQwenInstruct.Trim();

            // Always route through Unity playback so AEC/render tap works.
            if (voiceLauncher != null)
            {
                ConversationLog.AddEntry(ConversationRole.Wizard, text, "tester_panel_qwen");
                var speakerToSend = speaker;
                PostToMainThread(() => voiceLauncher.TriggerManualTesterSpeak(text, speakerToSend, modelPath: null, ttsInstruct: instruct));
                await WriteJsonAsync(context.Response, 200, "ok", "playing locally (qwen)").ConfigureAwait(false);
                return;
            }

            // Fallback: send to voice service (won't play locally).
            var url = (voiceServiceUrl ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(url))
            {
                await WriteJsonAsync(context.Response, 503, "error", "voice service URL not configured").ConfigureAwait(false);
                return;
            }

            try
            {
                // Use GET /speak for Qwen so we can pass instruct.
                var query = new List<string>
                {
                    "text=" + Uri.EscapeDataString(text),
                    "voice=" + Uri.EscapeDataString(speaker),
                };
                if (!string.IsNullOrWhiteSpace(instruct))
                {
                    query.Add("instruct=" + Uri.EscapeDataString(instruct));
                }
                var fullUrl = url + (url.Contains("?") ? "&" : "?") + string.Join("&", query);
                var response = await SharedHttpClient.GetAsync(fullUrl).ConfigureAwait(false);
                if (!response.IsSuccessStatusCode)
                {
                    var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, (int)response.StatusCode, "error", $"voice service error: {body}").ConfigureAwait(false);
                    return;
                }
                ConversationLog.AddEntry(ConversationRole.Wizard, text, "tester_panel_qwen");
                await WriteJsonAsync(context.Response, 200, "ok", "synthesis complete (no local playback)").ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 500, "error", $"voice request failed: {ex.Message}").ConfigureAwait(false);
            }
        }

        private string ResolveLlmServiceBaseUrl()
        {
            var url = (llmServiceBaseUrl ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(url))
            {
                url = "http://127.0.0.1:8000";
            }
            return url.TrimEnd('/');
        }

        private string ResolveOllamaBaseUrl()
        {
            var url = (ollamaBaseUrl ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(url))
            {
                url = (Environment.GetEnvironmentVariable("OLLAMA_BASE_URL") ?? string.Empty).Trim();
            }
            if (string.IsNullOrWhiteSpace(url))
            {
                url = "http://127.0.0.1:11434";
            }
            return url.TrimEnd('/');
        }

        private string ResolveVisionModel(string requestedModel)
        {
            var model = (requestedModel ?? string.Empty).Trim();
            if (!string.IsNullOrWhiteSpace(model))
            {
                return model;
            }
            model = (defaultVisionModel ?? string.Empty).Trim();
            if (!string.IsNullOrWhiteSpace(model))
            {
                return model;
            }
            model = (Environment.GetEnvironmentVariable("OLLAMA_MODEL") ?? string.Empty).Trim();
            if (!string.IsNullOrWhiteSpace(model))
            {
                return model;
            }
            return "gemma3:4b";
        }

        private bool TryGetLatestCameraJpegCopy(out byte[] jpeg)
        {
            jpeg = null;
            lock (_cameraLock)
            {
                if (_latestJpeg == null || _latestJpeg.Length <= 0)
                {
                    return false;
                }
                jpeg = new byte[_latestJpeg.Length];
                Buffer.BlockCopy(_latestJpeg, 0, jpeg, 0, _latestJpeg.Length);
            }
            return true;
        }

        private async Task<byte[]> TryGetLatestCameraJpegWithWaitAsync(int waitMs, int pollMs = 50)
        {
            var timeoutMs = Mathf.Max(0, waitMs);
            var intervalMs = Mathf.Clamp(pollMs, 10, 250);
            var startedAt = DateTime.UtcNow;

            while (true)
            {
                if (TryGetLatestCameraJpegCopy(out var jpeg))
                {
                    return jpeg;
                }

                var elapsedMs = (int)(DateTime.UtcNow - startedAt).TotalMilliseconds;
                if (elapsedMs >= timeoutMs)
                {
                    return null;
                }

                await Task.Delay(intervalMs).ConfigureAwait(false);
            }
        }

        private string BuildNoCameraFrameHint()
        {
            int bytes;
            int frameCount;
            lock (_cameraLock)
            {
                bytes = _latestJpeg != null ? _latestJpeg.Length : 0;
                frameCount = _cameraFrameCount;
            }

            var mode = useExternalCameraTexture ? "external" : "webcam";
            var clientActive = IsCameraClientActive() ? "true" : "false";
            return $"no camera frame (mode={mode}, frame_count={frameCount}, jpeg_bytes={bytes}, client_active={clientActive}). Start Preview and wait 1-2 seconds.";
        }

        private async Task HandleVisionDescribeAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method != "POST")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "POST,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            if (!enableCameraPreview)
            {
                await WriteJsonAsync(context.Response, 503, "error", "camera preview disabled").ConfigureAwait(false);
                return;
            }

            TouchCameraClientHeartbeat();

            var jpeg = await TryGetLatestCameraJpegWithWaitAsync(1500).ConfigureAwait(false);
            if (jpeg == null || jpeg.Length <= 0)
            {
                await WriteJsonAsync(context.Response, 503, "error", BuildNoCameraFrameHint()).ConfigureAwait(false);
                return;
            }

            var request = ParseJsonBody<VisionDescribeRequest>(context.Request);
            var prompt = (request.prompt ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(prompt))
            {
                prompt = string.IsNullOrWhiteSpace(defaultVisionPrompt)
                    ? "Describe what you see in this camera frame in 2-4 concise sentences."
                    : defaultVisionPrompt.Trim();
            }
            var model = ResolveVisionModel(request.model);
            var ollamaBaseUrl = ResolveOllamaBaseUrl();
            var ollamaProbe = await ProbeOllamaAsync(ollamaBaseUrl, model).ConfigureAwait(false);
            if (!ollamaProbe.Reachable)
            {
                await WriteJsonAsync(
                    context.Response,
                    503,
                    "error",
                    $"vision backend unavailable at {ollamaBaseUrl}: {ollamaProbe.Error}")
                    .ConfigureAwait(false);
                return;
            }
            if (!ollamaProbe.ModelAvailable)
            {
                await WriteJsonAsync(
                    context.Response,
                    503,
                    "error",
                    $"vision model not available in Ollama: {model}. Run: ollama pull {model}")
                    .ConfigureAwait(false);
                return;
            }
            var imageBase64 = Convert.ToBase64String(jpeg);

            var payload = new StringBuilder(imageBase64.Length + prompt.Length + model.Length + 256)
                .Append("{\"model\":\"").Append(EscapeJson(model)).Append('"')
                .Append(",\"prompt\":\"").Append(EscapeJson(prompt)).Append('"')
                .Append(",\"stream\":false")
                .Append(",\"images\":[\"").Append(imageBase64).Append("\"]}")
                .ToString();

            var url = ollamaBaseUrl + "/api/generate";
            try
            {
                using (var content = new StringContent(payload, Encoding.UTF8, "application/json"))
                {
                    using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(300)))
                    {
                        var response = await SharedHttpClient.PostAsync(url, content, cts.Token).ConfigureAwait(false);
                        var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                        if (!response.IsSuccessStatusCode)
                        {
                            await WriteJsonAsync(context.Response, (int)response.StatusCode, "error", $"vision request failed: {body}").ConfigureAwait(false);
                            return;
                        }

                        string description = string.Empty;
                        try
                        {
                            var result = JsonUtility.FromJson<OllamaGenerateResponse>(body);
                            description = (result.response ?? string.Empty).Trim();
                        }
                        catch (Exception)
                        {
                            description = string.Empty;
                        }

                        if (string.IsNullOrWhiteSpace(description))
                        {
                            description = body.Trim();
                        }

                        var responseJson = new StringBuilder(description.Length + prompt.Length + model.Length + 128)
                            .Append("{\"status\":\"ok\"")
                            .Append(",\"message\":\"vision description ready\"")
                            .Append(",\"model\":\"").Append(EscapeJson(model)).Append('"')
                            .Append(",\"prompt\":\"").Append(EscapeJson(prompt)).Append('"')
                            .Append(",\"description\":\"").Append(EscapeJson(description)).Append("\"}")
                            .ToString();
                        await WriteRawJsonAsync(context.Response, 200, responseJson).ConfigureAwait(false);
                    }
                }
            }
            catch (TaskCanceledException)
            {
                await WriteJsonAsync(
                    context.Response,
                    504,
                    "error",
                    "vision request timed out after 300s (5 minutes). Check Ollama is running and model is loaded.")
                    .ConfigureAwait(false);
            }
            catch (HttpRequestException ex)
            {
                var detail = ex.InnerException?.Message;
                if (string.IsNullOrWhiteSpace(detail))
                {
                    detail = ex.Message;
                }

                await WriteJsonAsync(
                    context.Response,
                    502,
                    "error",
                    $"vision request failed to {url}: {detail}")
                    .ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 502, "error", $"vision request failed: {ex.Message}").ConfigureAwait(false);
            }
        }

        private static async Task WriteRawJsonAsync(HttpListenerResponse response, int statusCode, string json)
        {
            var payload = string.IsNullOrWhiteSpace(json)
                ? "{\"status\":\"error\",\"message\":\"empty response\"}"
                : json;
            var bytes = Encoding.UTF8.GetBytes(payload);
            response.StatusCode = statusCode;
            response.ContentType = "application/json";
            response.ContentLength64 = bytes.Length;
            await response.OutputStream.WriteAsync(bytes, 0, bytes.Length).ConfigureAwait(false);
            response.Close();
        }

        private async Task HandleLlmPromptAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method == "GET")
            {
                try
                {
                    var getUrl = ResolveLlmServiceBaseUrl() + "/respond/config";
                    var response = await SharedHttpClient.GetAsync(getUrl).ConfigureAwait(false);
                    var getBody = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                    if (!response.IsSuccessStatusCode)
                    {
                        await WriteJsonAsync(context.Response, (int)response.StatusCode, "error", $"failed to load llm prompt: {getBody}").ConfigureAwait(false);
                        return;
                    }
                    await WriteRawJsonAsync(context.Response, 200, getBody).ConfigureAwait(false);
                }
                catch (Exception ex)
                {
                    await WriteJsonAsync(context.Response, 502, "error", $"failed to load llm prompt: {ex.Message}").ConfigureAwait(false);
                }
                return;
            }

            if (method != "POST")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "GET,POST,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            var request = ParseJsonBody<LlmPromptRequest>(context.Request);
            var reset = request.reset;
            var prompt = (request.prompt ?? string.Empty).Trim();
            if (!reset && string.IsNullOrEmpty(prompt))
            {
                await WriteJsonAsync(context.Response, 400, "error", "prompt required unless reset=true").ConfigureAwait(false);
                return;
            }

            var url = ResolveLlmServiceBaseUrl() + "/respond/config";
            var body = reset
                ? "{\"reset\":true}"
                : "{\"system_prompt\":\"" + EscapeJson(prompt) + "\"}";

            try
            {
                using (var content = new StringContent(body, Encoding.UTF8, "application/json"))
                {
                    var response = await SharedHttpClient.PostAsync(url, content).ConfigureAwait(false);
                    var raw = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                    if (!response.IsSuccessStatusCode)
                    {
                        await WriteJsonAsync(context.Response, (int)response.StatusCode, "error", $"failed to update llm prompt: {raw}").ConfigureAwait(false);
                        return;
                    }
                }

                var latest = await SharedHttpClient.GetAsync(url).ConfigureAwait(false);
                var latestBody = await latest.Content.ReadAsStringAsync().ConfigureAwait(false);
                if (!latest.IsSuccessStatusCode)
                {
                    await WriteJsonAsync(context.Response, (int)latest.StatusCode, "error", $"failed to load updated llm prompt: {latestBody}").ConfigureAwait(false);
                    return;
                }
                await WriteRawJsonAsync(context.Response, 200, latestBody).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 502, "error", $"failed to update llm prompt: {ex.Message}").ConfigureAwait(false);
            }
        }

        private async Task HandleGameAsync(HttpListenerContext context)
        {
            var request = ParseJsonBody<GameRequest>(context.Request);
            var action = (request.action ?? string.Empty).Trim().ToLowerInvariant();
            if (voiceLauncher == null)
            {
                await WriteJsonAsync(context.Response, 503, "error", "VoiceGameLauncher not assigned").ConfigureAwait(false);
                return;
            }

            switch (action)
            {
                case "launch":
                case "open":
                    var gameName = (request.name ?? string.Empty).Trim();
                    if (string.IsNullOrEmpty(gameName))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "game name required").ConfigureAwait(false);
                        return;
                    }
					PostToMainThread(() => voiceLauncher.TriggerLaunchForTester(gameName));
                    await WriteJsonAsync(context.Response, 200, "ok", $"launching {gameName}").ConfigureAwait(false);
                    return;
                case "exit":
                case "close":
					PostToMainThread(() => voiceLauncher.TriggerExitForTester());
                    await WriteJsonAsync(context.Response, 200, "ok", "exit intent sent").ConfigureAwait(false);
                    return;
                default:
                    await WriteJsonAsync(context.Response, 400, "error", "unknown game action").ConfigureAwait(false);
                    return;
            }
        }

        private async Task HandleGameManifestAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method == "GET")
            {
                await WriteGameManifestStatusAsync(context.Response).ConfigureAwait(false);
                return;
            }

            if (method != "POST")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "GET,POST,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            var body = ReadRequestBody(context.Request);
            JSONNode requestNode;
            try
            {
                requestNode = JSONNode.Parse(body);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 400, "error", $"invalid json body: {ex.Message}").ConfigureAwait(false);
                return;
            }

            var gamesNode = requestNode?["games"];
            if (gamesNode == null || !gamesNode.IsArray)
            {
                await WriteJsonAsync(context.Response, 400, "error", "games array is required").ConfigureAwait(false);
                return;
            }

            var manifestPath = ResolveGameManifestPath();
            var load = LoadGameManifestRoot(manifestPath);
            if (!load.Success)
            {
                await WriteJsonAsync(context.Response, 500, "error", load.Error).ConfigureAwait(false);
                return;
            }
            var manifestBaseDir = Path.GetDirectoryName(manifestPath);
            if (string.IsNullOrWhiteSpace(manifestBaseDir))
            {
                manifestBaseDir = ResolveProjectRootPath();
            }
            var launcherEnv = LoadLauncherEnvOverrides();

            var existingRoot = load.Root;
            var existingById = new Dictionary<string, JSONObject>(StringComparer.OrdinalIgnoreCase);
            var oldGames = existingRoot["games"];
            if (oldGames != null && oldGames.IsArray)
            {
                var oldArray = oldGames.AsArray;
                for (int i = 0; i < oldArray.Count; i++)
                {
                    var oldNode = oldArray[i];
                    if (oldNode == null || !oldNode.IsObject)
                    {
                        continue;
                    }

                    var oldObj = oldNode.AsObject;
                    var oldId = NormalizeGameId((oldObj["id"]?.Value ?? string.Empty).Trim());
                    if (string.IsNullOrWhiteSpace(oldId))
                    {
                        continue;
                    }
                    existingById[oldId] = oldObj;
                }
            }

            var nextGames = new JSONArray();
            var seenIds = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var reqArray = gamesNode.AsArray;
            for (int i = 0; i < reqArray.Count; i++)
            {
                var row = reqArray[i];
                if (row == null || !row.IsObject)
                {
                    continue;
                }

                var rowObj = row.AsObject;
                var rawId = (rowObj["id"]?.Value ?? string.Empty).Trim();
                var rawName = (rowObj["name"]?.Value ?? string.Empty).Trim();
                var id = NormalizeGameId(string.IsNullOrWhiteSpace(rawId) ? rawName : rawId);
                if (string.IsNullOrWhiteSpace(id))
                {
                    await WriteJsonAsync(context.Response, 400, "error", $"invalid game id at index {i}").ConfigureAwait(false);
                    return;
                }
                if (!seenIds.Add(id))
                {
                    await WriteJsonAsync(context.Response, 400, "error", $"duplicate game id: {id}").ConfigureAwait(false);
                    return;
                }

                var name = string.IsNullOrWhiteSpace(rawName) ? id : rawName;
                var rawExecInput = (rowObj["exec"]?.Value ?? string.Empty).Trim();
                var rawWorkdirInput = (rowObj["workdir"]?.Value ?? string.Empty).Trim();
                var exec = ResolvePathFromConfigOrPlaceholder(
                    rawExecInput,
                    manifestBaseDir,
                    launcherEnv,
                    allowCommandName: true);
                var workdir = ResolvePathFromConfigOrPlaceholder(
                    rawWorkdirInput,
                    manifestBaseDir,
                    launcherEnv,
                    allowCommandName: false);
                if (!string.IsNullOrWhiteSpace(rawExecInput) && string.IsNullOrWhiteSpace(exec))
                {
                    await WriteJsonAsync(
                        context.Response,
                        400,
                        "error",
                        $"game '{id}' executable path is unresolved. Please provide an absolute path.")
                        .ConfigureAwait(false);
                    return;
                }
                if (!string.IsNullOrWhiteSpace(rawWorkdirInput) && string.IsNullOrWhiteSpace(workdir))
                {
                    await WriteJsonAsync(
                        context.Response,
                        400,
                        "error",
                        $"game '{id}' workdir is unresolved. Please provide an absolute path.")
                        .ConfigureAwait(false);
                    return;
                }
                var keywords = ParseKeywordList(rowObj);

                JSONObject target;
                if (!existingById.TryGetValue(id, out target))
                {
                    target = new JSONObject();
                }

                target["id"] = id;
                target["name"] = name;
                target["exec"] = exec;
                target["workdir"] = workdir;

                var synonyms = new JSONArray();
                foreach (var keyword in keywords)
                {
                    synonyms.Add(keyword);
                }
                target["synonyms"] = synonyms;

                if (target["args"] == null || !target["args"].IsArray)
                {
                    target["args"] = new JSONArray();
                }
                if (target["env"] == null || !target["env"].IsObject)
                {
                    target["env"] = new JSONObject();
                }

                nextGames.Add(target);
            }

            existingRoot["games"] = nextGames;

            try
            {
                var parent = Path.GetDirectoryName(manifestPath);
                if (!string.IsNullOrWhiteSpace(parent))
                {
                    Directory.CreateDirectory(parent);
                }
                File.WriteAllText(manifestPath, existingRoot.ToString(2), Encoding.UTF8);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 500, "error", $"failed to save manifest: {ex.Message}").ConfigureAwait(false);
                return;
            }

            await WriteRawJsonAsync(
                context.Response,
                200,
                "{\"status\":\"ok\",\"message\":\"saved. restart intent_service and game_launcher to apply immediately.\"}")
                .ConfigureAwait(false);
        }

        private async Task HandleFilePickAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method != "POST")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "POST,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            var request = ParseJsonBody<FilePickRequest>(context.Request);
            var title = string.IsNullOrWhiteSpace(request.title) ? "Select File" : request.title.Trim();
            var filter = string.IsNullOrWhiteSpace(request.filter)
                ? "Executable Files (*.exe)|*.exe|All Files (*.*)|*.*"
                : request.filter.Trim();
            var projectRoot = ResolveProjectRootPath();
            var initialDir = NormalizePathOrCommandForConfig(request.initial_dir, projectRoot, allowCommandName: false);
            var initialFile = NormalizePathOrCommandForConfig(request.initial_filename, projectRoot, allowCommandName: false);
            var pick = await Task.Run(() => ShowHostOpenFileDialog(title, filter, initialDir, initialFile)).ConfigureAwait(false);
            if (!pick.Success)
            {
                await WriteJsonAsync(context.Response, 500, "error", pick.Error).ConfigureAwait(false);
                return;
            }

            var payload = new JSONObject();
            payload["status"] = "ok";
            payload["cancelled"] = pick.Cancelled;
            payload["path"] = pick.Path;
            payload["directory"] = string.IsNullOrWhiteSpace(pick.Path)
                ? string.Empty
                : (Path.GetDirectoryName(pick.Path) ?? string.Empty);
            await WriteRawJsonAsync(context.Response, 200, payload.ToString()).ConfigureAwait(false);
        }

        private static (bool Success, bool Cancelled, string Path, string Error) ShowHostOpenFileDialog(
            string title,
            string filter,
            string initialDir,
            string initialFile)
        {
            var done = new ManualResetEvent(false);
            var success = false;
            var cancelled = true;
            var selectedPath = string.Empty;
            var error = string.Empty;

            void Work()
            {
                try
                {
                    var formsAssembly = Assembly.Load("System.Windows.Forms");
                    var openFileDialogType = formsAssembly.GetType("System.Windows.Forms.OpenFileDialog", throwOnError: false);
                    if (openFileDialogType == null)
                    {
                        error = "native file dialog not available on this runtime";
                        return;
                    }

                    var dialog = Activator.CreateInstance(openFileDialogType);
                    if (dialog == null)
                    {
                        error = "failed to create file dialog instance";
                        return;
                    }

                    SetReflectedProperty(openFileDialogType, dialog, "Title", title);
                    SetReflectedProperty(openFileDialogType, dialog, "Filter", filter);
                    SetReflectedProperty(openFileDialogType, dialog, "CheckFileExists", true);
                    SetReflectedProperty(openFileDialogType, dialog, "Multiselect", false);
                    SetReflectedProperty(openFileDialogType, dialog, "RestoreDirectory", true);
                    if (!string.IsNullOrWhiteSpace(initialDir) && Directory.Exists(initialDir))
                    {
                        SetReflectedProperty(openFileDialogType, dialog, "InitialDirectory", initialDir);
                    }
                    if (!string.IsNullOrWhiteSpace(initialFile))
                    {
                        SetReflectedProperty(openFileDialogType, dialog, "FileName", initialFile);
                    }

                    var showDialogMethod = openFileDialogType.GetMethod("ShowDialog", Type.EmptyTypes);
                    if (showDialogMethod == null)
                    {
                        error = "file dialog ShowDialog method not found";
                        return;
                    }

                    var result = showDialogMethod.Invoke(dialog, null);
                    var code = Convert.ToInt32(result, CultureInfo.InvariantCulture);
                    // System.Windows.Forms.DialogResult.OK == 1
                    if (code != 1)
                    {
                        success = true;
                        cancelled = true;
                        return;
                    }

                    var fileNameObj = openFileDialogType.GetProperty("FileName")?.GetValue(dialog, null);
                    var filePath = (fileNameObj as string ?? string.Empty).Trim();
                    if (string.IsNullOrWhiteSpace(filePath))
                    {
                        success = true;
                        cancelled = true;
                        return;
                    }

                    selectedPath = Path.GetFullPath(filePath);
                    success = true;
                    cancelled = false;
                }
                catch (Exception ex)
                {
                    error = ex.Message;
                }
                finally
                {
                    done.Set();
                }
            }

            try
            {
                var thread = new Thread(Work);
                thread.SetApartmentState(ApartmentState.STA);
                thread.IsBackground = true;
                thread.Start();
                done.WaitOne();
            }
            catch (Exception ex)
            {
                return (false, false, string.Empty, ex.Message);
            }
            finally
            {
                done.Dispose();
            }

            if (!string.IsNullOrWhiteSpace(error))
            {
                return (false, false, string.Empty, error);
            }
            return (success, cancelled, selectedPath, string.Empty);
        }

        private static void SetReflectedProperty(Type targetType, object instance, string name, object value)
        {
            if (targetType == null || instance == null || string.IsNullOrWhiteSpace(name))
            {
                return;
            }

            var prop = targetType.GetProperty(name);
            if (prop == null || !prop.CanWrite)
            {
                return;
            }

            try
            {
                prop.SetValue(instance, value, null);
            }
            catch
            {
                // Ignore unsupported property assignments.
            }
        }

        private async Task WriteGameManifestStatusAsync(HttpListenerResponse response)
        {
            var manifestPath = ResolveGameManifestPath();
            var load = LoadGameManifestRoot(manifestPath);
            if (!load.Success)
            {
                await WriteJsonAsync(response, 500, "error", load.Error).ConfigureAwait(false);
                return;
            }
            var manifestBaseDir = Path.GetDirectoryName(manifestPath);
            if (string.IsNullOrWhiteSpace(manifestBaseDir))
            {
                manifestBaseDir = ResolveProjectRootPath();
            }
            var launcherEnv = LoadLauncherEnvOverrides();

            var root = load.Root;
            var outGames = new JSONArray();
            var unresolvedCount = 0;
            var gamesNode = root["games"];
            if (gamesNode != null && gamesNode.IsArray)
            {
                var gamesArray = gamesNode.AsArray;
                for (int i = 0; i < gamesArray.Count; i++)
                {
                    var node = gamesArray[i];
                    if (node == null || !node.IsObject)
                    {
                        continue;
                    }

                    var obj = node.AsObject;
                    var id = (obj["id"]?.Value ?? string.Empty).Trim();
                    if (string.IsNullOrWhiteSpace(id))
                    {
                        continue;
                    }

                    var item = new JSONObject();
                    item["id"] = id;
                    item["name"] = (obj["name"]?.Value ?? string.Empty).Trim();
                    var rawExec = (obj["exec"]?.Value ?? string.Empty).Trim();
                    var rawWorkdir = (obj["workdir"]?.Value ?? string.Empty).Trim();
                    var resolvedExec = ResolvePathFromConfigOrPlaceholder(
                        rawExec,
                        manifestBaseDir,
                        launcherEnv,
                        allowCommandName: true);
                    var resolvedWorkdir = ResolvePathFromConfigOrPlaceholder(
                        rawWorkdir,
                        manifestBaseDir,
                        launcherEnv,
                        allowCommandName: false);
                    if (!string.IsNullOrWhiteSpace(rawExec) && string.IsNullOrWhiteSpace(resolvedExec))
                    {
                        unresolvedCount++;
                    }
                    if (!string.IsNullOrWhiteSpace(rawWorkdir) && string.IsNullOrWhiteSpace(resolvedWorkdir))
                    {
                        unresolvedCount++;
                    }
                    item["exec"] = resolvedExec;
                    item["workdir"] = resolvedWorkdir;

                    var keywords = new JSONArray();
                    var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                    var synonymsNode = obj["synonyms"];
                    if (synonymsNode != null && synonymsNode.IsArray)
                    {
                        var synonyms = synonymsNode.AsArray;
                        for (int j = 0; j < synonyms.Count; j++)
                        {
                            var text = (synonyms[j]?.Value ?? string.Empty).Trim();
                            if (!string.IsNullOrWhiteSpace(text) && seen.Add(text))
                            {
                                keywords.Add(text);
                            }
                        }
                    }
                    item["keywords"] = keywords;
                    outGames.Add(item);
                }
            }

            var payload = new JSONObject();
            payload["status"] = "ok";
            payload["path"] = manifestPath;
            payload["unresolved_count"] = unresolvedCount;
            payload["games"] = outGames;
            await WriteRawJsonAsync(response, 200, payload.ToString()).ConfigureAwait(false);
        }

        private static string ReadRequestBody(HttpListenerRequest request)
        {
            using (var reader = new StreamReader(request.InputStream, request.ContentEncoding ?? Encoding.UTF8))
            {
                return reader.ReadToEnd();
            }
        }

        private static string NormalizeGameId(string raw)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return string.Empty;
            }

            var text = raw.Trim();
            var sb = new StringBuilder(text.Length);
            var wroteUnderscore = false;
            for (int i = 0; i < text.Length; i++)
            {
                var ch = char.ToLowerInvariant(text[i]);
                if ((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9'))
                {
                    sb.Append(ch);
                    wroteUnderscore = false;
                    continue;
                }

                if (ch == '_' || ch == '-' || char.IsWhiteSpace(ch))
                {
                    if (!wroteUnderscore && sb.Length > 0)
                    {
                        sb.Append('_');
                        wroteUnderscore = true;
                    }
                }
            }

            var normalized = sb.ToString().Trim('_');
            return normalized;
        }

        private static List<string> ParseKeywordList(JSONObject rowObj)
        {
            var result = new List<string>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

            var keywordsNode = rowObj["keywords"];
            if (keywordsNode != null && keywordsNode.IsArray)
            {
                var arr = keywordsNode.AsArray;
                for (int i = 0; i < arr.Count; i++)
                {
                    var value = (arr[i]?.Value ?? string.Empty).Trim();
                    if (!string.IsNullOrWhiteSpace(value) && seen.Add(value))
                    {
                        result.Add(value);
                    }
                }
            }

            var keywordsText = (rowObj["keywords_text"]?.Value ?? string.Empty).Trim();
            if (!string.IsNullOrWhiteSpace(keywordsText))
            {
                var parts = keywordsText.Split(',');
                for (int i = 0; i < parts.Length; i++)
                {
                    var value = (parts[i] ?? string.Empty).Trim();
                    if (!string.IsNullOrWhiteSpace(value) && seen.Add(value))
                    {
                        result.Add(value);
                    }
                }
            }

            return result;
        }

        private async Task HandleRuntimeConfigAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method == "GET")
            {
                await WriteRuntimeConfigStatusAsync(context.Response, "runtime config").ConfigureAwait(false);
                return;
            }

            if (method != "POST")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "GET,POST,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            var body = ReadRequestBody(context.Request);
            JSONNode requestNode;
            try
            {
                requestNode = string.IsNullOrWhiteSpace(body) ? new JSONObject() : JSONNode.Parse(body);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 400, "error", $"invalid json body: {ex.Message}").ConfigureAwait(false);
                return;
            }

            if (requestNode == null || !requestNode.IsObject)
            {
                await WriteJsonAsync(context.Response, 400, "error", "json object body is required").ConfigureAwait(false);
                return;
            }

            var requestObj = requestNode.AsObject;
            var configPath = ResolveLauncherConfigPath();
            var load = LoadLauncherConfigRoot(configPath);
            if (!load.Success)
            {
                await WriteJsonAsync(context.Response, 500, "error", load.Error).ConfigureAwait(false);
                return;
            }

            var root = load.Root;
            var pythonObj = EnsureObjectNode(root, "python");
            var pathsObj = EnsureObjectNode(root, "paths");
            var openaiObj = EnsureObjectNode(root, "openai");
            var intentObj = EnsureObjectNode(root, "intent");
            var envObj = EnsureObjectNode(root, "env");
            var projectRoot = ResolveProjectRootPath();

            string value;
            if (TryReadOptionalString(requestObj, "asr_python", out value))
            {
                SetOrRemoveString(
                    pythonObj,
                    "asr",
                    NormalizePathOrCommandForConfig(value, projectRoot, allowCommandName: true));
            }
            if (TryReadOptionalString(requestObj, "tts_python", out value))
            {
                SetOrRemoveString(
                    pythonObj,
                    "tts",
                    NormalizePathOrCommandForConfig(value, projectRoot, allowCommandName: true));
            }
            if (TryReadOptionalString(requestObj, "intent_manifest_path", out value))
            {
                SetOrRemoveString(
                    pathsObj,
                    "intent_manifest",
                    NormalizePathOrCommandForConfig(value, projectRoot, allowCommandName: false));
            }
            if (TryReadOptionalString(requestObj, "game_manifest_path", out value))
            {
                SetOrRemoveString(
                    pathsObj,
                    "game_manifest",
                    NormalizePathOrCommandForConfig(value, projectRoot, allowCommandName: false));
            }
            if (TryReadOptionalString(requestObj, "openai_api_key", out value))
            {
                SetOrRemoveString(openaiObj, "api_key", value);
            }
            if (TryReadOptionalString(requestObj, "openai_transcribe_model", out value))
            {
                SetOrRemoveString(openaiObj, "transcribe_model", value);
            }
            if (TryReadOptionalString(requestObj, "openai_base_url", out value))
            {
                SetOrRemoveString(openaiObj, "base_url", value);
            }
            if (TryReadOptionalString(requestObj, "openai_transcribe_prompt", out value))
            {
                SetOrRemoveString(openaiObj, "transcribe_prompt", value);
            }
            if (TryReadOptionalString(requestObj, "ollama_model", out value))
            {
                SetOrRemoveString(envObj, "OLLAMA_MODEL", value);
            }
            if (TryReadOptionalString(requestObj, "launch_triggers", out value))
            {
                SetOrRemoveStringList(intentObj, "launch_triggers", ParsePhraseList(value));
            }
            if (TryReadOptionalString(requestObj, "exit_keywords", out value))
            {
                SetOrRemoveStringList(intentObj, "exit_keywords", ParsePhraseList(value));
            }

            // Clean up legacy flat keys when intent rules are stored in nested intent object.
            root.Remove("launch_triggers");
            root.Remove("exit_keywords");

            try
            {
                var parent = Path.GetDirectoryName(configPath);
                if (!string.IsNullOrWhiteSpace(parent))
                {
                    Directory.CreateDirectory(parent);
                }
                File.WriteAllText(configPath, root.ToString(2), Encoding.UTF8);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 500, "error", $"failed to save launcher config: {ex.Message}").ConfigureAwait(false);
                return;
            }

            await WriteRuntimeConfigStatusAsync(
                    context.Response,
                    "saved. restart scripts/start_local_services.py to apply service runtime changes.")
                .ConfigureAwait(false);
        }

        private async Task WriteRuntimeConfigStatusAsync(HttpListenerResponse response, string message)
        {
            var configPath = ResolveLauncherConfigPath();
            var defaultConfigPath = ResolveLauncherDefaultConfigPath();
            var load = LoadMergedLauncherConfigRoot(configPath, defaultConfigPath);
            if (!load.Success)
            {
                await WriteJsonAsync(response, 500, "error", load.Error).ConfigureAwait(false);
                return;
            }

            var root = load.Root;
            var pythonObj = EnsureObjectNode(root, "python");
            var pathsObj = EnsureObjectNode(root, "paths");
            var openaiObj = EnsureObjectNode(root, "openai");
            var intentObj = EnsureObjectNode(root, "intent");
            var envObj = EnsureObjectNode(root, "env");
            var projectRoot = ResolveProjectRootPath();
            var launchTriggers = ReadStringList(intentObj, "launch_triggers");
            if (launchTriggers.Count == 0)
            {
                launchTriggers = ReadStringList(root, "launch_triggers");
            }
            if (launchTriggers.Count == 0)
            {
                launchTriggers = GetDefaultLaunchTriggers();
            }
            var exitKeywords = ReadStringList(intentObj, "exit_keywords");
            if (exitKeywords.Count == 0)
            {
                exitKeywords = ReadStringList(root, "exit_keywords");
            }
            if (exitKeywords.Count == 0)
            {
                exitKeywords = GetDefaultExitKeywords();
            }

            var openaiApiKey = (openaiObj["api_key"]?.Value ?? string.Empty).Trim();
            var payload = new JSONObject();
            payload["status"] = "ok";
            payload["message"] = message;
            payload["path"] = configPath;
            payload["default_path"] = defaultConfigPath;
            payload["asr_python"] = NormalizePathOrCommandForConfig(
                (pythonObj["asr"]?.Value ?? string.Empty).Trim(),
                projectRoot,
                allowCommandName: true);
            payload["tts_python"] = NormalizePathOrCommandForConfig(
                (pythonObj["tts"]?.Value ?? string.Empty).Trim(),
                projectRoot,
                allowCommandName: true);
            payload["intent_manifest_path"] = NormalizePathOrCommandForConfig(
                (pathsObj["intent_manifest"]?.Value ?? string.Empty).Trim(),
                projectRoot,
                allowCommandName: false);
            payload["game_manifest_path"] = NormalizePathOrCommandForConfig(
                (pathsObj["game_manifest"]?.Value ?? string.Empty).Trim(),
                projectRoot,
                allowCommandName: false);
            payload["openai_api_key"] = openaiApiKey;
            payload["openai_api_key_set"] = !string.IsNullOrWhiteSpace(openaiApiKey);
            payload["openai_transcribe_model"] = (openaiObj["transcribe_model"]?.Value ?? string.Empty).Trim();
            payload["openai_base_url"] = (openaiObj["base_url"]?.Value ?? string.Empty).Trim();
            payload["openai_transcribe_prompt"] = (openaiObj["transcribe_prompt"]?.Value ?? string.Empty).Trim();
            var ollamaModel = (envObj["OLLAMA_MODEL"]?.Value ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(ollamaModel))
            {
                ollamaModel = (Environment.GetEnvironmentVariable("OLLAMA_MODEL") ?? string.Empty).Trim();
            }
            if (string.IsNullOrWhiteSpace(ollamaModel))
            {
                ollamaModel = "gemma3:4b";
            }
            payload["ollama_model"] = ollamaModel;
            payload["launch_triggers"] = string.Join(", ", launchTriggers);
            payload["exit_keywords"] = string.Join(", ", exitKeywords);
            payload["effective_game_manifest_path"] = ResolveGameManifestPath();
            await WriteRawJsonAsync(response, 200, payload.ToString()).ConfigureAwait(false);
        }

        private async Task HandleRuntimePrereqAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method != "GET")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "GET,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            var payload = new JSONObject();
            payload["status"] = "ok";

            var configuredModel = ResolveConfiguredOllamaModel();
            var ollamaBase = ResolveOllamaBaseUrl();
            var ollamaExe = ResolveOllamaExecutablePath();

            var piperExe = (Environment.GetEnvironmentVariable("PIPER_EXECUTABLE") ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(piperExe))
            {
                piperExe = ResolveBundledPiperExecutablePath();
            }
            piperExe = ResolveAbsolutePathCandidate(piperExe);

            var piperModel = (Environment.GetEnvironmentVariable("PIPER_MODEL_PATH") ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(piperModel))
            {
                piperModel = ResolveBundledPiperModelPath();
            }
            piperModel = ResolveAbsolutePathCandidate(piperModel);

            var piperConfig = ResolveAbsolutePathCandidate((Environment.GetEnvironmentVariable("PIPER_CONFIG_PATH") ?? string.Empty).Trim());
            var piperExeReady = !string.IsNullOrWhiteSpace(piperExe) && File.Exists(piperExe);
            var piperModelReady = !string.IsNullOrWhiteSpace(piperModel) && File.Exists(piperModel);
            var piperReady = piperExeReady && piperModelReady;

            var ollamaProbe = await ProbeOllamaAsync(ollamaBase, configuredModel).ConfigureAwait(false);

            payload["piper_ready"] = piperReady;
            payload["piper_executable_path"] = piperExe;
            payload["piper_model_path"] = piperModel;
            payload["piper_config_path"] = piperConfig;
            payload["piper_executable_exists"] = piperExeReady;
            payload["piper_model_exists"] = piperModelReady;
            payload["ollama_base_url"] = ollamaBase;
            payload["ollama_executable_path"] = ollamaExe;
            payload["ollama_installed"] = !string.IsNullOrWhiteSpace(ollamaExe);
            payload["ollama_running"] = ollamaProbe.Reachable;
            payload["ollama_model"] = configuredModel;
            payload["ollama_model_available"] = ollamaProbe.ModelAvailable;
            payload["ollama_error"] = ollamaProbe.Error;
            payload["ollama_download_url"] = "https://ollama.com/download/windows";
            payload["ollama_install_command"] = "winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements";
            payload["ollama_pull_command"] = "ollama pull " + configuredModel;
            payload["needs_piper_setup"] = !piperReady;
            payload["needs_ollama_setup"] = !ollamaProbe.Reachable || !ollamaProbe.ModelAvailable;

            await WriteRawJsonAsync(context.Response, 200, payload.ToString()).ConfigureAwait(false);
        }

        private async Task HandleRuntimeOllamaAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method != "POST")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "POST,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            var request = ParseJsonBody<RuntimeActionRequest>(context.Request);
            var action = (request.action ?? string.Empty).Trim().ToLowerInvariant();
            var model = NormalizeOllamaModelName(request.model);
            if (string.IsNullOrWhiteSpace(model))
            {
                model = ResolveConfiguredOllamaModel();
            }

            string error;
            switch (action)
            {
                case "open_download":
                    if (!TryOpenUrl("https://ollama.com/download/windows", out error))
                    {
                        await WriteJsonAsync(context.Response, 500, "error", $"failed to open browser: {error}").ConfigureAwait(false);
                        return;
                    }
                    await WriteJsonAsync(context.Response, 200, "ok", "opened Ollama download page").ConfigureAwait(false);
                    return;

                case "install":
                    if (!TryStartPowerShellDetached(
                        "winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements",
                        elevate: true,
                        hidden: false,
                        out error))
                    {
                        await WriteJsonAsync(context.Response, 500, "error", $"failed to start Ollama install: {error}").ConfigureAwait(false);
                        return;
                    }
                    await WriteJsonAsync(
                        context.Response,
                        200,
                        "ok",
                        "started Ollama install in elevated PowerShell. Approve UAC prompt and wait for completion.")
                        .ConfigureAwait(false);
                    return;

                case "pull_model":
                    if (string.IsNullOrWhiteSpace(model))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "invalid model name").ConfigureAwait(false);
                        return;
                    }

                    var escapedModel = model.Replace("'", "''");
                    if (!TryStartPowerShellDetached(
                        "ollama pull '" + escapedModel + "'",
                        elevate: false,
                        hidden: false,
                        out error))
                    {
                        await WriteJsonAsync(context.Response, 500, "error", $"failed to start model pull: {error}").ConfigureAwait(false);
                        return;
                    }
                    await WriteJsonAsync(
                        context.Response,
                        200,
                        "ok",
                        $"started model pull: {model}. wait until the PowerShell window finishes.")
                        .ConfigureAwait(false);
                    return;

                default:
                    await WriteJsonAsync(context.Response, 400, "error", "unknown action").ConfigureAwait(false);
                    return;
            }
        }

        private async Task<(bool Reachable, bool ModelAvailable, string Error)> ProbeOllamaAsync(string baseUrl, string requiredModel)
        {
            var model = NormalizeOllamaModelName(requiredModel);
            if (string.IsNullOrWhiteSpace(model))
            {
                model = "gemma3:4b";
            }

            var url = (baseUrl ?? string.Empty).Trim().TrimEnd('/');
            if (string.IsNullOrWhiteSpace(url))
            {
                url = "http://127.0.0.1:11434";
            }
            url += "/api/tags";

            try
            {
                using (var cts = new CancellationTokenSource(TimeSpan.FromSeconds(2.5)))
                {
                    var response = await SharedHttpClient.GetAsync(url, cts.Token).ConfigureAwait(false);
                    var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                    if (!response.IsSuccessStatusCode)
                    {
                        return (false, false, $"http {(int)response.StatusCode}: {body}");
                    }

                    var parsed = JSONNode.Parse(body);
                    var models = parsed?["models"];
                    if (models == null || !models.IsArray)
                    {
                        return (true, false, "ollama /api/tags returned no models list");
                    }

                    var available = models.AsArray;
                    for (int i = 0; i < available.Count; i++)
                    {
                        var node = available[i];
                        var name = (node?["name"]?.Value ?? string.Empty).Trim();
                        if (OllamaModelNamesMatch(model, name))
                        {
                            return (true, true, string.Empty);
                        }
                    }

                    return (true, false, $"model not found: {model}");
                }
            }
            catch (TaskCanceledException)
            {
                return (false, false, "request timeout");
            }
            catch (Exception ex)
            {
                return (false, false, ex.Message);
            }
        }

        private string ResolveConfiguredOllamaModel()
        {
            var configPath = ResolveLauncherConfigPath();
            var defaultConfigPath = ResolveLauncherDefaultConfigPath();
            var load = LoadMergedLauncherConfigRoot(configPath, defaultConfigPath);
            if (load.Success && load.Root != null)
            {
                var envNode = load.Root["env"];
                if (envNode != null && envNode.IsObject)
                {
                    var model = (envNode["OLLAMA_MODEL"]?.Value ?? string.Empty).Trim();
                    if (!string.IsNullOrWhiteSpace(model))
                    {
                        return model;
                    }
                }
            }

            var fromEnv = (Environment.GetEnvironmentVariable("OLLAMA_MODEL") ?? string.Empty).Trim();
            if (!string.IsNullOrWhiteSpace(fromEnv))
            {
                return fromEnv;
            }

            return "gemma3:4b";
        }

        private static string NormalizeOllamaModelName(string raw)
        {
            var text = (raw ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(text))
            {
                return string.Empty;
            }

            for (int i = 0; i < text.Length; i++)
            {
                var c = text[i];
                var ok = (c >= 'a' && c <= 'z')
                    || (c >= 'A' && c <= 'Z')
                    || (c >= '0' && c <= '9')
                    || c == '-' || c == '_' || c == '.' || c == ':' || c == '/';
                if (!ok)
                {
                    return string.Empty;
                }
            }
            return text;
        }

        private static bool OllamaModelNamesMatch(string expected, string candidate)
        {
            var exp = (expected ?? string.Empty).Trim();
            var got = (candidate ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(exp) || string.IsNullOrWhiteSpace(got))
            {
                return false;
            }
            if (string.Equals(exp, got, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            var expBase = exp.Split(':')[0].Trim();
            var gotBase = got.Split(':')[0].Trim();
            return !string.IsNullOrWhiteSpace(expBase)
                && !string.IsNullOrWhiteSpace(gotBase)
                && string.Equals(expBase, gotBase, StringComparison.OrdinalIgnoreCase);
        }

        private static string ResolveOllamaExecutablePath()
        {
            var envExe = ResolveAbsolutePathCandidate(Environment.GetEnvironmentVariable("OLLAMA_EXE"));
            if (!string.IsNullOrWhiteSpace(envExe) && File.Exists(envExe))
            {
                return envExe;
            }

            var fromPath = ResolveExecutableFromPath("ollama.exe");
            if (!string.IsNullOrWhiteSpace(fromPath))
            {
                return fromPath;
            }

            var candidates = new[]
            {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Ollama", "ollama.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Ollama", "ollama.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), "Ollama", "ollama.exe"),
            };
            for (int i = 0; i < candidates.Length; i++)
            {
                var candidate = ResolveAbsolutePathCandidate(candidates[i]);
                if (!string.IsNullOrWhiteSpace(candidate) && File.Exists(candidate))
                {
                    return candidate;
                }
            }

            return string.Empty;
        }

        private static string ResolveExecutableFromPath(string executableName)
        {
            var exe = (executableName ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(exe))
            {
                return string.Empty;
            }

            var pathEnv = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
            var parts = pathEnv.Split(Path.PathSeparator);
            for (int i = 0; i < parts.Length; i++)
            {
                var dir = (parts[i] ?? string.Empty).Trim().Trim('"');
                if (string.IsNullOrWhiteSpace(dir))
                {
                    continue;
                }
                try
                {
                    var candidate = Path.Combine(dir, exe);
                    if (File.Exists(candidate))
                    {
                        return Path.GetFullPath(candidate);
                    }
                }
                catch
                {
                }
            }

            return string.Empty;
        }

        private static bool TryOpenUrl(string url, out string error)
        {
            error = string.Empty;
            try
            {
                var startInfo = new ProcessStartInfo
                {
                    FileName = url,
                    UseShellExecute = true,
                };
                Process.Start(startInfo);
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
        }

        private static bool TryStartPowerShellDetached(string script, bool elevate, bool hidden, out string error)
        {
            error = string.Empty;
            var source = (script ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(source))
            {
                error = "empty script";
                return false;
            }

            try
            {
                var encoded = Convert.ToBase64String(Encoding.Unicode.GetBytes(source));
                var args = "-NoProfile -ExecutionPolicy Bypass -EncodedCommand " + encoded;
                var startInfo = new ProcessStartInfo
                {
                    FileName = "powershell.exe",
                    Arguments = args,
                    UseShellExecute = elevate || !hidden,
                    WindowStyle = hidden ? ProcessWindowStyle.Hidden : ProcessWindowStyle.Normal,
                    CreateNoWindow = hidden && !elevate
                };
                if (elevate)
                {
                    startInfo.Verb = "runas";
                }

                Process.Start(startInfo);
                return true;
            }
            catch (Exception ex)
            {
                error = ex.Message;
                return false;
            }
        }

        private static string ResolveBundledPiperExecutablePath()
        {
            var root = ResolveProjectRootPath();
            var candidate = ResolveAbsolutePathCandidate(Path.Combine(root, "runtime", "piper", "piper.exe"));
            if (!string.IsNullOrWhiteSpace(candidate) && File.Exists(candidate))
            {
                return candidate;
            }
            return string.Empty;
        }

        private static string ResolveBundledPiperModelPath()
        {
            var root = ResolveProjectRootPath();
            var modelsDir = ResolveAbsolutePathCandidate(Path.Combine(root, "runtime", "piper", "models"));
            if (string.IsNullOrWhiteSpace(modelsDir) || !Directory.Exists(modelsDir))
            {
                return string.Empty;
            }

            var preferred = new[]
            {
                "en_US-lessac-medium.onnx",
                "en_US-amy-medium.onnx",
                "en_US-ryan-high.onnx",
            };
            for (int i = 0; i < preferred.Length; i++)
            {
                var candidate = ResolveAbsolutePathCandidate(Path.Combine(modelsDir, preferred[i]));
                if (!string.IsNullOrWhiteSpace(candidate) && File.Exists(candidate))
                {
                    return candidate;
                }
            }

            try
            {
                var first = Directory.GetFiles(modelsDir, "*.onnx", SearchOption.AllDirectories).FirstOrDefault();
                return ResolveAbsolutePathCandidate(first);
            }
            catch
            {
                return string.Empty;
            }
        }

        private static bool TryReadOptionalString(JSONObject obj, string key, out string value)
        {
            value = string.Empty;
            if (obj == null || string.IsNullOrWhiteSpace(key) || !obj.HasKey(key))
            {
                return false;
            }

            value = (obj[key]?.Value ?? string.Empty).Trim();
            return true;
        }

        private static JSONObject EnsureObjectNode(JSONObject root, string key)
        {
            if (root != null)
            {
                var current = root[key];
                if (current != null && current.IsObject)
                {
                    return current.AsObject;
                }
            }

            var created = new JSONObject();
            if (root != null)
            {
                root[key] = created;
            }
            return created;
        }

        private static void SetOrRemoveString(JSONObject obj, string key, string value)
        {
            if (obj == null || string.IsNullOrWhiteSpace(key))
            {
                return;
            }

            if (string.IsNullOrWhiteSpace(value))
            {
                obj.Remove(key);
                return;
            }

            obj[key] = value.Trim();
        }

        private static void SetOrRemoveStringList(JSONObject obj, string key, List<string> values)
        {
            if (obj == null || string.IsNullOrWhiteSpace(key))
            {
                return;
            }

            if (values == null || values.Count == 0)
            {
                obj.Remove(key);
                return;
            }

            var arr = new JSONArray();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 0; i < values.Count; i++)
            {
                var text = (values[i] ?? string.Empty).Trim();
                if (string.IsNullOrWhiteSpace(text))
                {
                    continue;
                }
                if (seen.Add(text))
                {
                    arr.Add(text);
                }
            }

            if (arr.Count == 0)
            {
                obj.Remove(key);
                return;
            }

            obj[key] = arr;
        }

        private static List<string> ReadStringList(JSONObject obj, string key)
        {
            var values = new List<string>();
            if (obj == null || string.IsNullOrWhiteSpace(key))
            {
                return values;
            }

            var node = obj[key];
            if (node != null && node.IsArray)
            {
                var arr = node.AsArray;
                var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                for (int i = 0; i < arr.Count; i++)
                {
                    var text = (arr[i]?.Value ?? string.Empty).Trim();
                    if (!string.IsNullOrWhiteSpace(text) && seen.Add(text))
                    {
                        values.Add(text);
                    }
                }
            }
            return values;
        }

        private static List<string> ParsePhraseList(string text)
        {
            var values = new List<string>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (string.IsNullOrWhiteSpace(text))
            {
                return values;
            }

            var merged = text
                .Replace("\r\n", "\n")
                .Replace('，', ',')
                .Replace('；', ';')
                .Replace(';', ',')
                .Replace('\n', ',');
            var parts = merged.Split(',');
            for (int i = 0; i < parts.Length; i++)
            {
                var value = (parts[i] ?? string.Empty).Trim();
                if (!string.IsNullOrWhiteSpace(value) && seen.Add(value))
                {
                    values.Add(value);
                }
            }
            return values;
        }

        private static List<string> GetDefaultLaunchTriggers()
        {
            return new List<string>
            {
                "open",
                "start",
                "launch",
                "play",
                "begin",
                "load"
            };
        }

        private static List<string> GetDefaultExitKeywords()
        {
            return new List<string>
            {
                "back home",
                "go home",
                "return home",
                "back",
                "quit",
                "exit",
                "stop",
                "cancel",
                "close",
                "close game"
            };
        }

        private static string ResolveProjectRootPath()
        {
            try
            {
                var appRoot = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
                var appScripts = Path.Combine(appRoot, "scripts");
                if (Directory.Exists(appScripts))
                {
                    return appRoot;
                }

                // Installed layout can be <install>\app\<Unity build> with scripts at <install>\scripts.
                var installRoot = Path.GetFullPath(Path.Combine(appRoot, ".."));
                var installScripts = Path.Combine(installRoot, "scripts");
                if (Directory.Exists(installScripts))
                {
                    return installRoot;
                }

                return appRoot;
            }
            catch
            {
                return Directory.GetCurrentDirectory();
            }
        }

        private static string ResolveUserStateDirectoryPath()
        {
            var fromEnv = ResolveAbsolutePathCandidate(Environment.GetEnvironmentVariable("VOICE_AGENT_STATE_DIR"));
            if (!string.IsNullOrWhiteSpace(fromEnv))
            {
                return fromEnv;
            }

            try
            {
                var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                if (!string.IsNullOrWhiteSpace(localAppData))
                {
                    return Path.GetFullPath(Path.Combine(localAppData, "VoiceAgent"));
                }
            }
            catch
            {
            }

            return Path.GetFullPath(Path.Combine(ResolveProjectRootPath(), "state"));
        }

        private static string ResolveUserLauncherConfigPathDefault()
        {
            return Path.GetFullPath(Path.Combine(ResolveUserStateDirectoryPath(), "local_services.user.json"));
        }

        private static string ResolveUserManifestPathDefault()
        {
            return Path.GetFullPath(Path.Combine(ResolveUserStateDirectoryPath(), "manifest.json"));
        }

        private static string EnsureUserManifestPathDefault()
        {
            var userManifestPath = ResolveUserManifestPathDefault();
            try
            {
                var parent = Path.GetDirectoryName(userManifestPath);
                if (!string.IsNullOrWhiteSpace(parent))
                {
                    Directory.CreateDirectory(parent);
                }

                if (!File.Exists(userManifestPath))
                {
                    var installedManifest = Path.Combine(ResolveProjectRootPath(), "scripts", "intent_service", "manifest.json");
                    if (File.Exists(installedManifest))
                    {
                        File.Copy(installedManifest, userManifestPath, false);
                    }
                    else
                    {
                        File.WriteAllText(userManifestPath, "{\"games\":[]}", Encoding.UTF8);
                    }
                }
            }
            catch
            {
                // Keep returning the target path even if seeding fails.
            }

            return userManifestPath;
        }

        private static bool IsPathWithinRoot(string candidatePath, string rootPath)
        {
            if (string.IsNullOrWhiteSpace(candidatePath) || string.IsNullOrWhiteSpace(rootPath))
            {
                return false;
            }

            try
            {
                var fullPath = Path.GetFullPath(candidatePath).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                var fullRoot = Path.GetFullPath(rootPath).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                if (string.Equals(fullPath, fullRoot, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }

                return fullPath.StartsWith(fullRoot + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
                    || fullPath.StartsWith(fullRoot + Path.AltDirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
            }
            catch
            {
                return false;
            }
        }

        private static bool ShouldPreferUserWritableManifestPath(string manifestPath)
        {
            if (Application.isEditor || string.IsNullOrWhiteSpace(manifestPath))
            {
                return false;
            }

            var installScriptsDir = Path.Combine(ResolveProjectRootPath(), "scripts");
            if (!Directory.Exists(installScriptsDir))
            {
                return false;
            }

            return IsPathWithinRoot(manifestPath, installScriptsDir);
        }

        private static string ResolveExistingFilePathCandidate(string raw, string baseDir = null)
        {
            var candidate = ResolveAbsolutePathCandidate(raw, baseDir);
            if (string.IsNullOrWhiteSpace(candidate))
            {
                return string.Empty;
            }

            try
            {
                if (File.Exists(candidate))
                {
                    return candidate;
                }

                // Legacy buggy value: <install>\app\scripts\... should be <install>\scripts\...
                var normalized = candidate.Replace('/', '\\');
                var marker = "\\app\\scripts\\";
                var idx = normalized.IndexOf(marker, StringComparison.OrdinalIgnoreCase);
                if (idx >= 0)
                {
                    var repaired = normalized.Remove(idx, "\\app".Length);
                    repaired = Path.GetFullPath(repaired);
                    if (File.Exists(repaired))
                    {
                        return repaired;
                    }
                }
            }
            catch
            {
            }

            return string.Empty;
        }

        private static bool LooksLikePathValue(string raw)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return false;
            }

            var text = raw.Trim();
            if (text.StartsWith(".", StringComparison.Ordinal))
            {
                return true;
            }
            return text.IndexOf('\\') >= 0
                || text.IndexOf('/') >= 0
                || text.IndexOf(':') >= 0;
        }

        private static string NormalizePathOrCommandForConfig(string raw, string baseDir, bool allowCommandName)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return string.Empty;
            }

            var trimmed = raw.Trim();
            if (allowCommandName && !LooksLikePathValue(trimmed))
            {
                return trimmed;
            }

            var expanded = Environment.ExpandEnvironmentVariables(trimmed);
            if (expanded.IndexOf('%') >= 0 && trimmed.IndexOf('%') >= 0)
            {
                // Keep unresolved %VAR% literals as-is.
                return trimmed;
            }

            try
            {
                if (Path.IsPathRooted(expanded))
                {
                    return Path.GetFullPath(expanded);
                }
                var root = string.IsNullOrWhiteSpace(baseDir) ? ResolveProjectRootPath() : baseDir;
                return Path.GetFullPath(Path.Combine(root, expanded));
            }
            catch
            {
                return trimmed;
            }
        }

        private static bool IsSinglePercentPlaceholder(string raw)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return false;
            }

            var text = raw.Trim();
            if (!(text.StartsWith("%", StringComparison.Ordinal) && text.EndsWith("%", StringComparison.Ordinal)))
            {
                return false;
            }
            if (text.Length < 3)
            {
                return false;
            }

            return text.IndexOf('%', 1) == text.Length - 1;
        }

        private Dictionary<string, string> LoadLauncherEnvOverrides()
        {
            var result = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var configPath = ResolveLauncherConfigPath();
            var defaultConfigPath = ResolveLauncherDefaultConfigPath();
            var load = LoadMergedLauncherConfigRoot(configPath, defaultConfigPath);
            if (!load.Success || load.Root == null)
            {
                return result;
            }

            var envNode = load.Root["env"];
            if (envNode == null || !envNode.IsObject)
            {
                return result;
            }

            var envObj = envNode.AsObject;
            foreach (var pair in envObj.Linq)
            {
                var key = (pair.Key ?? string.Empty).Trim();
                var value = (pair.Value?.Value ?? string.Empty).Trim();
                if (!string.IsNullOrWhiteSpace(key) && !string.IsNullOrWhiteSpace(value))
                {
                    result[key] = value;
                }
            }
            return result;
        }

        private string ResolvePathFromConfigOrPlaceholder(
            string raw,
            string baseDir,
            Dictionary<string, string> launcherEnv,
            bool allowCommandName)
        {
            var text = (raw ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(text))
            {
                return string.Empty;
            }

            if (!IsSinglePercentPlaceholder(text))
            {
                return NormalizePathOrCommandForConfig(text, baseDir, allowCommandName);
            }

            var key = text.Substring(1, text.Length - 2).Trim();
            if (string.IsNullOrWhiteSpace(key))
            {
                return string.Empty;
            }

            string resolved;
            if (launcherEnv != null && launcherEnv.TryGetValue(key, out resolved) && !string.IsNullOrWhiteSpace(resolved))
            {
                return NormalizePathOrCommandForConfig(resolved, baseDir, allowCommandName);
            }

            resolved = Environment.GetEnvironmentVariable(key);
            if (!string.IsNullOrWhiteSpace(resolved))
            {
                return NormalizePathOrCommandForConfig(resolved, baseDir, allowCommandName);
            }

            var expanded = Environment.ExpandEnvironmentVariables(text);
            if (!string.Equals(expanded, text, StringComparison.Ordinal) && !string.IsNullOrWhiteSpace(expanded))
            {
                return NormalizePathOrCommandForConfig(expanded, baseDir, allowCommandName);
            }

            // Unresolved placeholder -> require explicit absolute path in UI.
            return string.Empty;
        }

        private string ResolveLauncherConfigPath()
        {
            var fromEnv = ResolveAbsolutePathCandidate(Environment.GetEnvironmentVariable("VOICE_AGENT_LAUNCHER_CONFIG"));
            if (!string.IsNullOrWhiteSpace(fromEnv))
            {
                return fromEnv;
            }

            if (!Application.isEditor)
            {
                return ResolveUserLauncherConfigPathDefault();
            }

            var cwdDefault = ResolveAbsolutePathCandidate(Path.Combine("scripts", "local_services.user.json"));
            if (!string.IsNullOrWhiteSpace(cwdDefault))
            {
                return cwdDefault;
            }

            return Path.GetFullPath(Path.Combine(Application.dataPath, "..", "scripts", "local_services.user.json"));
        }

        private string ResolveLauncherDefaultConfigPath()
        {
            var fromEnv = ResolveAbsolutePathCandidate(Environment.GetEnvironmentVariable("VOICE_AGENT_DEFAULT_CONFIG"));
            if (!string.IsNullOrWhiteSpace(fromEnv))
            {
                return fromEnv;
            }

            var cwdDefault = ResolveAbsolutePathCandidate(Path.Combine("scripts", "local_services.default.json"));
            if (!string.IsNullOrWhiteSpace(cwdDefault))
            {
                return cwdDefault;
            }

            return Path.GetFullPath(Path.Combine(Application.dataPath, "..", "scripts", "local_services.default.json"));
        }

        private static string ResolveAbsolutePathCandidate(string raw, string baseDir = null)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return string.Empty;
            }

            try
            {
                var expanded = Environment.ExpandEnvironmentVariables(raw.Trim());
                if (Path.IsPathRooted(expanded))
                {
                    return Path.GetFullPath(expanded);
                }
                var root = string.IsNullOrWhiteSpace(baseDir) ? ResolveProjectRootPath() : baseDir;
                return Path.GetFullPath(Path.Combine(root, expanded));
            }
            catch
            {
                return string.Empty;
            }
        }

        private static (bool Success, JSONObject Root, string Error) LoadLauncherConfigRoot(string path)
        {
            try
            {
                JSONObject rootObj = null;
                if (File.Exists(path))
                {
                    var raw = File.ReadAllText(path, Encoding.UTF8);
                    if (!string.IsNullOrWhiteSpace(raw))
                    {
                        var parsed = JSONNode.Parse(raw);
                        if (parsed != null && parsed.IsObject)
                        {
                            rootObj = parsed.AsObject;
                        }
                    }
                }

                if (rootObj == null)
                {
                    rootObj = new JSONObject();
                }

                if (rootObj["python"] == null || !rootObj["python"].IsObject)
                {
                    rootObj["python"] = new JSONObject();
                }
                if (rootObj["paths"] == null || !rootObj["paths"].IsObject)
                {
                    rootObj["paths"] = new JSONObject();
                }
                if (rootObj["openai"] == null || !rootObj["openai"].IsObject)
                {
                    rootObj["openai"] = new JSONObject();
                }
                if (rootObj["intent"] == null || !rootObj["intent"].IsObject)
                {
                    rootObj["intent"] = new JSONObject();
                }
                if (rootObj["env"] == null || !rootObj["env"].IsObject)
                {
                    rootObj["env"] = new JSONObject();
                }

                return (true, rootObj, string.Empty);
            }
            catch (Exception ex)
            {
                return (false, null, $"failed to load launcher config: {ex.Message}");
            }
        }

        private static JSONNode CloneJsonNode(JSONNode node)
        {
            if (node == null)
            {
                return null;
            }

            try
            {
                return JSONNode.Parse(node.ToString());
            }
            catch
            {
                return node;
            }
        }

        private static void MergeJsonObjectInto(JSONObject target, JSONObject source)
        {
            if (target == null || source == null)
            {
                return;
            }

            foreach (var pair in source.Linq)
            {
                var key = pair.Key;
                var value = pair.Value;
                if (string.IsNullOrWhiteSpace(key) || value == null)
                {
                    continue;
                }

                if (value.IsObject)
                {
                    var current = target[key];
                    JSONObject targetChild;
                    if (current != null && current.IsObject)
                    {
                        targetChild = current.AsObject;
                    }
                    else
                    {
                        targetChild = new JSONObject();
                        target[key] = targetChild;
                    }

                    MergeJsonObjectInto(targetChild, value.AsObject);
                    continue;
                }

                target[key] = CloneJsonNode(value);
            }
        }

        private static (bool Success, JSONObject Root, string Error) LoadMergedLauncherConfigRoot(string userPath, string defaultPath)
        {
            var baseLoad = LoadLauncherConfigRoot(defaultPath);
            if (!baseLoad.Success)
            {
                return baseLoad;
            }

            var userLoad = LoadLauncherConfigRoot(userPath);
            if (!userLoad.Success)
            {
                return userLoad;
            }

            var merged = new JSONObject();
            MergeJsonObjectInto(merged, baseLoad.Root);
            MergeJsonObjectInto(merged, userLoad.Root);

            if (merged["python"] == null || !merged["python"].IsObject)
            {
                merged["python"] = new JSONObject();
            }
            if (merged["paths"] == null || !merged["paths"].IsObject)
            {
                merged["paths"] = new JSONObject();
            }
            if (merged["openai"] == null || !merged["openai"].IsObject)
            {
                merged["openai"] = new JSONObject();
            }
            if (merged["intent"] == null || !merged["intent"].IsObject)
            {
                merged["intent"] = new JSONObject();
            }
            if (merged["env"] == null || !merged["env"].IsObject)
            {
                merged["env"] = new JSONObject();
            }

            return (true, merged, string.Empty);
        }

        private string ResolveGameManifestPath()
        {
            var userManifestPath = string.Empty;
            var configPath = ResolveLauncherConfigPath();
            var defaultConfigPath = ResolveLauncherDefaultConfigPath();
            var config = LoadMergedLauncherConfigRoot(configPath, defaultConfigPath);
            if (config.Success && config.Root != null)
            {
                var pathsNode = config.Root["paths"];
                if (pathsNode != null && pathsNode.IsObject)
                {
                    var paths = pathsNode.AsObject;
                    var gameFromConfig = ResolveExistingFilePathCandidate((paths["game_manifest"]?.Value ?? string.Empty).Trim());
                    if (!string.IsNullOrWhiteSpace(gameFromConfig))
                    {
                        if (ShouldPreferUserWritableManifestPath(gameFromConfig))
                        {
                            if (string.IsNullOrWhiteSpace(userManifestPath))
                            {
                                userManifestPath = EnsureUserManifestPathDefault();
                            }
                            return userManifestPath;
                        }
                        return gameFromConfig;
                    }
                    var intentFromConfig = ResolveExistingFilePathCandidate((paths["intent_manifest"]?.Value ?? string.Empty).Trim());
                    if (!string.IsNullOrWhiteSpace(intentFromConfig))
                    {
                        if (ShouldPreferUserWritableManifestPath(intentFromConfig))
                        {
                            if (string.IsNullOrWhiteSpace(userManifestPath))
                            {
                                userManifestPath = EnsureUserManifestPathDefault();
                            }
                            return userManifestPath;
                        }
                        return intentFromConfig;
                    }
                }
            }

            var primary = ResolveExistingFilePathCandidate(Environment.GetEnvironmentVariable("GAME_LAUNCHER_MANIFEST_PATH"));
            if (!string.IsNullOrWhiteSpace(primary))
            {
                if (ShouldPreferUserWritableManifestPath(primary))
                {
                    if (string.IsNullOrWhiteSpace(userManifestPath))
                    {
                        userManifestPath = EnsureUserManifestPathDefault();
                    }
                    return userManifestPath;
                }
                return primary;
            }

            var secondary = ResolveExistingFilePathCandidate(Environment.GetEnvironmentVariable("INTENT_MANIFEST_PATH"));
            if (!string.IsNullOrWhiteSpace(secondary))
            {
                if (ShouldPreferUserWritableManifestPath(secondary))
                {
                    if (string.IsNullOrWhiteSpace(userManifestPath))
                    {
                        userManifestPath = EnsureUserManifestPathDefault();
                    }
                    return userManifestPath;
                }
                return secondary;
            }

            if (!Application.isEditor)
            {
                if (string.IsNullOrWhiteSpace(userManifestPath))
                {
                    userManifestPath = EnsureUserManifestPathDefault();
                }
                return userManifestPath;
            }

            var cwdDefault = ResolveExistingFilePathCandidate(Path.Combine("scripts", "intent_service", "manifest.json"));
            if (!string.IsNullOrWhiteSpace(cwdDefault))
            {
                return cwdDefault;
            }

            return Path.GetFullPath(Path.Combine(ResolveProjectRootPath(), "scripts", "intent_service", "manifest.json"));
        }

        private static (bool Success, JSONObject Root, string Error) LoadGameManifestRoot(string path)
        {
            try
            {
                JSONObject rootObj = null;
                if (File.Exists(path))
                {
                    var raw = File.ReadAllText(path, Encoding.UTF8);
                    if (!string.IsNullOrWhiteSpace(raw))
                    {
                        var parsed = JSONNode.Parse(raw);
                        if (parsed != null && parsed.IsObject)
                        {
                            rootObj = parsed.AsObject;
                        }
                    }
                }

                if (rootObj == null)
                {
                    rootObj = new JSONObject();
                }

                if (rootObj["games"] == null || !rootObj["games"].IsArray)
                {
                    rootObj["games"] = new JSONArray();
                }

                return (true, rootObj, string.Empty);
            }
            catch (Exception ex)
            {
                return (false, null, $"failed to load manifest: {ex.Message}");
            }
        }

        private async Task HandleAsrAsync(HttpListenerContext context)
        {
            var method = (context.Request.HttpMethod ?? string.Empty).Trim().ToUpperInvariant();
            if (method == "GET")
            {
                await WriteAsrStatusAsync(context.Response, "asr status").ConfigureAwait(false);
                return;
            }

            if (method != "POST")
            {
                context.Response.StatusCode = 405;
                context.Response.Headers["Allow"] = "GET,POST,OPTIONS";
                await WriteJsonAsync(context.Response, 405, "error", "method not allowed").ConfigureAwait(false);
                return;
            }

            var request = ParseJsonBody<AsrRequest>(context.Request);
            var action = (request.action ?? string.Empty).Trim().ToLowerInvariant();
            if (string.IsNullOrWhiteSpace(action) || action == "status")
            {
                await WriteAsrStatusAsync(context.Response, "asr status").ConfigureAwait(false);
                return;
            }

            switch (action)
            {
                case "set_mode":
                case "mode":
                {
                    var modeRaw = string.IsNullOrWhiteSpace(request.mode) ? request.value : request.mode;
                    var normalizedMode = NormalizeAsrMode(modeRaw);
                    if (string.IsNullOrWhiteSpace(normalizedMode))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "mode must be offline or api").ConfigureAwait(false);
                        return;
                    }

                    var setResult = await SetAsrModeAsync(normalizedMode).ConfigureAwait(false);
                    if (!setResult.Success)
                    {
                        await WriteJsonAsync(context.Response, setResult.StatusCode, "error", setResult.Error).ConfigureAwait(false);
                        return;
                    }

                    await WriteAsrStatusAsync(context.Response, $"asr mode set to {normalizedMode}").ConfigureAwait(false);
                    return;
                }
                case "start_listening":
                case "resume_listening":
                {
                    var ok = await SetAgentListeningAsync(true).ConfigureAwait(false);
                    if (!ok)
                    {
                        await WriteJsonAsync(context.Response, 503, "error", "VoiceGameLauncher not assigned").ConfigureAwait(false);
                        return;
                    }
                    await WriteAsrStatusAsync(context.Response, "agent listening started").ConfigureAwait(false);
                    return;
                }
                case "pause_listening":
                case "stop_listening":
                {
                    var ok = await SetAgentListeningAsync(false).ConfigureAwait(false);
                    if (!ok)
                    {
                        await WriteJsonAsync(context.Response, 503, "error", "VoiceGameLauncher not assigned").ConfigureAwait(false);
                        return;
                    }
                    await WriteAsrStatusAsync(context.Response, "agent listening paused").ConfigureAwait(false);
                    return;
                }
                case "set_listening":
                case "listening":
                {
                    var target = request.listening;
                    if (!TryParseBool(request.value, out target))
                    {
                        target = request.listening;
                    }

                    var ok = await SetAgentListeningAsync(target).ConfigureAwait(false);
                    if (!ok)
                    {
                        await WriteJsonAsync(context.Response, 503, "error", "VoiceGameLauncher not assigned").ConfigureAwait(false);
                        return;
                    }

                    await WriteAsrStatusAsync(
                        context.Response,
                        target ? "agent listening started" : "agent listening paused").ConfigureAwait(false);
                    return;
                }
                default:
                    await WriteJsonAsync(context.Response, 400, "error", "unknown asr action").ConfigureAwait(false);
                    return;
            }
        }

        private async Task WriteAsrStatusAsync(HttpListenerResponse response, string message)
        {
            var config = await LoadAsrConfigAsync().ConfigureAwait(false);
            if (!config.Success)
            {
                await WriteJsonAsync(response, config.StatusCode, "error", config.Error).ConfigureAwait(false);
                return;
            }

            var listening = await GetAgentListeningAsync().ConfigureAwait(false);
            var modes = config.Config.available_modes;
            if (modes == null || modes.Length == 0)
            {
                modes = new[] { "offline", "api" };
            }

            var payload = new StringBuilder(256);
            payload.Append("{\"status\":\"ok\",\"message\":\"")
                .Append(EscapeJson(message))
                .Append("\",\"mode\":\"")
                .Append(EscapeJson(config.Config.mode))
                .Append("\",\"listening\":")
                .Append(listening ? "true" : "false")
                .Append(",\"openai_configured\":")
                .Append(config.Config.openai_configured ? "true" : "false")
                .Append(",\"openai_model\":\"")
                .Append(EscapeJson(config.Config.openai_model ?? string.Empty))
                .Append("\",\"available_modes\":[");

            for (var i = 0; i < modes.Length; i++)
            {
                if (i > 0)
                {
                    payload.Append(',');
                }
                payload.Append('\"').Append(EscapeJson(modes[i] ?? string.Empty)).Append('\"');
            }

            payload.Append("]}");
            await WriteRawJsonAsync(response, 200, payload.ToString()).ConfigureAwait(false);
        }

        private string ResolveAsrServiceBaseUrl()
        {
            return ResolveLlmServiceBaseUrl();
        }

        private static string NormalizeAsrMode(string mode)
        {
            var normalized = (mode ?? string.Empty).Trim().ToLowerInvariant();
            switch (normalized)
            {
                case "offline":
                case "local":
                case "whisper":
                    return "offline";
                case "api":
                case "openai":
                case "online":
                    return "api";
                default:
                    return string.Empty;
            }
        }

        private static bool TryParseBool(string raw, out bool value)
        {
            value = false;
            if (string.IsNullOrWhiteSpace(raw))
            {
                return false;
            }

            var normalized = raw.Trim().ToLowerInvariant();
            if (normalized == "1" || normalized == "true" || normalized == "on" || normalized == "yes")
            {
                value = true;
                return true;
            }
            if (normalized == "0" || normalized == "false" || normalized == "off" || normalized == "no")
            {
                value = false;
                return true;
            }
            return false;
        }

        private string ParseAsrErrorMessage(string raw)
        {
            if (string.IsNullOrWhiteSpace(raw))
            {
                return "asr service returned empty response";
            }

            try
            {
                var parsed = JsonUtility.FromJson<AsrErrorResponse>(raw);
                if (!string.IsNullOrWhiteSpace(parsed.detail))
                {
                    return parsed.detail.Trim();
                }
                if (!string.IsNullOrWhiteSpace(parsed.message))
                {
                    return parsed.message.Trim();
                }
            }
            catch (Exception)
            {
            }

            return raw.Trim();
        }

        private async Task<(bool Success, int StatusCode, AsrConfigResponse Config, string Error)> LoadAsrConfigAsync()
        {
            var empty = new AsrConfigResponse
            {
                mode = "offline",
                available_modes = new[] { "offline", "api" },
                openai_model = string.Empty
            };

            try
            {
                var url = ResolveAsrServiceBaseUrl() + "/transcribe/config";
                var response = await SharedHttpClient.GetAsync(url).ConfigureAwait(false);
                var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                if (!response.IsSuccessStatusCode)
                {
                    return (false, (int)response.StatusCode, empty, ParseAsrErrorMessage(body));
                }

                var parsed = JsonUtility.FromJson<AsrConfigResponse>(body);
                if (string.IsNullOrWhiteSpace(parsed.mode))
                {
                    parsed.mode = "offline";
                }
                if (parsed.available_modes == null || parsed.available_modes.Length == 0)
                {
                    parsed.available_modes = new[] { "offline", "api" };
                }
                parsed.mode = NormalizeAsrMode(parsed.mode);
                if (string.IsNullOrWhiteSpace(parsed.mode))
                {
                    parsed.mode = "offline";
                }
                return (true, 200, parsed, string.Empty);
            }
            catch (Exception ex)
            {
                return (false, 502, empty, $"failed to load asr config: {ex.Message}");
            }
        }

        private async Task<(bool Success, int StatusCode, string Error)> SetAsrModeAsync(string mode)
        {
            try
            {
                var payload = "{\"mode\":\"" + EscapeJson(mode) + "\"}";
                using (var content = new StringContent(payload, Encoding.UTF8, "application/json"))
                {
                    var response = await SharedHttpClient.PostAsync(ResolveAsrServiceBaseUrl() + "/transcribe/config", content).ConfigureAwait(false);
                    var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                    if (!response.IsSuccessStatusCode)
                    {
                        return (false, (int)response.StatusCode, ParseAsrErrorMessage(body));
                    }
                }

                return (true, 200, string.Empty);
            }
            catch (Exception ex)
            {
                return (false, 502, $"failed to set asr mode: {ex.Message}");
            }
        }

        private async Task<bool> SetAgentListeningAsync(bool listening)
        {
            if (voiceLauncher == null)
            {
                return false;
            }

            try
            {
                return await RunOnMainThreadAsync(() => voiceLauncher.SetAgentListeningForTester(listening)).ConfigureAwait(false);
            }
            catch (Exception)
            {
                return false;
            }
        }

        private async Task<bool> GetAgentListeningAsync()
        {
            if (voiceLauncher == null)
            {
                return false;
            }

            try
            {
                return await RunOnMainThreadAsync(() => voiceLauncher.IsAgentListeningForTester()).ConfigureAwait(false);
            }
            catch (Exception)
            {
                return false;
            }
        }

        private bool IsCameraClientActive()
        {
            var lastTicks = Interlocked.Read(ref _lastCameraClientRequestUtcTicks);
            if (lastTicks <= 0)
            {
                return false;
            }

            var age = (DateTime.UtcNow.Ticks - lastTicks) / (double)TimeSpan.TicksPerSecond;
            return age <= Mathf.Max(0.25f, cameraClientActiveWindowSeconds);
        }

        private void TouchCameraClientHeartbeat()
        {
            Interlocked.Exchange(ref _lastCameraClientRequestUtcTicks, DateTime.UtcNow.Ticks);
        }

        private async Task HandleCameraPingAsync(HttpListenerContext context)
        {
            TouchCameraClientHeartbeat();
            await WriteJsonAsync(context.Response, 200, "ok", "camera heartbeat").ConfigureAwait(false);
        }

        private async Task HandleCameraJpegAsync(HttpListenerContext context)
        {
            TouchCameraClientHeartbeat();
            if (!enableCameraPreview)
            {
                await WriteJsonAsync(context.Response, 503, "error", "camera preview disabled").ConfigureAwait(false);
                return;
            }

            // Give Unity one short window to produce a fresh frame instead of failing immediately.
            var jpeg = await TryGetLatestCameraJpegWithWaitAsync(900, 40).ConfigureAwait(false);

            if (jpeg == null)
            {
                await WriteJsonAsync(context.Response, 503, "error", "no camera frame").ConfigureAwait(false);
                return;
            }

            try
            {
                context.Response.StatusCode = 200;
                context.Response.ContentType = "image/jpeg";
                context.Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate, proxy-revalidate";
                context.Response.ContentLength64 = jpeg.LongLength;
                await context.Response.OutputStream.WriteAsync(jpeg, 0, jpeg.Length).ConfigureAwait(false);
                context.Response.Close();
            }
            catch (System.Exception)
            {
                try { context.Response.Abort(); } catch { }
            }
        }

        private async Task HandleCameraStatusAsync(HttpListenerContext context)
        {
            int texW;
            int texH;
            string texType;
            bool hasRaw;
            bool hasRenderer;
            int bytes = 0;
            int frameCount = 0;
            long frameTicks = 0;
            lock (_cameraLock)
            {
                bytes = _latestJpeg != null ? _latestJpeg.Length : 0;
                texW = _lastExternalTextureWidth;
                texH = _lastExternalTextureHeight;
                texType = _lastExternalTextureType ?? string.Empty;
                hasRaw = _hasExternalRawImageBinding;
                hasRenderer = _hasExternalRendererBinding;
                frameCount = _cameraFrameCount;
                frameTicks = _cameraLastFrameUtcTicks;
            }
            var age = frameTicks <= 0 ? -1f : (float)TimeSpan.FromTicks(DateTime.UtcNow.Ticks - frameTicks).TotalSeconds;
            var lastClientTicks = Interlocked.Read(ref _lastCameraClientRequestUtcTicks);
            var clientAgeSec = lastClientTicks <= 0
                ? -1f
                : (float)TimeSpan.FromTicks(DateTime.UtcNow.Ticks - lastClientTicks).TotalSeconds;
            var payload = new StringBuilder(256)
                .Append("{\"status\":\"ok\"")
                .Append(",\"mode\":\"").Append(useExternalCameraTexture ? "external" : "webcam").Append('"')
                .Append(",\"has_external_raw_image\":").Append(hasRaw ? "true" : "false")
                .Append(",\"has_external_renderer\":").Append(hasRenderer ? "true" : "false")
                .Append(",\"run_in_background\":").Append(_runInBackgroundEnabled ? "true" : "false")
                .Append(",\"client_active\":").Append(IsCameraClientActive() ? "true" : "false")
                .Append(",\"texture_type\":\"").Append(EscapeJson(texType)).Append('"')
                .Append(",\"texture_width\":").Append(texW)
                .Append(",\"texture_height\":").Append(texH)
                .Append(",\"frame_count\":").Append(frameCount)
                .Append(",\"jpeg_bytes\":").Append(bytes)
                .Append(",\"client_age_s\":").Append(clientAgeSec.ToString("0.00", CultureInfo.InvariantCulture))
                .Append(",\"frame_age_s\":").Append(age.ToString("0.00", CultureInfo.InvariantCulture))
                .Append('}')
                .ToString();
            var buffer = Encoding.UTF8.GetBytes(payload);
            context.Response.StatusCode = 200;
            context.Response.ContentType = "application/json";
            context.Response.ContentLength64 = buffer.Length;
            await context.Response.OutputStream.WriteAsync(buffer, 0, buffer.Length).ConfigureAwait(false);
            context.Response.Close();
        }

        private async Task HandleCameraMjpegAsync(HttpListenerContext context)
        {
            TouchCameraClientHeartbeat();
            if (!enableCameraPreview)
            {
                await WriteJsonAsync(context.Response, 503, "error", "camera preview disabled").ConfigureAwait(false);
                return;
            }

            var boundary = "frame";
            try
            {
                context.Response.StatusCode = 200;
                context.Response.SendChunked = true;
                context.Response.ContentType = "multipart/x-mixed-replace; boundary=" + boundary;
                context.Response.Headers["Cache-Control"] = "no-store, no-cache, must-revalidate, proxy-revalidate";
                var stream = context.Response.OutputStream;
                var delay = Mathf.Max(1, Mathf.FloorToInt(1000f / Mathf.Max(1, cameraFps)));
                var newline = Encoding.ASCII.GetBytes("\r\n");

                while (listener != null && context.Response.OutputStream != null)
                {
                    TouchCameraClientHeartbeat();
                    byte[] jpeg = null;
                    lock (_cameraLock)
                    {
                        if (_latestJpeg != null && _latestJpeg.Length > 0)
                        {
                            jpeg = _latestJpeg;
                        }
                    }

                    if (jpeg != null)
                    {
                        var header = Encoding.ASCII.GetBytes($"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {jpeg.Length}\r\n\r\n");
                        await stream.WriteAsync(header, 0, header.Length).ConfigureAwait(false);
                        await stream.WriteAsync(jpeg, 0, jpeg.Length).ConfigureAwait(false);
                        await stream.WriteAsync(newline, 0, newline.Length).ConfigureAwait(false);
                        await stream.FlushAsync().ConfigureAwait(false);
                    }

                    await Task.Delay(delay).ConfigureAwait(false);
                }
            }
            catch (Exception)
            {
                try { context.Response.OutputStream?.Close(); } catch { }
                try { context.Response.Close(); } catch { }
            }
        }

        private void PostToMainThread(Action action)
        {
            if (action == null) return;
            var ctx = mainThreadContext;
            if (ctx != null && SynchronizationContext.Current != ctx)
            {
                ctx.Post(_ => action(), null);
            }
            else
            {
                action();
            }
        }

        private Task<T> RunOnMainThreadAsync<T>(Func<T> action)
        {
            if (action == null)
            {
                throw new ArgumentNullException(nameof(action));
            }

            var tcs = new TaskCompletionSource<T>();
            PostToMainThread(() =>
            {
                try
                {
                    tcs.TrySetResult(action());
                }
                catch (Exception ex)
                {
                    tcs.TrySetException(ex);
                }
            });
            return tcs.Task;
        }

        private async Task HandleVoiceOptionsAsync(HttpListenerContext context)
        {
            var list = EnumerateVoiceOptions().ToArray();
            var models = EnumerateTtsModelOptions().ToArray();
            var builder = new StringBuilder();
            builder.Append("{\"voices\":[");
            for (int i = 0; i < list.Length; i++)
            {
                if (i > 0)
                {
                    builder.Append(',');
                }
                builder.Append('\"').Append(EscapeJson(list[i])).Append('\"');
            }
            builder.Append("],\"current\":\"").Append(EscapeJson(activeVoiceCode ?? DetermineInitialVoiceCode()));
            builder.Append("\",\"models\":[");
            for (int i = 0; i < models.Length; i++)
            {
                if (i > 0)
                {
                    builder.Append(',');
                }
                builder.Append('\"').Append(EscapeJson(models[i])).Append('\"');
            }
            builder.Append("],\"modelCurrent\":\"").Append(EscapeJson(activeTtsModel ?? DetermineInitialTtsModel())).Append("\"}");
            var payload = Encoding.UTF8.GetBytes(builder.ToString());
            context.Response.StatusCode = 200;
            context.Response.ContentType = "application/json";
            context.Response.ContentLength64 = payload.Length;
            await context.Response.OutputStream.WriteAsync(payload, 0, payload.Length).ConfigureAwait(false);
            context.Response.Close();
        }

        private async Task HandleQwenOptionsAsync(HttpListenerContext context)
        {
            var list = EnumerateQwenSpeakers().ToArray();
            var current = string.IsNullOrWhiteSpace(activeQwenSpeaker) ? DetermineInitialQwenSpeaker() : activeQwenSpeaker;
            var sb = new StringBuilder(256);
            sb.Append("{\"speakers\":[");
            for (int i = 0; i < list.Length; i++)
            {
                if (i > 0) sb.Append(',');
                sb.Append('\"').Append(EscapeJson(list[i])).Append('\"');
            }
            sb.Append("],\"current\":\"").Append(EscapeJson(current)).Append("\"}");
            var payload = Encoding.UTF8.GetBytes(sb.ToString());
            context.Response.StatusCode = 200;
            context.Response.ContentType = "application/json";
            context.Response.ContentLength64 = payload.Length;
            await context.Response.OutputStream.WriteAsync(payload, 0, payload.Length).ConfigureAwait(false);
            context.Response.Close();
        }

        private async Task HandleLogsAsync(HttpListenerContext context)
        {
            var entries = ConversationLog.GetSnapshot();
            var builder = new StringBuilder(entries.Length * 128);
            builder.Append("{\"entries\":[");
            for (int i = 0; i < entries.Length; i++)
            {
                if (i > 0)
                {
                    builder.Append(',');
                }

                var entry = entries[i];
                builder.Append("{\"timestamp\":\"")
                    .Append(entry.TimestampUtc.ToString("o", CultureInfo.InvariantCulture))
                    .Append("\",\"role\":\"")
                    .Append(entry.Role.ToString().ToLowerInvariant())
                    .Append("\",\"speaker\":\"")
                    .Append(EscapeJson(entry.Speaker ?? string.Empty))
                    .Append("\",\"message\":\"")
                    .Append(EscapeJson(entry.Message ?? string.Empty))
                    .Append("\"");

                if (!string.IsNullOrEmpty(entry.Metadata))
                {
                    builder.Append(",\"metadata\":\"")
                        .Append(EscapeJson(entry.Metadata))
                        .Append("\"");
                }

                if (!string.IsNullOrEmpty(entry.Source))
                {
                    builder.Append(",\"source\":\"")
                        .Append(EscapeJson(entry.Source))
                        .Append("\"");
                }

                builder.Append('}');
            }

            builder.Append("]}");
            var payload = Encoding.UTF8.GetBytes(builder.ToString());
            context.Response.StatusCode = 200;
            context.Response.ContentType = "application/json";
            context.Response.ContentLength64 = payload.Length;
            await context.Response.OutputStream.WriteAsync(payload, 0, payload.Length).ConfigureAwait(false);
            context.Response.Close();
        }

        private string DetermineInitialVoiceCode()
        {
            if (!string.IsNullOrWhiteSpace(defaultVoiceCode))
            {
                return defaultVoiceCode.Trim();
            }

            var candidate = availableVoices?.FirstOrDefault(v => !string.IsNullOrWhiteSpace(v));
            return string.IsNullOrWhiteSpace(candidate) ? "en_US" : candidate.Trim();
        }

        private string DetermineInitialTtsModel()
        {
            // Prefer scanned models from filesystem
            try
            {
                var firstScanned = EnumerateTtsModelOptions().FirstOrDefault(m => !string.IsNullOrWhiteSpace(m));
                if (!string.IsNullOrWhiteSpace(firstScanned))
                {
                    return firstScanned;
                }
            }
            catch (Exception) { }

            if (!string.IsNullOrWhiteSpace(defaultTtsModel))
            {
                return defaultTtsModel.Trim();
            }

            var candidate = availableTtsModels?.FirstOrDefault(m => !string.IsNullOrWhiteSpace(m));
            return string.IsNullOrWhiteSpace(candidate) ? "piper-zh" : candidate.Trim();
        }

        private IEnumerable<string> EnumerateVoiceOptions()
        {
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (!string.IsNullOrWhiteSpace(defaultVoiceCode))
            {
                var trimmed = defaultVoiceCode.Trim();
                if (seen.Add(trimmed))
                {
                    yield return trimmed;
                }
            }

            if (availableVoices != null)
            {
                foreach (var voice in availableVoices)
                {
                    var trimmed = (voice ?? string.Empty).Trim();
                    if (string.IsNullOrEmpty(trimmed))
                    {
                        continue;
                    }
                    if (seen.Add(trimmed))
                    {
                        yield return trimmed;
                    }
                }
            }

            if (seen.Count == 0)
            {
                yield return "en_US";
            }
        }

        private string DetermineInitialQwenSpeaker()
        {
            if (!string.IsNullOrWhiteSpace(defaultQwenSpeaker))
            {
                return defaultQwenSpeaker.Trim();
            }
            var candidate = qwenSpeakers?.FirstOrDefault(s => !string.IsNullOrWhiteSpace(s));
            return string.IsNullOrWhiteSpace(candidate) ? "Ryan" : candidate.Trim();
        }

        private IEnumerable<string> EnumerateQwenSpeakers()
        {
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            if (!string.IsNullOrWhiteSpace(defaultQwenSpeaker))
            {
                var trimmed = defaultQwenSpeaker.Trim();
                if (seen.Add(trimmed))
                {
                    yield return trimmed;
                }
            }
            if (qwenSpeakers != null)
            {
                foreach (var s in qwenSpeakers)
                {
                    var trimmed = (s ?? string.Empty).Trim();
                    if (string.IsNullOrWhiteSpace(trimmed)) continue;
                    if (seen.Add(trimmed)) yield return trimmed;
                }
            }
            if (seen.Count == 0)
            {
                yield return "Ryan";
            }
        }

        private IEnumerable<string> EnumerateTtsModelOptions()
        {
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

            // Scan models from filesystem (gather first, then yield outside try/catch)
			List<string> scanned = null;
			try
			{
				var dir = (modelsDirectory ?? string.Empty).Trim();
				if (!string.IsNullOrEmpty(dir) && System.IO.Directory.Exists(dir))
				{
					var option = scanModelsRecursively ? System.IO.SearchOption.AllDirectories : System.IO.SearchOption.TopDirectoryOnly;
					scanned = System.IO.Directory.EnumerateFiles(dir, "*.onnx", option).ToList();
				}
			}
			catch (Exception ex)
			{
				Debug.LogWarning($"[UserTestPanel] Model scan failed: {ex.Message}");
			}

			if (scanned != null)
			{
				foreach (var path in scanned)
				{
					string full = path;
					if (string.IsNullOrWhiteSpace(full))
					{
						continue;
					}
					// Normalize to absolute path
					try { full = System.IO.Path.GetFullPath(full); } catch {}
					if (seen.Add(full))
					{
						yield return full;
					}
				}
			}

            if (seen.Count == 0)
            {
                yield return "piper-zh";
            }
        }

        private static string BuildSpeakPayload(string text, string voice, string model, float speed, float volume)
        {
            var sb = new StringBuilder();
            sb.Append("{\"text\":\"").Append(EscapeJson(text)).Append("\"");
            if (!string.IsNullOrEmpty(voice))
            {
                sb.Append(",\"voice\":\"").Append(EscapeJson(voice)).Append("\"");
            }
            if (!string.IsNullOrEmpty(model))
            {
                sb.Append(",\"model\":\"").Append(EscapeJson(model)).Append("\"");
            }
            sb.Append(",\"speed\":").Append(speed.ToString(CultureInfo.InvariantCulture));
            sb.Append(",\"volume\":").Append(volume.ToString(CultureInfo.InvariantCulture));
            sb.Append('}');
            return sb.ToString();
        }

        private static T ParseJsonBody<T>(HttpListenerRequest request) where T : new()
        {
            try
            {
                using (var reader = new StreamReader(request.InputStream, request.ContentEncoding ?? Encoding.UTF8))
                {
                    var json = reader.ReadToEnd();
                    if (string.IsNullOrWhiteSpace(json))
                    {
                        return new T();
                    }

                    return JsonUtility.FromJson<T>(json);
                }
            }
            catch (Exception)
            {
                return new T();
            }
        }

        private static async Task WriteJsonAsync(HttpListenerResponse response, int statusCode, string status, string message)
        {
            var payload = $"{{\"status\":\"{status}\",\"message\":\"{EscapeJson(message)}\"}}";
            var buffer = Encoding.UTF8.GetBytes(payload);
            response.StatusCode = statusCode;
            response.ContentType = "application/json";
            response.ContentLength64 = buffer.Length;
            await response.OutputStream.WriteAsync(buffer, 0, buffer.Length).ConfigureAwait(false);
            response.Close();
        }

        private static string EscapeJson(string value)
        {
            if (string.IsNullOrEmpty(value))
            {
                return string.Empty;
            }

            var sb = new StringBuilder(value.Length + 16);
            for (int i = 0; i < value.Length; i++)
            {
                var c = value[i];
                switch (c)
                {
                    case '\\':
                        sb.Append("\\\\");
                        break;
                    case '"':
                        sb.Append("\\\"");
                        break;
                    case '\n':
                        sb.Append("\\n");
                        break;
                    case '\r':
                        sb.Append("\\r");
                        break;
                    case '\t':
                        sb.Append("\\t");
                        break;
                    case '\b':
                        sb.Append("\\b");
                        break;
                    case '\f':
                        sb.Append("\\f");
                        break;
                    default:
                        if (c < 32)
                        {
                            sb.Append("\\u");
                            sb.Append(((int)c).ToString("x4"));
                        }
                        else
                        {
                            sb.Append(c);
                        }
                        break;
                }
            }

            return sb.ToString();
        }

        private static async Task RespondWithHtmlAsync(HttpListenerResponse response)
        {
            var html = BuildPanelHtml();
            var buffer = Encoding.UTF8.GetBytes(html);
            response.StatusCode = 200;
            response.ContentType = "text/html; charset=utf-8";
            response.ContentLength64 = buffer.Length;
            await response.OutputStream.WriteAsync(buffer, 0, buffer.Length).ConfigureAwait(false);
            response.Close();
        }

        private static async Task RespondWithSdkHtmlAsync(HttpListenerResponse response)
        {
            var html = BuildSdkPanelHtml();
            var buffer = Encoding.UTF8.GetBytes(html);
            response.StatusCode = 200;
            response.ContentType = "text/html; charset=utf-8";
            response.ContentLength64 = buffer.Length;
            await response.OutputStream.WriteAsync(buffer, 0, buffer.Length).ConfigureAwait(false);
            response.Close();
        }

        private async Task RespondWithTelemetryHtmlAsync(HttpListenerResponse response)
        {
            var html = BuildTelemetryLandingHtml();
            var buffer = Encoding.UTF8.GetBytes(html);
            response.StatusCode = 200;
            response.ContentType = "text/html; charset=utf-8";
            response.ContentLength64 = buffer.Length;
            await response.OutputStream.WriteAsync(buffer, 0, buffer.Length).ConfigureAwait(false);
            response.Close();
        }

        private static async Task RespondWithGameConfigHtmlAsync(HttpListenerResponse response)
        {
            var html = BuildGameConfigHtml();
            var buffer = Encoding.UTF8.GetBytes(html);
            response.StatusCode = 200;
            response.ContentType = "text/html; charset=utf-8";
            response.ContentLength64 = buffer.Length;
            await response.OutputStream.WriteAsync(buffer, 0, buffer.Length).ConfigureAwait(false);
            response.Close();
        }

        private static async Task RespondWithRuntimeConfigHtmlAsync(HttpListenerResponse response)
        {
            var html = BuildRuntimeConfigHtml();
            var buffer = Encoding.UTF8.GetBytes(html);
            response.StatusCode = 200;
            response.ContentType = "text/html; charset=utf-8";
            response.ContentLength64 = buffer.Length;
            await response.OutputStream.WriteAsync(buffer, 0, buffer.Length).ConfigureAwait(false);
            response.Close();
        }

        private static async Task RespondWithSetupWizardHtmlAsync(HttpListenerResponse response)
        {
            var html = BuildSetupWizardHtml();
            var buffer = Encoding.UTF8.GetBytes(html);
            response.StatusCode = 200;
            response.ContentType = "text/html; charset=utf-8";
            response.ContentLength64 = buffer.Length;
            await response.OutputStream.WriteAsync(buffer, 0, buffer.Length).ConfigureAwait(false);
            response.Close();
        }

        private string BuildTelemetryLandingHtml()
        {
            var dashboardBase = string.IsNullOrWhiteSpace(telemetryDashboardUrl)
                ? "http://127.0.0.1:8101/dashboard"
                : telemetryDashboardUrl.Trim();
            var dashboardBaseJs = EscapeJson(dashboardBase);

            var sb = new StringBuilder(4096);
            sb.AppendLine(@"<!DOCTYPE html>");
            sb.AppendLine(@"<html lang=""en"">");
            sb.AppendLine(@"<head>");
            sb.AppendLine(@"<meta charset=""utf-8"">");
            sb.AppendLine(@"<meta name=""viewport"" content=""width=device-width, initial-scale=1"">");
            sb.AppendLine(@"<title>Telemetry Dashboard</title>");
            sb.AppendLine(@"<style>");
            sb.AppendLine(@"body { margin:0; font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0f1117; color:#f4f4f4; }");
            sb.AppendLine(@"header { padding:14px 18px; border-bottom:1px solid rgba(255,255,255,0.1); display:flex; align-items:center; gap:10px; flex-wrap:wrap; }");
            sb.AppendLine(@"input, select, button { border-radius:8px; border:1px solid rgba(255,255,255,0.2); background:#131c2d; color:#f4f4f4; height:34px; padding:0 10px; }");
            sb.AppendLine(@"button { cursor:pointer; background:#2a5ca6; border-color:#4278ca; }");
            sb.AppendLine(@"a { color:#93c5fd; text-decoration:none; border-bottom:1px dashed #93c5fd; }");
            sb.AppendLine(@"#frame { width:100%; height:calc(100vh - 74px); border:0; background:#0b1220; }");
            sb.AppendLine(@"</style>");
            sb.AppendLine(@"</head>");
            sb.AppendLine(@"<body>");
            sb.AppendLine(@"<header>");
            sb.AppendLine(@"<a href=""/"">Back to Panel</a>");
            sb.AppendLine(@"<label>User</label><input id=""userId"" value=""demo_user"">");
            sb.AppendLine(@"<label>Days</label>");
            sb.AppendLine(@"<select id=""days""><option>7</option><option selected>14</option><option>21</option><option>30</option></select>");
            sb.AppendLine(@"<button id=""openBtn"">Open Dashboard</button>");
            sb.AppendLine(@"</header>");
            sb.AppendLine(@"<iframe id=""frame"" title=""telemetry dashboard""></iframe>");
            sb.AppendLine(@"<script>");
            sb.AppendLine($@"const dashboardBase = ""{dashboardBaseJs}"";");
            sb.AppendLine(@"const frame = document.getElementById('frame');");
            sb.AppendLine(@"const userInput = document.getElementById('userId');");
            sb.AppendLine(@"const daysInput = document.getElementById('days');");
            sb.AppendLine(@"function buildUrl(){");
            sb.AppendLine(@"  const user = encodeURIComponent((userInput.value || 'demo_user').trim() || 'demo_user');");
            sb.AppendLine(@"  const days = encodeURIComponent(daysInput.value || '14');");
            sb.AppendLine(@"  return `${dashboardBase}?user_id=${user}&days=${days}`;");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function openDash(){ frame.src = buildUrl(); }");
            sb.AppendLine(@"document.getElementById('openBtn').addEventListener('click', openDash);");
            sb.AppendLine(@"openDash();");
            sb.AppendLine(@"</script>");
            sb.AppendLine(@"</body>");
            sb.AppendLine(@"</html>");
            return sb.ToString();
        }

        private static string BuildGameConfigHtml()
        {
            var sb = new StringBuilder(8192);
            sb.AppendLine(@"<!DOCTYPE html>");
            sb.AppendLine(@"<html lang=""en"">");
            sb.AppendLine(@"<head>");
            sb.AppendLine(@"<meta charset=""utf-8"">");
            sb.AppendLine(@"<meta name=""viewport"" content=""width=device-width, initial-scale=1"">");
            sb.AppendLine(@"<title>Game Config</title>");
            sb.AppendLine(@"<style>");
            sb.AppendLine(@"body{margin:0;padding:1rem 1.1rem;background:#0f1117;color:#f5f7ff;font-family:'Segoe UI',sans-serif;}");
            sb.AppendLine(@"h1{margin:.2rem 0 .6rem 0;font-size:1.35rem;}");
            sb.AppendLine(@"a{color:#93c5fd;text-decoration:none;border-bottom:1px dashed #93c5fd;}");
            sb.AppendLine(@"small{opacity:.75;}");
            sb.AppendLine(@".card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.11);border-radius:12px;padding:.9rem;margin-top:.7rem;}");
            sb.AppendLine(@".toolbar{display:flex;gap:.55rem;flex-wrap:wrap;align-items:center;margin:.6rem 0;}");
            sb.AppendLine(@"button{cursor:pointer;border:none;border-radius:8px;padding:.55rem .9rem;background:#3b82f6;color:#fff;font-weight:600;}");
            sb.AppendLine(@"button.secondary{background:#334155;}");
            sb.AppendLine(@"button.warn{background:#dc2626;}");
            sb.AppendLine(@"input{width:100%;box-sizing:border-box;border:1px solid rgba(255,255,255,.2);border-radius:8px;background:rgba(255,255,255,.07);color:#f5f7ff;padding:.44rem .58rem;}");
            sb.AppendLine(@".pathRow{display:flex;gap:.4rem;align-items:center;}");
            sb.AppendLine(@".pathRow input{flex:1;}");
            sb.AppendLine(@".tinyBtn{padding:.42rem .55rem;font-size:.78rem;white-space:nowrap;background:#334155;}");
            sb.AppendLine(@"table{width:100%;border-collapse:collapse;font-size:.9rem;}");
            sb.AppendLine(@"th,td{border-bottom:1px solid rgba(255,255,255,.1);padding:.42rem .35rem;vertical-align:top;}");
            sb.AppendLine(@"th{text-align:left;font-size:.78rem;opacity:.85;text-transform:uppercase;letter-spacing:.04em;}");
            sb.AppendLine(@"#status{margin-top:.6rem;opacity:.9;}");
            sb.AppendLine(@"#manifestPath{font-family:Consolas,monospace;font-size:.8rem;opacity:.8;word-break:break-all;}");
            sb.AppendLine(@"@media(max-width:900px){table,thead,tbody,tr,th,td{display:block;} th{display:none;} tr{border:1px solid rgba(255,255,255,.12);border-radius:10px;margin:.6rem 0;padding:.45rem;} td{border:none;padding:.28rem 0;} td::before{display:block;font-size:.72rem;opacity:.7;margin-bottom:.15rem;}}");
            sb.AppendLine(@"</style>");
            sb.AppendLine(@"</head>");
            sb.AppendLine(@"<body>");
            sb.AppendLine(@"<h1>Game Config</h1>");
            sb.AppendLine(@"<p><a href=""/index.html"">Back to User Panel</a></p>");
            sb.AppendLine(@"<div class=""card"">");
            sb.AppendLine(@"<small>配置会写入本机 manifest：游戏名、语音关键词和可执行路径；可用 Browse 在宿主机选文件，路径会自动规范为绝对路径。</small>");
            sb.AppendLine(@"<div id=""manifestPath""></div>");
            sb.AppendLine(@"<div class=""toolbar"">");
            sb.AppendLine(@"<button onclick=""addRow()"">Add Game</button>");
            sb.AppendLine(@"<button class=""secondary"" onclick=""reloadGames()"">Reload</button>");
            sb.AppendLine(@"<button onclick=""saveGames()"">Save</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<table>");
            sb.AppendLine(@"<thead><tr><th>ID</th><th>Game Name</th><th>Keywords</th><th>Executable Path</th><th>Workdir</th><th>Action</th></tr></thead>");
            sb.AppendLine(@"<tbody id=""rows""></tbody>");
            sb.AppendLine(@"</table>");
            sb.AppendLine(@"<div id=""status"">Ready.</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<script>");
            sb.AppendLine(@"const rowsEl = document.getElementById('rows');");
            sb.AppendLine(@"const statusEl = document.getElementById('status');");
            sb.AppendLine(@"const manifestPathEl = document.getElementById('manifestPath');");
            sb.AppendLine(@"let gameRows = [];");
            sb.AppendLine(@"function esc(v){ return String(v||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/""/g,'&quot;').replace(/'/g,'&#39;'); }");
            sb.AppendLine(@"function splitKeywords(text){");
            sb.AppendLine(@"  return String(text||'').split(',').map(s=>s.trim()).filter(Boolean);");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function render(){");
            sb.AppendLine(@"  if(!rowsEl) return;");
            sb.AppendLine(@"  if(!gameRows.length){ rowsEl.innerHTML = '<tr><td colspan=""6"">No games configured.</td></tr>'; return; }");
            sb.AppendLine(@"  rowsEl.innerHTML = gameRows.map((g,i)=>`");
            sb.AppendLine(@"    <tr>");
            sb.AppendLine(@"      <td data-label=""ID""><input value=""${esc(g.id)}"" oninput=""updateField(${i},'id',this.value)"" placeholder=""disc_golf""></td>");
            sb.AppendLine(@"      <td data-label=""Game Name""><input value=""${esc(g.name)}"" oninput=""updateField(${i},'name',this.value)"" placeholder=""Disc Golf""></td>");
            sb.AppendLine(@"      <td data-label=""Keywords""><input value=""${esc(g.keywords_text)}"" oninput=""updateField(${i},'keywords_text',this.value)"" placeholder=""disc golf,discgolf,frisbee golf""></td>");
            sb.AppendLine(@"      <td data-label=""Executable Path""><div class=""pathRow""><input value=""${esc(g.exec)}"" oninput=""updateField(${i},'exec',this.value)"" placeholder=""D:\\Games\\DiscGolf\\DiscGolf.exe""><button type=""button"" class=""tinyBtn"" onclick=""browseExec(${i})"">Browse</button></div></td>");
            sb.AppendLine(@"      <td data-label=""Workdir""><input value=""${esc(g.workdir)}"" oninput=""updateField(${i},'workdir',this.value)"" placeholder=""D:\\Games\\DiscGolf""></td>");
            sb.AppendLine(@"      <td data-label=""Action""><button class=""warn"" onclick=""removeRow(${i})"">Delete</button></td>");
            sb.AppendLine(@"    </tr>`).join('');");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function updateField(i,key,val){ if(!gameRows[i]) return; gameRows[i][key]=val; }");
            sb.AppendLine(@"function addRow(){ gameRows.push({id:'',name:'',keywords_text:'',exec:'',workdir:''}); render(); }");
            sb.AppendLine(@"function removeRow(i){ gameRows.splice(i,1); render(); }");
            sb.AppendLine(@"async function browseExec(i){");
            sb.AppendLine(@"  if(!gameRows[i]) return;");
            sb.AppendLine(@"  statusEl.textContent = 'Opening file picker on host...';");
            sb.AppendLine(@"  const payload = {");
            sb.AppendLine(@"    title: 'Select Game Executable',");
            sb.AppendLine(@"    filter: 'Executable Files (*.exe)|*.exe|All Files (*.*)|*.*',");
            sb.AppendLine(@"    initial_dir: String(gameRows[i].workdir || ''),");
            sb.AppendLine(@"    initial_filename: String(gameRows[i].exec || '')");
            sb.AppendLine(@"  };");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch('/api/file/pick', {");
            sb.AppendLine(@"      method:'POST',");
            sb.AppendLine(@"      headers:{'Content-Type':'application/json'},");
            sb.AppendLine(@"      body: JSON.stringify(payload)");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    const data = await resp.json();");
            sb.AppendLine(@"    if(!resp.ok || data.status !== 'ok'){ throw new Error((data && data.message) ? data.message : ('HTTP '+resp.status)); }");
            sb.AppendLine(@"    if(data.cancelled){ statusEl.textContent = 'Browse cancelled.'; return; }");
            sb.AppendLine(@"    const path = String(data.path || '').trim();");
            sb.AppendLine(@"    const dir = String(data.directory || '').trim();");
            sb.AppendLine(@"    if(path){ gameRows[i].exec = path; }");
            sb.AppendLine(@"    if(dir){ gameRows[i].workdir = dir; }");
            sb.AppendLine(@"    render();");
            sb.AppendLine(@"    statusEl.textContent = 'Selected: ' + (path || 'no file');");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function reloadGames(){");
            sb.AppendLine(@"  statusEl.textContent = 'Loading...';");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch('/api/game/manifest');");
            sb.AppendLine(@"    const data = await resp.json();");
            sb.AppendLine(@"    if(!resp.ok || data.status !== 'ok'){ throw new Error((data && data.message) ? data.message : ('HTTP '+resp.status)); }");
            sb.AppendLine(@"    const list = Array.isArray(data.games) ? data.games : [];");
            sb.AppendLine(@"    gameRows = list.map(g=>({");
            sb.AppendLine(@"      id: String(g.id||''),");
            sb.AppendLine(@"      name: String(g.name||''),");
            sb.AppendLine(@"      exec: String(g.exec||''),");
            sb.AppendLine(@"      workdir: String(g.workdir||''),");
            sb.AppendLine(@"      keywords_text: Array.isArray(g.keywords) ? g.keywords.join(', ') : ''");
            sb.AppendLine(@"    }));");
            sb.AppendLine(@"    if(manifestPathEl){ manifestPathEl.textContent = 'Manifest: ' + String(data.path||''); }");
            sb.AppendLine(@"    render();");
            sb.AppendLine(@"    const unresolved = Number(data.unresolved_count || 0);");
            sb.AppendLine(@"    statusEl.textContent = unresolved > 0");
            sb.AppendLine(@"      ? ('Loaded with warnings: ' + unresolved + ' unresolved path fields. Please fill absolute paths and save.')");
            sb.AppendLine(@"      : 'Loaded.';");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function saveGames(){");
            sb.AppendLine(@"  const payload = {");
            sb.AppendLine(@"    games: gameRows.map(g=>({");
            sb.AppendLine(@"      id: String(g.id||'').trim(),");
            sb.AppendLine(@"      name: String(g.name||'').trim(),");
            sb.AppendLine(@"      keywords: splitKeywords(g.keywords_text),");
            sb.AppendLine(@"      exec: String(g.exec||'').trim(),");
            sb.AppendLine(@"      workdir: String(g.workdir||'').trim()");
            sb.AppendLine(@"    }))");
            sb.AppendLine(@"  };");
            sb.AppendLine(@"  statusEl.textContent = 'Saving...';");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch('/api/game/manifest', {");
            sb.AppendLine(@"      method:'POST',");
            sb.AppendLine(@"      headers:{'Content-Type':'application/json'},");
            sb.AppendLine(@"      body: JSON.stringify(payload)");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    const data = await resp.json();");
            sb.AppendLine(@"    if(!resp.ok || data.status !== 'ok'){ throw new Error((data && data.message) ? data.message : ('HTTP '+resp.status)); }");
            sb.AppendLine(@"    statusEl.textContent = 'Saved. ' + (data.message || 'Restart intent_service and game_launcher to apply immediately.');");
            sb.AppendLine(@"    await reloadGames();");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"reloadGames();");
            sb.AppendLine(@"</script>");
            sb.AppendLine(@"</body>");
            sb.AppendLine(@"</html>");
            return sb.ToString();
        }

        private static string BuildRuntimeConfigHtml()
        {
            var sb = new StringBuilder(8192);
            sb.AppendLine(@"<!DOCTYPE html>");
            sb.AppendLine(@"<html lang=""en"">");
            sb.AppendLine(@"<head>");
            sb.AppendLine(@"<meta charset=""utf-8"">");
            sb.AppendLine(@"<meta name=""viewport"" content=""width=device-width, initial-scale=1"">");
            sb.AppendLine(@"<title>Runtime Config</title>");
            sb.AppendLine(@"<style>");
            sb.AppendLine(@"body{margin:0;padding:1rem 1.1rem;background:#0f1117;color:#f5f7ff;font-family:'Segoe UI',sans-serif;}");
            sb.AppendLine(@"h1{margin:.2rem 0 .6rem 0;font-size:1.35rem;}");
            sb.AppendLine(@"a{color:#93c5fd;text-decoration:none;border-bottom:1px dashed #93c5fd;}");
            sb.AppendLine(@"small{opacity:.75;}");
            sb.AppendLine(@".card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.11);border-radius:12px;padding:.9rem;margin-top:.7rem;}");
            sb.AppendLine(@".grid{display:grid;grid-template-columns:1fr 1fr;gap:.7rem;}");
            sb.AppendLine(@".field{display:flex;flex-direction:column;gap:.3rem;}");
            sb.AppendLine(@"label{font-size:.85rem;opacity:.9;}");
            sb.AppendLine(@"input{width:100%;box-sizing:border-box;border:1px solid rgba(255,255,255,.2);border-radius:8px;background:rgba(255,255,255,.07);color:#f5f7ff;padding:.44rem .58rem;}");
            sb.AppendLine(@".toolbar{display:flex;gap:.55rem;flex-wrap:wrap;align-items:center;margin-top:.8rem;}");
            sb.AppendLine(@"button{cursor:pointer;border:none;border-radius:8px;padding:.55rem .9rem;background:#3b82f6;color:#fff;font-weight:600;}");
            sb.AppendLine(@"button.secondary{background:#334155;}");
            sb.AppendLine(@"details{margin-top:.8rem;border:1px solid rgba(255,255,255,.16);border-radius:10px;padding:.55rem .7rem;background:rgba(255,255,255,.03);}");
            sb.AppendLine(@"summary{cursor:pointer;font-size:.9rem;opacity:.92;}");
            sb.AppendLine(@"#status{margin-top:.6rem;opacity:.9;}");
            sb.AppendLine(@"#configPath{font-family:Consolas,monospace;font-size:.8rem;opacity:.8;word-break:break-all;}");
            sb.AppendLine(@"@media(max-width:900px){.grid{grid-template-columns:1fr;}}");
            sb.AppendLine(@"</style>");
            sb.AppendLine(@"</head>");
            sb.AppendLine(@"<body>");
            sb.AppendLine(@"<h1>Runtime Config</h1>");
            sb.AppendLine(@"<p><a href=""/index.html"">Back to User Panel</a></p>");
            sb.AppendLine(@"<div class=""card"">");
            sb.AppendLine(@"<small>For most users, only OpenAI, voice command phrases, and game manifest path need changes.</small>");
            sb.AppendLine(@"<div id=""configPath""></div>");
            sb.AppendLine(@"<div class=""grid"">");
            sb.AppendLine(@"<div class=""field""><label>OpenAI API Key</label><input id=""openaiKey"" type=""password"" placeholder=""sk-...""></div>");
            sb.AppendLine(@"<div class=""field""><label>OpenAI Transcribe Model</label><input id=""openaiModel"" placeholder=""gpt-4o-mini-transcribe""></div>");
            sb.AppendLine(@"<div class=""field""><label>OpenAI Base URL (Optional)</label><input id=""openaiBaseUrl"" placeholder=""https://api.openai.com/v1""></div>");
            sb.AppendLine(@"<div class=""field""><label>OpenAI ASR Prompt</label><input id=""openaiPrompt"" placeholder=""Optional. Leave empty to avoid prompt bias/hallucination.""></div>");
            sb.AppendLine(@"<div class=""field""><label>Launch Triggers (comma separated)</label><input id=""launchTriggers"" placeholder=""open, start, launch, play, begin, load""></div>");
            sb.AppendLine(@"<div class=""field""><label>Exit Keywords (comma separated)</label><input id=""exitKeywords"" placeholder=""back home, go home, quit, exit, stop, close game""></div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<details>");
            sb.AppendLine(@"  <summary>Advanced (optional): Manifest paths</summary>");
            sb.AppendLine(@"  <div class=""grid"">");
            sb.AppendLine(@"    <div class=""field""><label>Intent Manifest Path</label><input id=""intentManifest"" placeholder=""Usually leave empty (default manifest)""></div>");
            sb.AppendLine(@"    <div class=""field""><label>Game Manifest Path</label><input id=""gameManifest"" placeholder=""Usually leave empty (same as intent manifest)""></div>");
            sb.AppendLine(@"  </div>");
            sb.AppendLine(@"</details>");
            sb.AppendLine(@"<div class=""toolbar"">");
            sb.AppendLine(@"<button onclick=""saveConfig()"">Save</button>");
            sb.AppendLine(@"<button class=""secondary"" onclick=""reloadConfig()"">Reload</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div id=""status"">Ready.</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<script>");
            sb.AppendLine(@"const statusEl = document.getElementById('status');");
            sb.AppendLine(@"const configPathEl = document.getElementById('configPath');");
            sb.AppendLine(@"const intentManifestEl = document.getElementById('intentManifest');");
            sb.AppendLine(@"const gameManifestEl = document.getElementById('gameManifest');");
            sb.AppendLine(@"const openaiKeyEl = document.getElementById('openaiKey');");
            sb.AppendLine(@"const openaiModelEl = document.getElementById('openaiModel');");
            sb.AppendLine(@"const openaiBaseUrlEl = document.getElementById('openaiBaseUrl');");
            sb.AppendLine(@"const openaiPromptEl = document.getElementById('openaiPrompt');");
            sb.AppendLine(@"const launchTriggersEl = document.getElementById('launchTriggers');");
            sb.AppendLine(@"const exitKeywordsEl = document.getElementById('exitKeywords');");
            sb.AppendLine(@"function setText(el, v){ if(el){ el.value = String(v || ''); } }");
            sb.AppendLine(@"function getText(el){ return el ? String(el.value || '').trim() : ''; }");
            sb.AppendLine(@"async function reloadConfig(){");
            sb.AppendLine(@"  statusEl.textContent = 'Loading...';");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch('/api/runtime/config');");
            sb.AppendLine(@"    const data = await resp.json();");
            sb.AppendLine(@"    if(!resp.ok || data.status !== 'ok'){ throw new Error((data && data.message) ? data.message : ('HTTP ' + resp.status)); }");
            sb.AppendLine(@"    setText(intentManifestEl, data.intent_manifest_path);");
            sb.AppendLine(@"    setText(gameManifestEl, data.game_manifest_path);");
            sb.AppendLine(@"    setText(openaiKeyEl, data.openai_api_key);");
            sb.AppendLine(@"    setText(openaiModelEl, data.openai_transcribe_model);");
            sb.AppendLine(@"    setText(openaiBaseUrlEl, data.openai_base_url);");
            sb.AppendLine(@"    setText(openaiPromptEl, data.openai_transcribe_prompt);");
            sb.AppendLine(@"    setText(launchTriggersEl, data.launch_triggers);");
            sb.AppendLine(@"    setText(exitKeywordsEl, data.exit_keywords);");
            sb.AppendLine(@"    if(configPathEl){ configPathEl.textContent = 'Config: ' + String(data.path || ''); }");
            sb.AppendLine(@"    statusEl.textContent = 'Loaded.';");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function saveConfig(){");
            sb.AppendLine(@"  statusEl.textContent = 'Saving...';");
            sb.AppendLine(@"  const payload = {");
            sb.AppendLine(@"    intent_manifest_path: getText(intentManifestEl),");
            sb.AppendLine(@"    game_manifest_path: getText(gameManifestEl),");
            sb.AppendLine(@"    openai_api_key: getText(openaiKeyEl),");
            sb.AppendLine(@"    openai_transcribe_model: getText(openaiModelEl),");
            sb.AppendLine(@"    openai_base_url: getText(openaiBaseUrlEl),");
            sb.AppendLine(@"    openai_transcribe_prompt: getText(openaiPromptEl),");
            sb.AppendLine(@"    launch_triggers: getText(launchTriggersEl),");
            sb.AppendLine(@"    exit_keywords: getText(exitKeywordsEl)");
            sb.AppendLine(@"  };");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch('/api/runtime/config', {");
            sb.AppendLine(@"      method:'POST',");
            sb.AppendLine(@"      headers:{'Content-Type':'application/json'},");
            sb.AppendLine(@"      body: JSON.stringify(payload)");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    const data = await resp.json();");
            sb.AppendLine(@"    if(!resp.ok || data.status !== 'ok'){ throw new Error((data && data.message) ? data.message : ('HTTP ' + resp.status)); }");
            sb.AppendLine(@"    statusEl.textContent = data.message || 'Saved. Restart local services to apply.';");
            sb.AppendLine(@"    await reloadConfig();");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"reloadConfig();");
            sb.AppendLine(@"</script>");
            sb.AppendLine(@"</body>");
            sb.AppendLine(@"</html>");
            return sb.ToString();
        }

        private static string BuildSetupWizardHtml()
        {
            var sb = new StringBuilder(12288);
            sb.AppendLine(@"<!DOCTYPE html>");
            sb.AppendLine(@"<html lang=""en"">");
            sb.AppendLine(@"<head>");
            sb.AppendLine(@"<meta charset=""utf-8"">");
            sb.AppendLine(@"<meta name=""viewport"" content=""width=device-width, initial-scale=1"">");
            sb.AppendLine(@"<title>First-Run Wizard</title>");
            sb.AppendLine(@"<style>");
            sb.AppendLine(@"body{margin:0;padding:1rem 1.1rem;background:#0f1117;color:#f5f7ff;font-family:'Segoe UI',sans-serif;}");
            sb.AppendLine(@"h1{margin:.2rem 0 .6rem 0;font-size:1.35rem;}");
            sb.AppendLine(@"h2{margin:.1rem 0 .6rem 0;font-size:1rem;letter-spacing:.03em;text-transform:uppercase;opacity:.9;}");
            sb.AppendLine(@"a{color:#93c5fd;text-decoration:none;border-bottom:1px dashed #93c5fd;}");
            sb.AppendLine(@"small{opacity:.75;}");
            sb.AppendLine(@".card{background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.11);border-radius:12px;padding:.9rem;margin-top:.7rem;}");
            sb.AppendLine(@".grid{display:grid;grid-template-columns:1fr 1fr;gap:.7rem;}");
            sb.AppendLine(@".field{display:flex;flex-direction:column;gap:.3rem;}");
            sb.AppendLine(@"label{font-size:.85rem;opacity:.9;}");
            sb.AppendLine(@"input,select{width:100%;box-sizing:border-box;border:1px solid rgba(255,255,255,.2);border-radius:8px;background:rgba(255,255,255,.07);color:#f5f7ff;padding:.5rem .58rem;}");
            sb.AppendLine(@"input[type=checkbox]{width:auto;}");
            sb.AppendLine(@".toggle{display:flex;align-items:center;gap:.5rem;margin-top:.2rem;}");
            sb.AppendLine(@".toolbar{display:flex;gap:.55rem;flex-wrap:wrap;align-items:center;margin-top:.8rem;}");
            sb.AppendLine(@"button{cursor:pointer;border:none;border-radius:8px;padding:.55rem .9rem;background:#3b82f6;color:#fff;font-weight:600;}");
            sb.AppendLine(@"button.secondary{background:#334155;}");
            sb.AppendLine(@"#status{margin-top:.75rem;opacity:.92;white-space:pre-wrap;}");
            sb.AppendLine(@"@media(max-width:900px){.grid{grid-template-columns:1fr;}}");
            sb.AppendLine(@"</style>");
            sb.AppendLine(@"</head>");
            sb.AppendLine(@"<body>");
            sb.AppendLine(@"<h1>First-Run Wizard</h1>");
            sb.AppendLine(@"<p><a href=""/index.html"">Back to User Panel</a></p>");
            sb.AppendLine(@"<div class=""card"">");
            sb.AppendLine(@"<h2>Step 0: Prerequisites (Piper + Ollama)</h2>");
            sb.AppendLine(@"<div class=""grid"">");
            sb.AppendLine(@"<div class=""field""><label>Piper Status</label><input id=""piperStatus"" readonly></div>");
            sb.AppendLine(@"<div class=""field""><label>Piper Model Path</label><input id=""piperModelPath"" readonly></div>");
            sb.AppendLine(@"<div class=""field""><label>Ollama Status</label><input id=""ollamaStatus"" readonly></div>");
            sb.AppendLine(@"<div class=""field""><label>Ollama Model</label><input id=""ollamaModel"" placeholder=""gemma3:4b""></div>");
            sb.AppendLine(@"<div class=""field""><label>Model Downloaded</label><input id=""ollamaModelReady"" readonly></div>");
            sb.AppendLine(@"<div class=""field""><label>Ollama Hint</label><input id=""ollamaHint"" readonly></div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""toolbar"">");
            sb.AppendLine(@"<button class=""secondary"" onclick=""refreshPrereq()"">Refresh Prerequisites</button>");
            sb.AppendLine(@"<button class=""secondary"" onclick=""installOllama()"">Install Ollama (winget)</button>");
            sb.AppendLine(@"<button class=""secondary"" onclick=""pullOllamaModel()"">Pull Ollama Model</button>");
            sb.AppendLine(@"<button class=""secondary"" onclick=""openOllamaDownload()"">Open Ollama Download</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<small>Piper is bundled when packaging includes runtime/piper. Ollama is guided-install and model pull is separate.</small>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""card"">");
            sb.AppendLine(@"<h2>Step 1: ASR Mode and Listening</h2>");
            sb.AppendLine(@"<div class=""grid"">");
            sb.AppendLine(@"<div class=""field""><label>ASR Mode</label><select id=""asrMode""><option value=""offline"">offline</option><option value=""api"">api (OpenAI)</option></select></div>");
            sb.AppendLine(@"<div class=""field""><label>Agent Listening</label><div class=""toggle""><input id=""listeningOn"" type=""checkbox""><span>Enable microphone listening</span></div></div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""card"">");
            sb.AppendLine(@"<h2>Step 2: OpenAI</h2>");
            sb.AppendLine(@"<div class=""grid"">");
            sb.AppendLine(@"<div class=""field""><label>OpenAI API Key</label><input id=""openaiKey"" type=""password"" placeholder=""sk-...""></div>");
            sb.AppendLine(@"<div class=""field""><label>Transcribe Model</label><input id=""openaiModel"" placeholder=""gpt-4o-mini-transcribe""></div>");
            sb.AppendLine(@"<div class=""field""><label>OpenAI Base URL (Optional)</label><input id=""openaiBaseUrl"" placeholder=""https://api.openai.com/v1""></div>");
            sb.AppendLine(@"<div class=""field""><label>ASR Prompt</label><input id=""openaiPrompt"" placeholder=""Optional. Leave empty to avoid prompt bias/hallucination.""></div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""card"">");
            sb.AppendLine(@"<h2>Step 3: Intent Rules and Manifest</h2>");
            sb.AppendLine(@"<div class=""grid"">");
            sb.AppendLine(@"<div class=""field""><label>Launch Triggers (comma separated)</label><input id=""launchTriggers"" placeholder=""open, start, launch, play, begin, load""></div>");
            sb.AppendLine(@"<div class=""field""><label>Exit Keywords (comma separated)</label><input id=""exitKeywords"" placeholder=""back home, go home, quit, exit, stop, close game""></div>");
            sb.AppendLine(@"<div class=""field""><label>Intent Manifest Path (Optional)</label><input id=""intentManifest"" placeholder=""Usually keep default""></div>");
            sb.AppendLine(@"<div class=""field""><label>Game Manifest Path (Optional)</label><input id=""gameManifest"" placeholder=""Usually same as intent manifest""></div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<small>Advanced game rows (name/keywords/executable path) are in <a href=""/games.html"">Game Config</a>.</small>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""card"">");
            sb.AppendLine(@"<h2>Step 4: Save</h2>");
            sb.AppendLine(@"<div class=""toolbar"">");
            sb.AppendLine(@"<button onclick=""saveAll()"">Save Wizard Config</button>");
            sb.AppendLine(@"<button class=""secondary"" onclick=""reloadAll()"">Reload</button>");
            sb.AppendLine(@"<button class=""secondary"" onclick=""location.href='/games.html'"">Open Game Config</button>");
            sb.AppendLine(@"<button class=""secondary"" onclick=""location.href='/runtime.html'"">Open Runtime Config</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div id=""status"">Ready.</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<script>");
            sb.AppendLine(@"const statusEl = document.getElementById('status');");
            sb.AppendLine(@"const asrModeEl = document.getElementById('asrMode');");
            sb.AppendLine(@"const listeningEl = document.getElementById('listeningOn');");
            sb.AppendLine(@"const openaiKeyEl = document.getElementById('openaiKey');");
            sb.AppendLine(@"const openaiModelEl = document.getElementById('openaiModel');");
            sb.AppendLine(@"const openaiBaseUrlEl = document.getElementById('openaiBaseUrl');");
            sb.AppendLine(@"const openaiPromptEl = document.getElementById('openaiPrompt');");
            sb.AppendLine(@"const launchTriggersEl = document.getElementById('launchTriggers');");
            sb.AppendLine(@"const exitKeywordsEl = document.getElementById('exitKeywords');");
            sb.AppendLine(@"const intentManifestEl = document.getElementById('intentManifest');");
            sb.AppendLine(@"const gameManifestEl = document.getElementById('gameManifest');");
            sb.AppendLine(@"const piperStatusEl = document.getElementById('piperStatus');");
            sb.AppendLine(@"const piperModelPathEl = document.getElementById('piperModelPath');");
            sb.AppendLine(@"const ollamaStatusEl = document.getElementById('ollamaStatus');");
            sb.AppendLine(@"const ollamaModelEl = document.getElementById('ollamaModel');");
            sb.AppendLine(@"const ollamaModelReadyEl = document.getElementById('ollamaModelReady');");
            sb.AppendLine(@"const ollamaHintEl = document.getElementById('ollamaHint');");
            sb.AppendLine(@"function setText(el, v){ if(el){ el.value = String(v || ''); } }");
            sb.AppendLine(@"function getText(el){ return el ? String(el.value || '').trim() : ''; }");
            sb.AppendLine(@"async function fetchJson(url, opts){");
            sb.AppendLine(@"  const resp = await fetch(url, opts);");
            sb.AppendLine(@"  const data = await resp.json();");
            sb.AppendLine(@"  if(!resp.ok || data.status !== 'ok'){");
            sb.AppendLine(@"    throw new Error((data && data.message) ? data.message : ('HTTP ' + resp.status));");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  return data;");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function refreshPrereq(){");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const data = await fetchJson('/api/runtime/prereq');");
            sb.AppendLine(@"    setText(piperStatusEl, data.piper_ready ? 'ready' : 'missing executable/model');");
            sb.AppendLine(@"    setText(piperModelPathEl, data.piper_model_path || '');");
            sb.AppendLine(@"    if(!getText(ollamaModelEl)){ setText(ollamaModelEl, data.ollama_model || 'gemma3:4b'); }");
            sb.AppendLine(@"    const ollamaState = data.ollama_running ? 'running' : (data.ollama_installed ? 'installed (not running)' : 'not installed');");
            sb.AppendLine(@"    setText(ollamaStatusEl, ollamaState);");
            sb.AppendLine(@"    setText(ollamaModelReadyEl, data.ollama_model_available ? 'yes' : 'no');");
            sb.AppendLine(@"    const hint = data.ollama_error ? String(data.ollama_error) : (data.needs_ollama_setup ? 'install Ollama and pull model' : 'ready');");
            sb.AppendLine(@"    setText(ollamaHintEl, hint);");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    setText(ollamaStatusEl, 'error');");
            sb.AppendLine(@"    setText(ollamaHintEl, String(err));");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function openOllamaDownload(){");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const data = await fetchJson('/api/runtime/ollama', {");
            sb.AppendLine(@"      method:'POST',");
            sb.AppendLine(@"      headers:{'Content-Type':'application/json'},");
            sb.AppendLine(@"      body: JSON.stringify({ action:'open_download' })");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    statusEl.textContent = data.message || 'opened download page';");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function installOllama(){");
            sb.AppendLine(@"  statusEl.textContent = 'Starting Ollama install...';");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const data = await fetchJson('/api/runtime/ollama', {");
            sb.AppendLine(@"      method:'POST',");
            sb.AppendLine(@"      headers:{'Content-Type':'application/json'},");
            sb.AppendLine(@"      body: JSON.stringify({ action:'install' })");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    statusEl.textContent = data.message || 'install started';");
            sb.AppendLine(@"    await refreshPrereq();");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function pullOllamaModel(){");
            sb.AppendLine(@"  const model = getText(ollamaModelEl) || 'gemma3:4b';");
            sb.AppendLine(@"  statusEl.textContent = 'Starting model pull: ' + model + ' ...';");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const data = await fetchJson('/api/runtime/ollama', {");
            sb.AppendLine(@"      method:'POST',");
            sb.AppendLine(@"      headers:{'Content-Type':'application/json'},");
            sb.AppendLine(@"      body: JSON.stringify({ action:'pull_model', model })");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    statusEl.textContent = data.message || ('model pull started: ' + model);");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function reloadAll(){");
            sb.AppendLine(@"  statusEl.textContent = 'Loading...';");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const [asrData, runtimeData, prereqData] = await Promise.all([");
            sb.AppendLine(@"      fetchJson('/api/asr'),");
            sb.AppendLine(@"      fetchJson('/api/runtime/config'),");
            sb.AppendLine(@"      fetchJson('/api/runtime/prereq')");
            sb.AppendLine(@"    ]);");
            sb.AppendLine(@"    setText(asrModeEl, asrData.mode || 'offline');");
            sb.AppendLine(@"    if(listeningEl){ listeningEl.checked = !!asrData.listening; }");
            sb.AppendLine(@"    setText(openaiKeyEl, runtimeData.openai_api_key);");
            sb.AppendLine(@"    setText(openaiModelEl, runtimeData.openai_transcribe_model);");
            sb.AppendLine(@"    setText(openaiBaseUrlEl, runtimeData.openai_base_url);");
            sb.AppendLine(@"    setText(openaiPromptEl, runtimeData.openai_transcribe_prompt);");
            sb.AppendLine(@"    setText(ollamaModelEl, runtimeData.ollama_model || prereqData.ollama_model || 'gemma3:4b');");
            sb.AppendLine(@"    setText(launchTriggersEl, runtimeData.launch_triggers);");
            sb.AppendLine(@"    setText(exitKeywordsEl, runtimeData.exit_keywords);");
            sb.AppendLine(@"    setText(intentManifestEl, runtimeData.intent_manifest_path);");
            sb.AppendLine(@"    setText(gameManifestEl, runtimeData.game_manifest_path);");
            sb.AppendLine(@"    setText(piperStatusEl, prereqData.piper_ready ? 'ready' : 'missing executable/model');");
            sb.AppendLine(@"    setText(piperModelPathEl, prereqData.piper_model_path || '');");
            sb.AppendLine(@"    const ollamaState = prereqData.ollama_running ? 'running' : (prereqData.ollama_installed ? 'installed (not running)' : 'not installed');");
            sb.AppendLine(@"    setText(ollamaStatusEl, ollamaState);");
            sb.AppendLine(@"    setText(ollamaModelReadyEl, prereqData.ollama_model_available ? 'yes' : 'no');");
            sb.AppendLine(@"    const hint = prereqData.ollama_error ? String(prereqData.ollama_error) : (prereqData.needs_ollama_setup ? 'install Ollama and pull model' : 'ready');");
            sb.AppendLine(@"    setText(ollamaHintEl, hint);");
            sb.AppendLine(@"    statusEl.textContent = 'Loaded.';");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function saveAll(){");
            sb.AppendLine(@"  statusEl.textContent = 'Saving...';");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    await fetchJson('/api/runtime/config', {");
            sb.AppendLine(@"      method:'POST',");
            sb.AppendLine(@"      headers:{'Content-Type':'application/json'},");
            sb.AppendLine(@"      body: JSON.stringify({");
            sb.AppendLine(@"        openai_api_key: getText(openaiKeyEl),");
            sb.AppendLine(@"        openai_transcribe_model: getText(openaiModelEl),");
            sb.AppendLine(@"        openai_base_url: getText(openaiBaseUrlEl),");
            sb.AppendLine(@"        openai_transcribe_prompt: getText(openaiPromptEl),");
            sb.AppendLine(@"        ollama_model: getText(ollamaModelEl),");
            sb.AppendLine(@"        launch_triggers: getText(launchTriggersEl),");
            sb.AppendLine(@"        exit_keywords: getText(exitKeywordsEl),");
            sb.AppendLine(@"        intent_manifest_path: getText(intentManifestEl),");
            sb.AppendLine(@"        game_manifest_path: getText(gameManifestEl)");
            sb.AppendLine(@"      })");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    await fetchJson('/api/asr', {");
            sb.AppendLine(@"      method:'POST',");
            sb.AppendLine(@"      headers:{'Content-Type':'application/json'},");
            sb.AppendLine(@"      body: JSON.stringify({ action:'set_mode', mode:getText(asrModeEl) || 'offline' })");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    await fetchJson('/api/asr', {");
            sb.AppendLine(@"      method:'POST',");
            sb.AppendLine(@"      headers:{'Content-Type':'application/json'},");
            sb.AppendLine(@"      body: JSON.stringify({ action:'set_listening', listening:!!(listeningEl && listeningEl.checked) })");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    statusEl.textContent = 'Saved. Restart local services if executable/runtime paths changed.';");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"reloadAll();");
            sb.AppendLine(@"</script>");
            sb.AppendLine(@"</body>");
            sb.AppendLine(@"</html>");
            return sb.ToString();
        }


        private static string BuildPanelHtml()
        {
            var sb = new StringBuilder(8192);
            sb.AppendLine(@"<!DOCTYPE html>");
            sb.AppendLine(@"<html lang=""en"">");
            sb.AppendLine(@"<head>");
            sb.AppendLine(@"<meta charset=""utf-8"">");
            sb.AppendLine(@"<title>Robot User Test Panel</title>");
            sb.AppendLine(@"<meta name=""viewport"" content=""width=device-width, initial-scale=1"">");
            sb.AppendLine(@"<style>");
            sb.AppendLine(@"body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 1.5rem; background: #0f1117; color: #f4f4f4; line-height: 1.45; }");
            sb.AppendLine(@"h1 { font-size: 1.6rem; margin-top: 0; }");
            sb.AppendLine(@"section { margin-bottom: 1.75rem; padding: 1rem; border-radius: 12px; background: rgba(255,255,255,0.05); }");
            sb.AppendLine(@"section h2 { margin-top: 0; font-size: 1.05rem; letter-spacing: 0.02em; text-transform: uppercase; opacity: 0.8; }");
            sb.AppendLine(@"button { cursor: pointer; border: none; border-radius: 8px; padding: 0.65rem 1rem; margin: 0.35rem 0.35rem 0 0; background: #7c5dfa; color: #fff; font-size: 0.95rem; font-weight: 600; box-shadow: 0 4px 20px rgba(0,0,0,0.25); transition: transform 0.1s ease, box-shadow 0.1s ease; }");
            sb.AppendLine(@"button:hover { transform: translateY(-1px); box-shadow: 0 6px 25px rgba(0,0,0,0.35); }");
            sb.AppendLine(@"button:active { transform: translateY(0); box-shadow: 0 2px 12px rgba(0,0,0,0.25); }");
            sb.AppendLine(@".controls { display: flex; flex-wrap: wrap; align-items: center; gap: 0.65rem; }");
            sb.AppendLine(@"label { margin-right: 0.25rem; font-size: 0.85rem; opacity: 0.85; }");
            sb.AppendLine(@"input[type=number], input[type=text], select { min-width: 4rem; padding: 0.4rem 0.6rem; border-radius: 6px; border: none; background: rgba(255,255,255,0.08); color: #f4f4f4; }");
            sb.AppendLine(@"input[type=color] { width: 3rem; height: 2rem; border: none; border-radius: 6px; padding: 0; background: transparent; }");
            sb.AppendLine(@"textarea { width: 100%; min-height: 4rem; padding: 0.7rem 0.85rem; margin-top: 0.4rem; border-radius: 8px; border: none; background: rgba(255,255,255,0.08); color: #f4f4f4; font-size: 0.95rem; resize: vertical; }");
            sb.AppendLine(@"#status { margin-top: 1rem; font-size: 0.95rem; opacity: 0.9; }");
            sb.AppendLine(@".transcript-card { margin-bottom: 1.75rem; padding: 1.25rem; border-radius: 16px; background: linear-gradient(135deg, rgba(124,93,250,0.25), rgba(15,17,23,0.95)); border: 1px solid rgba(255,255,255,0.08); box-shadow: 0 24px 60px rgba(0,0,0,0.45); }");
            sb.AppendLine(@".transcript-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; }");
            sb.AppendLine(@".transcript-header h2 { margin: 0; font-size: 1.2rem; }");
            sb.AppendLine(@".transcript-subtitle { font-size: 0.9rem; opacity: 0.8; margin-top: 0.25rem; }");
            sb.AppendLine(@".ghost-btn { background: transparent; border: 1px solid rgba(255,255,255,0.35); color: #f4f4f4; padding: 0.45rem 1rem; border-radius: 999px; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em; box-shadow: none; }");
            sb.AppendLine(@".ghost-btn:hover { border-color: rgba(255,255,255,0.7); }");
            sb.AppendLine(@".log-list { max-height: 360px; overflow-y: auto; margin-top: 1rem; padding-right: 0.25rem; }");
            sb.AppendLine(@".log-entry { display: flex; gap: 0.75rem; padding: 0.65rem 0; border-bottom: 1px solid rgba(255,255,255,0.07); }");
            sb.AppendLine(@".log-entry:last-child { border-bottom: none; }");
            sb.AppendLine(@".log-icon { width: 42px; height: 42px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 1.2rem; font-weight: 600; box-shadow: 0 8px 24px rgba(0,0,0,0.45); }");
            sb.AppendLine(@".log-body { flex: 1; }");
            sb.AppendLine(@".log-meta { font-size: 0.78rem; letter-spacing: 0.04em; text-transform: uppercase; opacity: 0.7; display: flex; gap: 0.35rem; align-items: center; }");
            sb.AppendLine(@".log-speaker { font-weight: 600; letter-spacing: 0.08em; }");
            sb.AppendLine(@".log-message { margin-top: 0.2rem; font-size: 0.95rem; color: #f8fafc; }");
            sb.AppendLine(@".log-empty { padding: 1.2rem 0; font-size: 0.9rem; opacity: 0.6; text-align: center; }");
            sb.AppendLine(@"</style>");
            sb.AppendLine(@"</head>");
            sb.AppendLine(@"<body>");
            sb.AppendLine(@"<h1>Robot User Test Panel</h1>");
            sb.AppendLine(@"<p>Connect to the same Wi-Fi network as the host running Unity and open this page from any browser.</p>");
            sb.AppendLine(@"<p><a href=""/sdk.html"" style=""color:#93c5fd;text-decoration:none;border-bottom:1px dashed #93c5fd;"">Open SDK Visualizer</a></p>");
            sb.AppendLine(@"<p><a href=""/telemetry.html"" style=""color:#93c5fd;text-decoration:none;border-bottom:1px dashed #93c5fd;"">Open Exercise Dashboard</a></p>");
            sb.AppendLine(@"<p><a href=""/games.html"" style=""color:#93c5fd;text-decoration:none;border-bottom:1px dashed #93c5fd;"">Open Game Config</a></p>");
            sb.AppendLine(@"<p><a href=""/runtime.html"" style=""color:#93c5fd;text-decoration:none;border-bottom:1px dashed #93c5fd;"">Open Runtime Config</a></p>");
            sb.AppendLine(@"<p><a href=""/setup.html"" style=""color:#93c5fd;text-decoration:none;border-bottom:1px dashed #93c5fd;"">Open First-Run Wizard</a></p>");
            sb.AppendLine(@"<div class=""transcript-card"">");
            sb.AppendLine(@"<div class=""transcript-header"">");
            sb.AppendLine(@"<div>");
            sb.AppendLine(@"<h2>Live Transcript</h2>");
            sb.AppendLine(@"<p class=""transcript-subtitle"">Monitor patient speech, wizard overrides, and agent coaching responses.</p>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<button class=""ghost-btn"" onclick=""refreshLog()"">Refresh</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div id=""transcriptLog"" class=""log-list""></div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<section>");
            sb.AppendLine(@"<h2>LLM System Prompt</h2>");
            sb.AppendLine(@"<div class=""controls"" style=""flex-direction:column;align-items:stretch;"">");
            sb.AppendLine(@"<textarea id=""llmPromptText"" placeholder=""Edit runtime system prompt for /respond...""></textarea>");
            sb.AppendLine(@"<div class=""controls"" style=""width:100%;"">");
            sb.AppendLine(@"<button onclick=""loadLlmPrompt()"">Load Prompt</button>");
            sb.AppendLine(@"<button onclick=""saveLlmPrompt()"">Save Prompt</button>");
            sb.AppendLine(@"<button onclick=""resetLlmPrompt()"">Reset Prompt</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<small style=""opacity:0.75"">Applied in real time by python_voice_service /respond/config.</small>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</section>");
            sb.AppendLine(@"<section>");
            sb.AppendLine(@"<h2>Expressions</h2>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<label for=""faceSeconds"">Duration (s)</label>");
            sb.AppendLine(@"<input id=""faceSeconds"" type=""number"" min=""0"" step=""0.5"" value=""3"">");
            sb.AppendLine(@"<label for=""faceSelect"">Preset</label>");
            sb.AppendLine(@"<select id=""faceSelect"">");
            sb.AppendLine(@"  <option value="""">-- Select --</option>");
            sb.AppendLine(@"  <option value=""neutral"">neutral</option>");
            sb.AppendLine(@"  <option value=""happy"">happy</option>");
            sb.AppendLine(@"  <option value=""excited"">excited</option>");
            sb.AppendLine(@"  <option value=""sad"">sad</option>");
            sb.AppendLine(@"  <option value=""verysad"">verySad</option>");
            sb.AppendLine(@"  <option value=""confused"">confused</option>");
            sb.AppendLine(@"  <option value=""concerned"">concerned</option>");
            sb.AppendLine(@"  <option value=""upset"">upset</option>");
            sb.AppendLine(@"  <option disabled>──────────</option>");
            sb.AppendLine(@"  <option value=""ANeutral"">ANeutral</option>");
            sb.AppendLine(@"  <option value=""AHappy"">AHappy</option>");
            sb.AppendLine(@"  <option value=""AConcerned"">AConcerned</option>");
            sb.AppendLine(@"  <option value=""AConfused"">AConfused</option>");
            sb.AppendLine(@"  <option value=""AUpset"">AUpset</option>");
            sb.AppendLine(@"  <option value=""BNeutral"">BNeutral</option>");
            sb.AppendLine(@"  <option value=""BHappy"">BHappy</option>");
            sb.AppendLine(@"  <option value=""BConcerned"">BConcerned</option>");
            sb.AppendLine(@"  <option value=""BConfused"">BConfused</option>");
            sb.AppendLine(@"  <option value=""BUpset"">BUpset</option>");
            sb.AppendLine(@"  <option value=""CNeutral"">CNeutral</option>");
            sb.AppendLine(@"  <option value=""CHappy"">CHappy</option>");
            sb.AppendLine(@"  <option value=""CConcerned"">CConcerned</option>");
            sb.AppendLine(@"  <option value=""CConfused"">CConfused</option>");
            sb.AppendLine(@"  <option value=""CUpset"">CUpset</option>");
            sb.AppendLine(@"  <option value=""DNeutral"">DNeutral</option>");
            sb.AppendLine(@"  <option value=""DHappy"">DHappy</option>");
            sb.AppendLine(@"  <option value=""DConcerned"">DConcerned</option>");
            sb.AppendLine(@"  <option value=""DConfused"">DConfused</option>");
            sb.AppendLine(@"  <option value=""DUpset"">DUpset</option>");
            sb.AppendLine(@"</select>");
            sb.AppendLine(@"<input id=""faceCustom"" type=""text"" placeholder=""custom preset name"">");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<button onclick=""facePreset('excited')"">Excited</button>");
            sb.AppendLine(@"<button onclick=""facePreset('happy')"">Happy</button>");
            sb.AppendLine(@"<button onclick=""facePreset('neutral')"">Neutral</button>");
            sb.AppendLine(@"<button onclick=""facePreset('sad')"">Sad</button>");
            sb.AppendLine(@"<button onclick=""facePreset('verySad')"">Very Sad</button>");
            sb.AppendLine(@"<button onclick=""faceSelected()"">Send Selected</button>");
            sb.AppendLine(@"<button onclick=""customFace()"">Send Custom</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</section>");
            sb.AppendLine(@"<section>");
            sb.AppendLine(@"<h2>Camera</h2>");
            sb.AppendLine(@"<div class=""controls"" style=""flex-direction:column;align-items:flex-start"">");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<button onclick=""startCameraPreview()"">Start Preview</button>");
            sb.AppendLine(@"<button onclick=""stopCameraPreview()"">Stop Preview</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<img id=""cameraView"" src=""/camera.jpg"" alt=""camera"" style=""max-width:100%;width:640px;height:auto;border-radius:12px;border:1px solid rgba(255,255,255,0.08);box-shadow:0 12px 36px rgba(0,0,0,0.35)""/>");
            sb.AppendLine(@"<small style=""opacity:0.7"">Preview polling runs only when this page has focus or is fullscreen.</small>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</section>");
            sb.AppendLine(@"<section>");
            sb.AppendLine(@"<h2>Camera Vision (LLM)</h2>");
            sb.AppendLine(@"<div class=""controls"" style=""flex-direction:column;align-items:stretch;"">");
            sb.AppendLine(@"<input id=""visionModel"" type=""text"" placeholder=""Vision model (e.g. gemma3:4b)"" value=""gemma3:4b"">");
            sb.AppendLine(@"<textarea id=""visionPrompt"" placeholder=""Ask the model what it sees in the current camera frame..."">Describe what you see and call out anything important for the current exercise.</textarea>");
            sb.AppendLine(@"<div class=""controls"" style=""width:100%;"">");
            sb.AppendLine(@"<button onclick=""describeCameraNow()"">Describe Current Frame</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<pre id=""visionResult"" style=""min-height:88px;margin-top:0.4rem;padding:0.65rem 0.75rem;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:rgba(0,0,0,0.25);white-space:pre-wrap;"">Result will appear here.</pre>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</section>");
            sb.AppendLine(@"<section>");
            sb.AppendLine(@"<h2>LED Lighting</h2>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<input id=""ledColor"" type=""color"" value=""#00bfff"">");
            sb.AppendLine(@"<label for=""ledBrightness"">Brightness</label>");
            sb.AppendLine(@"<input id=""ledBrightness"" type=""number"" min=""0.1"" max=""1"" step=""0.1"" value=""0.8"">");
            sb.AppendLine(@"<label for=""ledPeriod"">Period</label>");
            sb.AppendLine(@"<input id=""ledPeriod"" type=""number"" min=""0.5"" step=""0.1"" value=""2"">");
            sb.AppendLine(@"<label for=""ledDuration"">Duration</label>");
            sb.AppendLine(@"<input id=""ledDuration"" type=""number"" min=""0"" step=""0.1"" value=""0"">");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<button onclick=""ledBreathe()"">Breathe</button>");
            sb.AppendLine(@"<button onclick=""ledSolid()"">Solid</button>");
            sb.AppendLine(@"<button onclick=""ledRandom()"">Random</button>");
            sb.AppendLine(@"<button onclick=""send('/api/led',{mode:'off'})"">Off</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</section>");
            sb.AppendLine(@"<section>");
            sb.AppendLine(@"<h2>Flower Servo</h2>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<button onclick=""send('/api/flower',{action:'open'})"">Open</button>");
            sb.AppendLine(@"<button onclick=""send('/api/flower',{action:'close'})"">Close</button>");
            sb.AppendLine(@"<button onclick=""send('/api/flower',{action:'open_hold'})"">Hold Open</button>");
            sb.AppendLine(@"<button onclick=""send('/api/flower',{action:'close_hold'})"">Hold Close</button>");
            sb.AppendLine(@"<button onclick=""send('/api/flower',{action:'center'})"">Center</button>");
            sb.AppendLine(@"<button onclick=""send('/api/flower',{action:'stop'})"">Stop</button>");
            sb.AppendLine(@"<button onclick=""send('/api/flower',{action:'open_slow'})"">Slow Open</button>");
            sb.AppendLine(@"<button onclick=""send('/api/flower',{action:'close_slow'})"">Slow Close</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</section>");
            sb.AppendLine(@"<section>");
            sb.AppendLine(@"<h2>Voice &amp; TTS</h2>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<label for=""voiceSelect"">Voice</label>");
            sb.AppendLine(@"<select id=""voiceSelect"" onchange=""setVoice(this.value)""></select>");
            sb.AppendLine(@"<label for=""ttsModelSelect"">TTS Model</label>");
            sb.AppendLine(@"<select id=""ttsModelSelect"" onchange=""setTtsModel(this.value)""></select>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""controls"" style=""flex-direction:column;align-items:stretch;"">");
            sb.AppendLine(@"<textarea id=""speakText"" placeholder=""Type the phrase you want the robot to say""></textarea>");
            sb.AppendLine(@"<div class=""controls"" style=""width:100%;"">");
            sb.AppendLine(@"<label for=""voiceSpeed"">Speed</label>");
            sb.AppendLine(@"<input id=""voiceSpeed"" type=""number"" min=""0.5"" max=""2"" step=""0.1"" value=""1"">");
            sb.AppendLine(@"<label for=""voiceVolume"">Volume</label>");
            sb.AppendLine(@"<input id=""voiceVolume"" type=""number"" min=""0.2"" max=""1.5"" step=""0.1"" value=""1"">");
            sb.AppendLine(@"<button onclick=""speakNow()"">Speak</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</section>");

            sb.AppendLine(@"<section>");
            sb.AppendLine(@"<h2>Speech Recognition</h2>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<label for=""asrModeSelect"">ASR Mode</label>");
            sb.AppendLine(@"<select id=""asrModeSelect""></select>");
            sb.AppendLine(@"<button onclick=""applyAsrMode()"">Apply Mode</button>");
            sb.AppendLine(@"<button onclick=""refreshAsrStatus()"">Refresh Status</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<button onclick=""startAgentListening()"">Start Listening</button>");
            sb.AppendLine(@"<button onclick=""pauseAgentListening()"">Pause Listening</button>");
            sb.AppendLine(@"<span id=""asrState"" style=""font-size:0.9rem;opacity:0.85;"">Loading ASR status...</span>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</section>");

            sb.AppendLine(@"<section>");
            sb.AppendLine(@"<h2>Qwen TTS (Speaker + Emotion)</h2>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<label for=""qwenSpeakerSelect"">Speaker</label>");
            sb.AppendLine(@"<select id=""qwenSpeakerSelect""></select>");
            sb.AppendLine(@"<label for=""qwenInstruct"">Emotion / Style</label>");
            sb.AppendLine(@"<input id=""qwenInstruct"" type=""text"" value=""friendly"" placeholder=""fixed by server"">");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""controls"" style=""flex-direction:column;align-items:stretch;"">");
            sb.AppendLine(@"<textarea id=""qwenSpeakText"" placeholder=""Text to synthesize with Qwen TTS""></textarea>");
            sb.AppendLine(@"<div class=""controls"" style=""width:100%;"">");
            sb.AppendLine(@"<button onclick=""qwenSpeakNow()"">Speak (Qwen)</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</section>");
            sb.AppendLine(@"<section>");
            sb.AppendLine(@"<h2>Game Control</h2>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<input id=""gameName"" type=""text"" placeholder=""Game ID (e.g. cornhole)"">");
            sb.AppendLine(@"<button onclick=""launchGame()"">Launch</button>");
            sb.AppendLine(@"<button onclick=""exitGame()"">Exit</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</section>");
            sb.AppendLine(@"<div id=""status"">Ready.</div>");
            sb.AppendLine(@"<script>");
            sb.AppendLine(@"const statusEl = document.getElementById('status');");
            sb.AppendLine(@"const voiceSelect = document.getElementById('voiceSelect');");
            sb.AppendLine(@"const modelSelect = document.getElementById('ttsModelSelect');");
            sb.AppendLine(@"const qwenSpeakerSelect = document.getElementById('qwenSpeakerSelect');");
            sb.AppendLine(@"const asrModeSelect = document.getElementById('asrModeSelect');");
            sb.AppendLine(@"const asrStateEl = document.getElementById('asrState');");
            sb.AppendLine(@"const llmPromptEl = document.getElementById('llmPromptText');");
            sb.AppendLine(@"const logContainer = document.getElementById('transcriptLog');");
            sb.AppendLine(@"const cameraView = document.getElementById('cameraView');");
            sb.AppendLine(@"const logRoleStyles = {");
            sb.AppendLine(@"  user:{icon:'🧍',bg:'rgba(59,130,246,0.18)',color:'#60a5fa'},");
            sb.AppendLine(@"  coach:{icon:'🤖',bg:'rgba(251,146,60,0.18)',color:'#fb923c'},");
            sb.AppendLine(@"  wizard:{icon:'🪄',bg:'rgba(168,85,247,0.18)',color:'#c084fc'},");
            sb.AppendLine(@"  system:{icon:'ℹ️',bg:'rgba(156,163,175,0.2)',color:'#d1d5db'}");
            sb.AppendLine(@"};");
            sb.AppendLine(@"function speakerFromRole(role){");
            sb.AppendLine(@"  switch(role){");
            sb.AppendLine(@"    case 'coach': return 'RACHEL';");
            sb.AppendLine(@"    case 'wizard': return 'Wizard Override';");
            sb.AppendLine(@"    case 'system': return 'System';");
            sb.AppendLine(@"    default: return 'User';");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function escapeHtml(str){");
            sb.AppendLine(@"  if(!str) return '';");
            sb.AppendLine(@"  return String(str)");
            sb.AppendLine(@"    .replace(/&/g,""&amp;"")");
            sb.AppendLine(@"    .replace(/</g,""&lt;"")");
            sb.AppendLine(@"    .replace(/>/g,""&gt;"")");
            sb.AppendLine(@"    .replace(/""/g,""&quot;"")");
            sb.AppendLine(@"    .replace(/'/g,""&#39;"");");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function formatLogTime(value){");
            sb.AppendLine(@"  if(!value) return '';");
            sb.AppendLine(@"  const date = new Date(value);");
            sb.AppendLine(@"  if(isNaN(date.getTime())) return '';");
            sb.AppendLine(@"  return date.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function renderLog(entries){");
            sb.AppendLine(@"  if(!logContainer) return;");
            sb.AppendLine(@"  if(!Array.isArray(entries) || !entries.length){");
            sb.AppendLine("    logContainer.innerHTML = '<div class=\"log-empty\">Conversations will appear here once the agent speaks.</div>';");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  const nearBottom = (logContainer.scrollTop + logContainer.clientHeight) >= (logContainer.scrollHeight - 20);");
            sb.AppendLine(@"  const html = entries.map(entry => {");
            sb.AppendLine(@"    let role = typeof entry.role === 'string' ? entry.role.toLowerCase() : 'user';");
            sb.AppendLine(@"    const src = typeof entry.source === 'string' ? entry.source.toLowerCase() : '';"); 
            sb.AppendLine(@"    // 如果来源是对话服务，强制按 coach 展示，避免角色字符串异常被当成 user");
            sb.AppendLine(@"    if (src.includes('dialog_service') || src === 'dialog' || src.includes('dialog')) { role = 'coach'; }");
            sb.AppendLine(@"    // 面板触发（tester_panel）或显式标为 wizard 的，按 wizard 展示");
            sb.AppendLine(@"    if (src.includes('tester_panel') || role === 'wizard') { role = 'wizard'; }");
            sb.AppendLine(@"    const style = logRoleStyles[role] || logRoleStyles.user;");
            sb.AppendLine(@"    const speaker = escapeHtml(entry.speaker || speakerFromRole(role));");
            sb.AppendLine(@"    const text = escapeHtml(entry.message || '');");
            sb.AppendLine(@"    const timestamp = formatLogTime(entry.timestamp);");
            sb.AppendLine("    return `<div class=\"log-entry\"><div class=\"log-icon\" style=\"background:${style.bg};color:${style.color};\">${style.icon}</div><div class=\"log-body\"><div class=\"log-meta\"><span class=\"log-speaker\">${speaker}</span><span>•</span><span>${timestamp}</span></div><div class=\"log-message\">${text}</div></div></div>`;");
            sb.AppendLine(@"  }).join('');");
            sb.AppendLine(@"  logContainer.innerHTML = html;");
            sb.AppendLine(@"  if(nearBottom){");
            sb.AppendLine(@"    logContainer.scrollTop = logContainer.scrollHeight;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function refreshLog(){");
            sb.AppendLine(@"  if(!logContainer) return;");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch('/api/logs');");
            sb.AppendLine(@"    if(!resp.ok) return;");
            sb.AppendLine(@"    const data = await resp.json();");
            sb.AppendLine(@"    const entries = Array.isArray(data.entries) ? data.entries : [];");
            sb.AppendLine(@"    renderLog(entries);");
            sb.AppendLine(@"  } catch(err) {");
            sb.AppendLine(@"    console.warn('log fetch failed', err);");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function send(endpoint, payload){");
            sb.AppendLine(@"  statusEl.textContent = 'Sending ' + endpoint + ' ...';");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch(endpoint,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload||{})});");
            sb.AppendLine(@"    const data = await resp.json();");
            sb.AppendLine(@"    statusEl.textContent = data.status + ': ' + data.message;");
            sb.AppendLine(@"    return data;");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"    return null;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function parseSeconds(){");
            sb.AppendLine(@"  const seconds = parseFloat(document.getElementById('faceSeconds').value);");
            sb.AppendLine(@"  return isNaN(seconds) ? 3 : seconds;");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function facePreset(name){");
            sb.AppendLine(@"  send('/api/face',{mode:name,seconds:parseSeconds()});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function customFace(){");
            sb.AppendLine(@"  const value = document.getElementById('faceCustom').value||'happy';");
            sb.AppendLine(@"  send('/api/face',{mode:'custom',value:value,seconds:parseSeconds()});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function faceSelected(){");
            sb.AppendLine(@"  const sel = document.getElementById('faceSelect');");
            sb.AppendLine(@"  if(!sel) return;");
            sb.AppendLine(@"  let value = (sel.value||'').trim();");
            sb.AppendLine(@"  if(!value){ statusEl.textContent='error: select a preset'; return; }");
            sb.AppendLine(@"  // 兼容：若用户列表里仍有空格，发送前去掉空格");
            sb.AppendLine(@"  value = value.replace(/\s+/g,'');");
            sb.AppendLine(@"  send('/api/face',{mode:value,seconds:parseSeconds()});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function ledBreathe(){");
            sb.AppendLine(@"  const color = document.getElementById('ledColor').value;");
            sb.AppendLine(@"  const brightness = parseFloat(document.getElementById('ledBrightness').value)||0.8;");
            sb.AppendLine(@"  const period = parseFloat(document.getElementById('ledPeriod').value)||2;");
            sb.AppendLine(@"  const duration = parseFloat(document.getElementById('ledDuration').value)||0;");
            sb.AppendLine(@"  send('/api/led',{mode:'breathe',color:color,brightness:brightness,period:period,duration:duration});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function ledSolid(){");
            sb.AppendLine(@"  const color = document.getElementById('ledColor').value;");
            sb.AppendLine(@"  const brightness = parseFloat(document.getElementById('ledBrightness').value)||0.8;");
            sb.AppendLine(@"  const duration = parseFloat(document.getElementById('ledDuration').value)||0;");
            sb.AppendLine(@"  send('/api/led',{mode:'solid',color:color,brightness:brightness,duration:duration});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function ledRandom(){");
            sb.AppendLine(@"  const duration = parseFloat(document.getElementById('ledDuration').value)||0;");
            sb.AppendLine(@"  send('/api/led',{mode:'random',duration:duration});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function setVoice(value){");
            sb.AppendLine(@"  if(!value) return;");
            sb.AppendLine(@"  send('/api/voice',{action:'set',voice:value});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function setTtsModel(value){");
            sb.AppendLine(@"  if(!value) return;");
            sb.AppendLine(@"  send('/api/voice',{action:'set_model',model:value});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function speakNow(){");
            sb.AppendLine(@"  const text = document.getElementById('speakText').value;");
            sb.AppendLine(@"  if(!text.trim()){");
            sb.AppendLine(@"    statusEl.textContent = 'error: enter text to speak';");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  const speed = parseFloat(document.getElementById('voiceSpeed').value)||1;");
            sb.AppendLine(@"  const volume = parseFloat(document.getElementById('voiceVolume').value)||1;");
            sb.AppendLine(@"  const model = modelSelect ? modelSelect.value : '';");
            sb.AppendLine(@"  send('/api/speak',{text:text,voice:voiceSelect.value,model:model,speed:speed,volume:volume});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function qwenSpeakNow(){");
            sb.AppendLine(@"  const text = document.getElementById('qwenSpeakText').value;");
            sb.AppendLine(@"  if(!text.trim()){ statusEl.textContent = 'error: enter text to speak'; return; }");
            sb.AppendLine(@"  const speaker = qwenSpeakerSelect ? qwenSpeakerSelect.value : '';");
            sb.AppendLine(@"  const instruct = (document.getElementById('qwenInstruct').value||'').trim();");
            sb.AppendLine(@"  send('/api/qwen/speak',{text:text,speaker:speaker,instruct:instruct});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function describeCameraNow(){");
            sb.AppendLine(@"  const promptEl = document.getElementById('visionPrompt');");
            sb.AppendLine(@"  const modelEl = document.getElementById('visionModel');");
            sb.AppendLine(@"  const resultEl = document.getElementById('visionResult');");
            sb.AppendLine(@"  if(resultEl){ resultEl.textContent = 'Preparing latest camera frame...'; }");
            sb.AppendLine(@"  if(statusEl){ statusEl.textContent = 'vision: preparing request...'; }");
            sb.AppendLine(@"  if(!cameraPolling){");
            sb.AppendLine(@"    cameraAutoStart = true;");
            sb.AppendLine(@"    startPolling();");
            sb.AppendLine(@"    pullCameraFrame();");
            sb.AppendLine(@"    await new Promise(resolve => setTimeout(resolve, 280));");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  const prompt = promptEl ? String(promptEl.value || '').trim() : '';");
            sb.AppendLine(@"  const model = modelEl ? String(modelEl.value || '').trim() : '';");
            sb.AppendLine(@"  const payload = { prompt: prompt };");
            sb.AppendLine(@"  if(model){ payload.model = model; }");
            sb.AppendLine(@"  const controller = new AbortController();");
            sb.AppendLine(@"  const timeoutId = setTimeout(() => controller.abort(), 300000);");
            sb.AppendLine(@"  if(statusEl){ statusEl.textContent = 'vision: requesting /api/vision/describe ...'; }");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch('/api/vision/describe', {");
            sb.AppendLine(@"      method:'POST',");
            sb.AppendLine(@"      headers:{'Content-Type':'application/json'},");
            sb.AppendLine(@"      body: JSON.stringify(payload),");
            sb.AppendLine(@"      signal: controller.signal");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    const raw = await resp.text();");
            sb.AppendLine(@"    let data = null;");
            sb.AppendLine(@"    try { data = JSON.parse(raw); } catch(err) { data = null; }");
            sb.AppendLine(@"    if(!resp.ok){");
            sb.AppendLine(@"      const msg = data && data.message ? data.message : (raw || ('HTTP ' + resp.status));");
            sb.AppendLine(@"      if(resultEl){ resultEl.textContent = 'Error: ' + msg; }");
            sb.AppendLine(@"      if(statusEl){ statusEl.textContent = 'vision error: ' + msg; }");
            sb.AppendLine(@"      return;");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    const desc = data && data.description ? String(data.description) : (raw || 'No description returned.');");
            sb.AppendLine(@"    if(resultEl){ resultEl.textContent = desc; }");
            sb.AppendLine(@"    if(statusEl){ statusEl.textContent = 'vision: done'; }");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    const msg = (err && err.name === 'AbortError') ? 'vision request timed out' : ('vision request failed: ' + err);");
            sb.AppendLine(@"    if(resultEl){ resultEl.textContent = 'Error: ' + msg; }");
            sb.AppendLine(@"    if(statusEl){ statusEl.textContent = msg; }");
            sb.AppendLine(@"  } finally {");
            sb.AppendLine(@"    clearTimeout(timeoutId);");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function loadLlmPrompt(){");
            sb.AppendLine(@"  if(!llmPromptEl) return;");
            sb.AppendLine(@"  statusEl.textContent = 'Loading /api/llm/prompt ...';");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch('/api/llm/prompt');");
            sb.AppendLine(@"    const data = await resp.json();");
            sb.AppendLine(@"    if(!resp.ok || data.status !== 'ok'){");
            sb.AppendLine(@"      const msg = (data && data.message) ? data.message : ('HTTP ' + resp.status);");
            sb.AppendLine(@"      statusEl.textContent = 'error: ' + msg;");
            sb.AppendLine(@"      return;");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    const promptText = (typeof data.prompt === 'string') ? data.prompt : ((typeof data.system_prompt === 'string') ? data.system_prompt : '');");
            sb.AppendLine(@"    llmPromptEl.value = promptText;");
            sb.AppendLine(@"    const source = data.runtime_override_active ? 'runtime' : (data.source || 'env_or_default');");
            sb.AppendLine(@"    statusEl.textContent = 'ok: llm prompt loaded (' + source + ')';");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function saveLlmPrompt(){");
            sb.AppendLine(@"  if(!llmPromptEl) return;");
            sb.AppendLine(@"  const prompt = (llmPromptEl.value || '').trim();");
            sb.AppendLine(@"  if(!prompt){ statusEl.textContent = 'error: prompt required'; return; }");
            sb.AppendLine(@"  await send('/api/llm/prompt',{prompt:prompt});");
            sb.AppendLine(@"  await loadLlmPrompt();");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function resetLlmPrompt(){");
            sb.AppendLine(@"  await send('/api/llm/prompt',{reset:true});");
            sb.AppendLine(@"  await loadLlmPrompt();");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function launchGame(){");
            sb.AppendLine(@"  const name = document.getElementById('gameName').value||'';");
            sb.AppendLine(@"  send('/api/game',{action:'launch',name:name});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function exitGame(){");
            sb.AppendLine(@"  send('/api/game',{action:'exit'});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function renderAsrStatus(data){");
            sb.AppendLine(@"  if(!data) return;");
            sb.AppendLine(@"  const mode = (typeof data.mode === 'string' && data.mode) ? data.mode : 'offline';");
            sb.AppendLine(@"  const listening = !!data.listening;");
            sb.AppendLine(@"  const apiReady = !!data.openai_configured;");
            sb.AppendLine(@"  if(asrStateEl){");
            sb.AppendLine(@"    const suffix = (mode === 'api' && !apiReady) ? ' | OPENAI_API_KEY missing' : '';");
            sb.AppendLine(@"    asrStateEl.textContent = `Mode: ${mode} | Listening: ${listening ? 'ON' : 'PAUSED'}${suffix}`;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  const modes = Array.isArray(data.available_modes) ? data.available_modes : ['offline','api'];");
            sb.AppendLine(@"  if(asrModeSelect){");
            sb.AppendLine(@"    asrModeSelect.innerHTML = modes.map(v => `<option value=""${v}"">${v}</option>`).join('');");
            sb.AppendLine(@"    if(modes.includes(mode)){ asrModeSelect.value = mode; }");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function refreshAsrStatus(){");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch('/api/asr');");
            sb.AppendLine(@"    const data = await resp.json();");
            sb.AppendLine(@"    if(!resp.ok || data.status !== 'ok'){");
            sb.AppendLine(@"      const msg = (data && data.message) ? data.message : ('HTTP ' + resp.status);");
            sb.AppendLine(@"      if(asrStateEl){ asrStateEl.textContent = 'ASR error: ' + msg; }");
            sb.AppendLine(@"      return;");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    renderAsrStatus(data);");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    if(asrStateEl){ asrStateEl.textContent = 'ASR error: ' + err; }");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function applyAsrMode(){");
            sb.AppendLine(@"  const mode = asrModeSelect ? String(asrModeSelect.value || '').trim() : '';");
            sb.AppendLine(@"  if(!mode){ statusEl.textContent = 'error: select ASR mode'; return; }");
            sb.AppendLine(@"  const data = await send('/api/asr',{action:'set_mode',mode:mode});");
            sb.AppendLine(@"  if(data && data.status === 'ok'){");
            sb.AppendLine(@"    renderAsrStatus(data);");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function startAgentListening(){");
            sb.AppendLine(@"  const data = await send('/api/asr',{action:'start_listening'});");
            sb.AppendLine(@"  if(data && data.status === 'ok'){ renderAsrStatus(data); }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function pauseAgentListening(){");
            sb.AppendLine(@"  const data = await send('/api/asr',{action:'pause_listening'});");
            sb.AppendLine(@"  if(data && data.status === 'ok'){ renderAsrStatus(data); }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function loadVoiceOptions(){");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch('/api/voice/options');");
            sb.AppendLine(@"    if(!resp.ok){ return; }");
            sb.AppendLine(@"    const data = await resp.json();");
            sb.AppendLine(@"    const voices = Array.isArray(data.voices) ? data.voices : [];");
            sb.AppendLine(@"    voiceSelect.innerHTML = voices.map(v => `<option value=""${v}"">${v}</option>`).join('');");
            sb.AppendLine(@"    const current = typeof data.current === 'string' ? data.current : '';");
            sb.AppendLine(@"    if(current && voices.includes(current)){");
            sb.AppendLine(@"      voiceSelect.value = current;");
            sb.AppendLine(@"    } else if(voices.length){");
            sb.AppendLine(@"      voiceSelect.value = voices[0];");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    const models = Array.isArray(data.models) ? data.models : [];");
            sb.AppendLine(@"    if(modelSelect){");
            sb.AppendLine(@"      modelSelect.innerHTML = models.map(v => `<option value=""${v}"">${v}</option>`).join('');");
            sb.AppendLine(@"      const modelCurrent = typeof data.modelCurrent === 'string' ? data.modelCurrent : '';");
            sb.AppendLine(@"      if(modelCurrent && models.includes(modelCurrent)){");
            sb.AppendLine(@"        modelSelect.value = modelCurrent;");
            sb.AppendLine(@"      } else if(models.length){");
            sb.AppendLine(@"        modelSelect.value = models[0];");
            sb.AppendLine(@"      }");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"  } catch(err) {");
            sb.AppendLine(@"    console.warn('voice options failed', err);");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function loadQwenOptions(){");
            sb.AppendLine(@"  if(!qwenSpeakerSelect) return;");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch('/api/qwen/options');");
            sb.AppendLine(@"    if(!resp.ok){ return; }");
            sb.AppendLine(@"    const data = await resp.json();");
            sb.AppendLine(@"    const speakers = Array.isArray(data.speakers) ? data.speakers : [];");
            sb.AppendLine(@"    qwenSpeakerSelect.innerHTML = speakers.map(v => `<option value=""${v}"">${v}</option>`).join('');");
            sb.AppendLine(@"    const current = typeof data.current === 'string' ? data.current : '';");
            sb.AppendLine(@"    if(current && speakers.includes(current)){ qwenSpeakerSelect.value = current; }");
            sb.AppendLine(@"    else if(speakers.length){ qwenSpeakerSelect.value = speakers[0]; }");
            sb.AppendLine(@"  } catch(err) { console.warn('qwen options failed', err); }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"if(logContainer){ renderLog([]); }");
            sb.AppendLine(@"refreshLog();");
            sb.AppendLine(@"setInterval(refreshLog, 4000);");
            sb.AppendLine(@"loadVoiceOptions();");
            sb.AppendLine(@"loadQwenOptions();");
            sb.AppendLine(@"refreshAsrStatus();");
            sb.AppendLine(@"setInterval(refreshAsrStatus, 5000);");
            sb.AppendLine(@"loadLlmPrompt();");
            sb.AppendLine(@"let cameraPolling = false;");
            sb.AppendLine(@"let cameraSeq = 0;");
            sb.AppendLine(@"let cameraLastLoadAt = 0;");
            sb.AppendLine(@"let cameraLastSetAt = 0;");
            sb.AppendLine(@"let cameraRequestInFlight = false;");
            sb.AppendLine(@"let cameraRequestStartedAt = 0;");
            sb.AppendLine(@"let cameraPullTimer = null;");
            sb.AppendLine(@"let cameraWatchdogTimer = null;");
            sb.AppendLine(@"let cameraHeartbeatTimer = null;");
            sb.AppendLine(@"let cameraAutoStart = false;");
            sb.AppendLine(@"function hasClientFocusOrFullscreen(){");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    if(document.fullscreenElement){ return true; }");
            sb.AppendLine(@"    if(typeof document.hidden === 'boolean' && !document.hidden){ return true; }");
            sb.AppendLine(@"    return !!(document.hasFocus && document.hasFocus());");
            sb.AppendLine(@"  } catch(_){");
            sb.AppendLine(@"    return true;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function cameraHeartbeat(){");
            sb.AppendLine(@"  if(!hasClientFocusOrFullscreen()) return;");
            sb.AppendLine(@"  fetch('/api/camera/ping',{method:'POST',cache:'no-store'}).catch(()=>{});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function pullCameraFrame(){");
            sb.AppendLine(@"  if(!cameraView) return;");
            sb.AppendLine(@"  if(!hasClientFocusOrFullscreen()) return;");
            sb.AppendLine(@"  const now = Date.now();");
            sb.AppendLine(@"  if(cameraRequestInFlight && (now - cameraRequestStartedAt) < 1800){ return; }");
            sb.AppendLine(@"  cameraSeq++;");
            sb.AppendLine(@"  cameraLastSetAt = now;");
            sb.AppendLine(@"  cameraRequestInFlight = true;");
            sb.AppendLine(@"  cameraRequestStartedAt = now;");
            sb.AppendLine(@"  cameraView.src = '/camera.jpg?t=' + cameraLastSetAt + '&n=' + cameraSeq;");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function cameraWatchdog(){");
            sb.AppendLine(@"  if(!cameraView) return;");
            sb.AppendLine(@"  if(!hasClientFocusOrFullscreen()) return;");
            sb.AppendLine(@"  const now = Date.now();");
            sb.AppendLine(@"  if(cameraRequestInFlight && (now - cameraRequestStartedAt) > 2500){");
            sb.AppendLine(@"    cameraRequestInFlight = false;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(cameraLastSetAt > 0 && (now - cameraLastSetAt) > 2200 && (now - cameraLastLoadAt) > 2200){");
            sb.AppendLine(@"    pullCameraFrame();");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function startPolling(){");
            sb.AppendLine(@"  if(cameraPolling) return;");
            sb.AppendLine(@"  if(!hasClientFocusOrFullscreen()) return;");
            sb.AppendLine(@"  cameraPolling = true;");
            sb.AppendLine(@"  cameraView.onload = () => { cameraLastLoadAt = Date.now(); cameraRequestInFlight = false; };");
            sb.AppendLine(@"  cameraView.onerror = () => { cameraRequestInFlight = false; setTimeout(pullCameraFrame, 250); };");
            sb.AppendLine(@"  cameraHeartbeat();");
            sb.AppendLine(@"  pullCameraFrame();");
            sb.AppendLine(@"  cameraPullTimer = setInterval(pullCameraFrame, 1000);");
            sb.AppendLine(@"  cameraWatchdogTimer = setInterval(cameraWatchdog, 1000);");
            sb.AppendLine(@"  cameraHeartbeatTimer = setInterval(cameraHeartbeat, 1000);");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function stopPolling(){");
            sb.AppendLine(@"  cameraPolling = false;");
            sb.AppendLine(@"  cameraRequestInFlight = false;");
            sb.AppendLine(@"  if(cameraPullTimer){ clearInterval(cameraPullTimer); cameraPullTimer = null; }");
            sb.AppendLine(@"  if(cameraWatchdogTimer){ clearInterval(cameraWatchdogTimer); cameraWatchdogTimer = null; }");
            sb.AppendLine(@"  if(cameraHeartbeatTimer){ clearInterval(cameraHeartbeatTimer); cameraHeartbeatTimer = null; }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function startCameraPreview(){");
            sb.AppendLine(@"  cameraAutoStart = true;");
            sb.AppendLine(@"  startPolling();");
            sb.AppendLine(@"  pullCameraFrame();");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function stopCameraPreview(){");
            sb.AppendLine(@"  cameraAutoStart = false;");
            sb.AppendLine(@"  stopPolling();");
            sb.AppendLine(@"  if(cameraView){ cameraView.removeAttribute('src'); }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"document.addEventListener('visibilitychange', () => {");
            sb.AppendLine(@"  if(document.hidden && !document.fullscreenElement){");
            sb.AppendLine(@"    stopPolling();");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(cameraAutoStart){");
            sb.AppendLine(@"    startPolling();");
            sb.AppendLine(@"    setTimeout(pullCameraFrame, 30);");
            sb.AppendLine(@"    setTimeout(pullCameraFrame, 200);");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"});");
            sb.AppendLine(@"window.addEventListener('focus', () => { if(cameraAutoStart){ startPolling(); setTimeout(pullCameraFrame, 30); } });");
            sb.AppendLine(@"window.addEventListener('blur', () => {");
            sb.AppendLine(@"  setTimeout(() => {");
            sb.AppendLine(@"    if(document.hidden && !document.fullscreenElement){ stopPolling(); }");
            sb.AppendLine(@"  }, 120);");
            sb.AppendLine(@"});");
            sb.AppendLine(@"document.addEventListener('fullscreenchange', () => {");
            sb.AppendLine(@"  if(cameraAutoStart && hasClientFocusOrFullscreen()){");
            sb.AppendLine(@"    startPolling();");
            sb.AppendLine(@"    setTimeout(pullCameraFrame, 30);");
            sb.AppendLine(@"  } else {");
            sb.AppendLine(@"    stopPolling();");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"});");
            sb.AppendLine(@"window.addEventListener('beforeunload', stopPolling);");
            sb.AppendLine(@"function initCamera(){ if(!cameraView) return; if(cameraAutoStart && hasClientFocusOrFullscreen()){ startPolling(); } }");
            sb.AppendLine(@"initCamera();");
            sb.AppendLine(@"</script>");
            sb.AppendLine(@"</body>");
            sb.AppendLine(@"</html>");
            return sb.ToString();
        }

        private static string BuildSdkPanelHtml()
        {
            var sb = new StringBuilder(4096);
            sb.AppendLine(@"<!DOCTYPE html>");
            sb.AppendLine(@"<html lang=""en"">");
            sb.AppendLine(@"<head>");
            sb.AppendLine(@"<meta charset=""utf-8"">");
            sb.AppendLine(@"<title>Voice Agent SDK Visualizer</title>");
            sb.AppendLine(@"<meta name=""viewport"" content=""width=device-width, initial-scale=1"">");
            sb.AppendLine(@"<style>");
            sb.AppendLine(@"body{font-family:'Segoe UI',sans-serif;margin:0;background:#0b1220;color:#e2e8f0;padding:1.25rem;}");
            sb.AppendLine(@"h1{margin:.25rem 0 .5rem 0;font-size:1.35rem;} p{opacity:.9;}");
            sb.AppendLine(@".wrap{display:grid;grid-template-columns:320px 1fr;gap:1rem;}");
            sb.AppendLine(@".card{background:#111a2e;border:1px solid rgba(148,163,184,.25);border-radius:12px;padding:1rem;}");
            sb.AppendLine(@"label{display:block;font-size:.82rem;opacity:.85;margin:.35rem 0;}");
            sb.AppendLine(@"select,input,textarea,button{width:100%;box-sizing:border-box;border-radius:8px;border:1px solid rgba(148,163,184,.35);background:#0f172a;color:#e2e8f0;padding:.6rem;}");
            sb.AppendLine(@"textarea{min-height:240px;font-family:Consolas,monospace;font-size:.85rem;}");
            sb.AppendLine(@"button{background:#2563eb;border:none;font-weight:700;cursor:pointer;margin-top:.6rem;}");
            sb.AppendLine(@"button.secondary{background:#334155;}");
            sb.AppendLine(@"pre{background:#020617;border:1px solid rgba(148,163,184,.25);padding:.75rem;border-radius:8px;overflow:auto;min-height:260px;}");
            sb.AppendLine(@".mono{font-family:Consolas,monospace;font-size:.82rem;}");
            sb.AppendLine(@".flow-grid{display:grid;grid-template-columns:280px 1fr 1fr;gap:1rem;margin-top:1rem;}");
            sb.AppendLine(@".template-list{display:flex;flex-direction:column;gap:.5rem;max-height:360px;overflow:auto;padding-right:.2rem;}");
            sb.AppendLine(@".template-item{border:1px dashed rgba(125,211,252,.45);border-radius:8px;padding:.55rem .6rem;background:rgba(15,23,42,.55);cursor:grab;font-size:.82rem;}");
            sb.AppendLine(@".template-item.utility{border-color:rgba(251,191,36,.55);}");
            sb.AppendLine(@".flow-canvas{min-height:380px;border:1px dashed rgba(148,163,184,.4);border-radius:12px;padding:.7rem;background:rgba(2,6,23,.45);display:flex;flex-direction:column;gap:.65rem;}");
            sb.AppendLine(@".flow-empty{opacity:.75;border:1px dashed rgba(148,163,184,.35);border-radius:8px;padding:.8rem;text-align:center;}");
            sb.AppendLine(@".flow-step{border:1px solid rgba(148,163,184,.35);border-radius:10px;padding:.6rem;background:#0f172a;}");
            sb.AppendLine(@".flow-step.selected{border-color:#60a5fa;}");
            sb.AppendLine(@".flow-step.running{border-color:#f59e0b;box-shadow:0 0 0 1px rgba(245,158,11,.35) inset;}");
            sb.AppendLine(@".flow-step.ok{border-color:#10b981;}");
            sb.AppendLine(@".flow-step.error{border-color:#ef4444;}");
            sb.AppendLine(@".flow-head{display:flex;align-items:center;gap:.5rem;justify-content:space-between;margin-bottom:.4rem;}");
            sb.AppendLine(@".flow-title{font-weight:700;font-size:.83rem;}");
            sb.AppendLine(@".flow-body{display:flex;flex-direction:column;gap:.45rem;}");
            sb.AppendLine(@".flow-row{display:flex;gap:.5rem;align-items:center;}");
            sb.AppendLine(@".flow-row > *{margin-top:0;}");
            sb.AppendLine(@".flow-small{font-size:.78rem;opacity:.82;}");
            sb.AppendLine(@".flow-chip{display:inline-block;padding:.15rem .45rem;border-radius:999px;font-size:.72rem;border:1px solid rgba(148,163,184,.35);}");
            sb.AppendLine(@".flow-chip.api{color:#93c5fd;border-color:rgba(147,197,253,.5);}");
            sb.AppendLine(@".flow-chip.delay{color:#fcd34d;border-color:rgba(252,211,77,.55);}");
            sb.AppendLine(@".flow-chip.cond{color:#86efac;border-color:rgba(134,239,172,.55);}");
            sb.AppendLine(@".flow-chip.keyword{color:#f9a8d4;border-color:rgba(249,168,212,.55);}");
            sb.AppendLine(@".flow-actions{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.3rem;}");
            sb.AppendLine(@".flow-actions button{width:auto;padding:.45rem .65rem;font-size:.8rem;margin-top:0;}");
            sb.AppendLine(@".flow-step button{width:auto;padding:.32rem .5rem;font-size:.76rem;margin-top:0;background:#1e293b;border:1px solid rgba(148,163,184,.35);}");
            sb.AppendLine(@".flow-editor{border:1px solid rgba(148,163,184,.3);border-radius:10px;padding:.65rem;background:#0b1326;}");
            sb.AppendLine(@".flow-editor textarea{min-height:130px;}");
            sb.AppendLine(@".flow-inline{display:flex;gap:.5rem;align-items:center;}");
            sb.AppendLine(@".flow-inline input[type=""checkbox""]{width:auto;}");
            sb.AppendLine(@"@media(max-width:900px){.wrap{grid-template-columns:1fr;}}");
            sb.AppendLine(@"@media(max-width:1200px){.flow-grid{grid-template-columns:1fr;}}");
            sb.AppendLine(@"</style>");
            sb.AppendLine(@"</head>");
            sb.AppendLine(@"<body>");
            sb.AppendLine(@"<h1>SDK Visualizer</h1>");
            sb.AppendLine(@"<p>This page calls the same <span class=""mono"">/api/*</span> routes as <span class=""mono"">python_sdk/voice_agent_sdk/client.py</span>.</p>");
            sb.AppendLine(@"<p><a href=""/index.html"" style=""color:#93c5fd;"">Back to User Panel</a></p>");
            sb.AppendLine(@"<div class=""wrap"">");
            sb.AppendLine(@"<div class=""card"">");
            sb.AppendLine(@"<label for=""sdkMethod"">SDK Method</label>");
            sb.AppendLine(@"<select id=""sdkMethod""></select>");
            sb.AppendLine(@"<label for=""sdkEndpoint"">HTTP Endpoint</label>");
            sb.AppendLine(@"<input id=""sdkEndpoint"" readonly>");
            sb.AppendLine(@"<label for=""sdkPayload"">JSON Payload</label>");
            sb.AppendLine(@"<textarea id=""sdkPayload""></textarea>");
            sb.AppendLine(@"<button onclick=""invokeSdk()"">Invoke</button>");
            sb.AppendLine(@"<button class=""secondary"" onclick=""resetPayload()"">Reset Payload</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""card"">");
            sb.AppendLine(@"<div class=""mono"" id=""sdkStatus"">Ready.</div>");
            sb.AppendLine(@"<h3>Response</h3>");
            sb.AppendLine(@"<pre id=""sdkResp""></pre>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""card"" style=""margin-top:1rem;"">");
            sb.AppendLine(@"<h2 style=""margin:.1rem 0 .45rem 0;font-size:1.12rem;"">Flow Builder (Phase 1)</h2>");
            sb.AppendLine(@"<p class=""flow-small"">Drag templates into canvas, edit step params, then run sequentially.</p>");
            sb.AppendLine(@"<div class=""flow-grid"">");
            sb.AppendLine(@"<div>");
            sb.AppendLine(@"<h3 style=""margin:.2rem 0 .45rem 0;"">Templates</h3>");
            sb.AppendLine(@"<div id=""flowTemplates"" class=""template-list""></div>");
            sb.AppendLine(@"<div class=""flow-small"">Includes all SDK API methods plus utility Delay/Condition nodes.</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div>");
            sb.AppendLine(@"<h3 style=""margin:.2rem 0 .45rem 0;"">Canvas</h3>");
            sb.AppendLine(@"<div class=""flow-actions"">");
            sb.AppendLine(@"<button id=""flowRunBtn"" type=""button"">Run Flow</button>");
            sb.AppendLine(@"<button id=""flowStopBtn"" class=""secondary"" type=""button"">Stop</button>");
            sb.AppendLine(@"<button id=""flowClearBtn"" class=""secondary"" type=""button"">Clear</button>");
            sb.AppendLine(@"<button id=""flowExportBtn"" class=""secondary"" type=""button"">Export</button>");
            sb.AppendLine(@"<button id=""flowImportBtn"" class=""secondary"" type=""button"">Import</button>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div id=""flowCanvas"" class=""flow-canvas""></div>");
            sb.AppendLine(@"<label for=""flowJson"">Flow JSON</label>");
            sb.AppendLine(@"<textarea id=""flowJson"" style=""min-height:135px;""></textarea>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div>");
            sb.AppendLine(@"<h3 style=""margin:.2rem 0 .45rem 0;"">Step Editor</h3>");
            sb.AppendLine(@"<div id=""flowEditor"" class=""flow-empty"">Select a step to edit.</div>");
            sb.AppendLine(@"<h3 style=""margin:.8rem 0 .45rem 0;"">Run Log</h3>");
            sb.AppendLine(@"<pre id=""flowLog"" style=""min-height:210px;""></pre>");
            sb.AppendLine(@"<div id=""flowStatus"" class=""mono"">Idle.</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<script>");
            sb.AppendLine(@"const sdkMap = {");
            sb.AppendLine(@"  'speak(text,voice,model,speed,volume)': { endpoint:'/api/speak', payload:{ text:'Hello from SDK visualizer', voice:'en_US', model:'', speed:1.0, volume:1.0 } },");
            sb.AppendLine(@"  'qwen_speak(text,speaker,instruct)': { endpoint:'/api/qwen/speak', payload:{ text:'Hello from Qwen', speaker:'Ryan', instruct:'friendly' } },");
            sb.AppendLine(@"  'set_tts_options(voice,model)': { endpoint:'/api/voice', payload:{ action:'set', voice:'en_US' } },");
            sb.AppendLine(@"  'set_tts_model(model)': { endpoint:'/api/voice', payload:{ action:'set_model', model:'' } },");
            sb.AppendLine(@"  'get_llm_prompt()': { endpoint:'/api/llm/prompt', method:'GET', payload:{} },");
            sb.AppendLine(@"  'set_llm_prompt(prompt)': { endpoint:'/api/llm/prompt', payload:{ prompt:'You are a concise coach. Keep replies under 2 sentences.' } },");
            sb.AppendLine(@"  'reset_llm_prompt()': { endpoint:'/api/llm/prompt', payload:{ reset:true } },");
            sb.AppendLine(@"  'describe_camera(prompt,model)': { endpoint:'/api/vision/describe', payload:{ prompt:'Describe what you see in the current camera frame.', model:'gemma3:4b' } },");
            sb.AppendLine(@"  'launch_game(name)': { endpoint:'/api/game', payload:{ action:'launch', name:'cornhole' } },");
            sb.AppendLine(@"  'exit_game()': { endpoint:'/api/game', payload:{ action:'exit' } },");
            sb.AppendLine(@"  'face_preset(mode,seconds)': { endpoint:'/api/face', payload:{ mode:'happy', seconds:3 } },");
            sb.AppendLine(@"  'flower_open()': { endpoint:'/api/flower', payload:{ action:'open' } },");
            sb.AppendLine(@"  'led_breathe(color,brightness,period,duration)': { endpoint:'/api/led', payload:{ mode:'breathe', color:'#00BFFF', brightness:0.8, period:2, duration:2 } }");
            sb.AppendLine(@"};");
            sb.AppendLine(@"const methodEl = document.getElementById('sdkMethod');");
            sb.AppendLine(@"const endpointEl = document.getElementById('sdkEndpoint');");
            sb.AppendLine(@"const payloadEl = document.getElementById('sdkPayload');");
            sb.AppendLine(@"const statusEl = document.getElementById('sdkStatus');");
            sb.AppendLine(@"const respEl = document.getElementById('sdkResp');");
            sb.AppendLine(@"const methods = Object.keys(sdkMap);");
            sb.AppendLine(@"const flowTemplatesEl = document.getElementById('flowTemplates');");
            sb.AppendLine(@"const flowCanvasEl = document.getElementById('flowCanvas');");
            sb.AppendLine(@"const flowEditorEl = document.getElementById('flowEditor');");
            sb.AppendLine(@"const flowLogEl = document.getElementById('flowLog');");
            sb.AppendLine(@"const flowJsonEl = document.getElementById('flowJson');");
            sb.AppendLine(@"const flowStatusEl = document.getElementById('flowStatus');");
            sb.AppendLine(@"const flowRunBtn = document.getElementById('flowRunBtn');");
            sb.AppendLine(@"const flowStopBtn = document.getElementById('flowStopBtn');");
            sb.AppendLine(@"const flowClearBtn = document.getElementById('flowClearBtn');");
            sb.AppendLine(@"const flowExportBtn = document.getElementById('flowExportBtn');");
            sb.AppendLine(@"const flowImportBtn = document.getElementById('flowImportBtn');");
            sb.AppendLine(@"let flowSteps = [];");
            sb.AppendLine(@"let flowSelectedId = '';");
            sb.AppendLine(@"let flowRunToken = 0;");
            sb.AppendLine(@"let flowRunning = false;");
            sb.AppendLine(@"let dragTemplateKey = '';");
            sb.AppendLine(@"let dragStepId = '';");
            sb.AppendLine(@"methodEl.innerHTML = methods.map(m => `<option value=""${m}"">${m}</option>`).join('');");
            sb.AppendLine(@"function syncMethod(){");
            sb.AppendLine(@"  const m = methodEl.value;");
            sb.AppendLine(@"  const cfg = sdkMap[m];");
            sb.AppendLine(@"  endpointEl.value = cfg.endpoint;");
            sb.AppendLine(@"  payloadEl.value = JSON.stringify(cfg.payload, null, 2);");
            sb.AppendLine(@"  respEl.textContent = '';");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function resetPayload(){ syncMethod(); }");
            sb.AppendLine(@"function cloneValue(v){ return JSON.parse(JSON.stringify(v)); }");
            sb.AppendLine(@"function makeStepId(){ return 'step_' + Math.random().toString(36).slice(2, 10); }");
            sb.AppendLine(@"function escapeHtml(value){");
            sb.AppendLine(@"  return String(value || '')");
            sb.AppendLine(@"    .replace(/&/g, '&amp;')");
            sb.AppendLine(@"    .replace(/</g, '&lt;')");
            sb.AppendLine(@"    .replace(/>/g, '&gt;')");
            sb.AppendLine(@"    .replace(/""/g, '&quot;')");
            sb.AppendLine(@"    .replace(/'/g, '&#39;');");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function invokeSdk(){");
            sb.AppendLine(@"  const cfg = sdkMap[methodEl.value] || { endpoint:endpointEl.value, payload:{} };");
            sb.AppendLine(@"  const endpoint = endpointEl.value;");
            sb.AppendLine(@"  const httpMethod = String(cfg.method || 'POST').toUpperCase();");
            sb.AppendLine(@"  let payload = {};");
            sb.AppendLine(@"  try { payload = JSON.parse(payloadEl.value || '{}'); }");
            sb.AppendLine(@"  catch(err){ statusEl.textContent = 'Invalid JSON: ' + err; return; }");
            sb.AppendLine(@"  statusEl.textContent = httpMethod + ' ' + endpoint + ' ...';");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const req = httpMethod === 'GET'");
            sb.AppendLine(@"      ? { method:'GET' }");
            sb.AppendLine(@"      : { method:httpMethod, headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) };");
            sb.AppendLine(@"    const resp = await fetch(endpoint, req);");
            sb.AppendLine(@"    const txt = await resp.text();");
            sb.AppendLine(@"    statusEl.textContent = `HTTP ${resp.status} ${resp.statusText}`;");
            sb.AppendLine(@"    try { respEl.textContent = JSON.stringify(JSON.parse(txt), null, 2); }");
            sb.AppendLine(@"    catch { respEl.textContent = txt; }");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'Request failed: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function callEndpoint(endpoint, httpMethod, payload){");
            sb.AppendLine(@"  const method = String(httpMethod || 'POST').toUpperCase();");
            sb.AppendLine(@"  const req = method === 'GET'");
            sb.AppendLine(@"    ? { method:'GET' }");
            sb.AppendLine(@"    : { method:method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload || {}) };");
            sb.AppendLine(@"  const resp = await fetch(endpoint, req);");
            sb.AppendLine(@"  const raw = await resp.text();");
            sb.AppendLine(@"  let json = null;");
            sb.AppendLine(@"  try { json = JSON.parse(raw); } catch (err) { }");
            sb.AppendLine(@"  return { ok:resp.ok, status:resp.status, statusText:resp.statusText, raw:raw, json:json };");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function buildTemplateCatalog(){");
            sb.AppendLine(@"  const apiTemplates = methods.map(m => ({ key:'api:' + m, label:m, kind:'api' }));");
            sb.AppendLine(@"  apiTemplates.push({ key:'util:delay', label:'delay(ms)', kind:'delay' });");
            sb.AppendLine(@"  apiTemplates.push({ key:'util:condition', label:'condition(expr)', kind:'condition' });");
            sb.AppendLine(@"  apiTemplates.push({ key:'util:keyword_wait', label:'wait_keyword(keyword)', kind:'condition' });");
            sb.AppendLine(@"  return apiTemplates;");
            sb.AppendLine(@"}");
            sb.AppendLine(@"const templateCatalog = buildTemplateCatalog();");
            sb.AppendLine(@"function createStepFromTemplate(key){");
            sb.AppendLine(@"  if(!key) return null;");
            sb.AppendLine(@"  if(key.startsWith('api:')){");
            sb.AppendLine(@"    const methodName = key.slice(4);");
            sb.AppendLine(@"    const cfg = sdkMap[methodName];");
            sb.AppendLine(@"    if(!cfg) return null;");
            sb.AppendLine(@"    return {");
            sb.AppendLine(@"      id: makeStepId(),");
            sb.AppendLine(@"      type: 'api',");
            sb.AppendLine(@"      name: methodName,");
            sb.AppendLine(@"      endpoint: cfg.endpoint,");
            sb.AppendLine(@"      method: String(cfg.method || 'POST').toUpperCase(),");
            sb.AppendLine(@"      payload: cloneValue(cfg.payload || {}),");
            sb.AppendLine(@"      continueOnError: false");
            sb.AppendLine(@"    };");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(key === 'util:delay'){");
            sb.AppendLine(@"    return { id: makeStepId(), type:'delay', name:'delay(ms)', delayMs:600 };");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(key === 'util:condition'){");
            sb.AppendLine(@"    return { id: makeStepId(), type:'condition', name:'condition(expr)', expression:'ctx.lastStatus >= 200 && ctx.lastStatus < 300' };");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(key === 'util:keyword_wait'){");
            sb.AppendLine(@"    return { id: makeStepId(), type:'keyword_wait', name:'wait_keyword', keyword:'thanks', timeoutMs:12000, pollMs:350, source:'user', caseSensitive:false, onlyNew:true };");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  return null;");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function exportFlowData(){");
            sb.AppendLine(@"  return flowSteps.map(step => {");
            sb.AppendLine(@"    if(step.type === 'api'){");
            sb.AppendLine(@"      return { type:'api', name:step.name || '', endpoint:step.endpoint || '', method:step.method || 'POST', payload:cloneValue(step.payload || {}), continueOnError:!!step.continueOnError };");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    if(step.type === 'delay'){");
            sb.AppendLine(@"      return { type:'delay', name:step.name || 'delay(ms)', delayMs:Math.max(0, Number(step.delayMs) || 0) };");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    if(step.type === 'condition'){");
            sb.AppendLine(@"      return { type:'condition', name:step.name || 'condition(expr)', expression:String(step.expression || '').trim() };");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    if(step.type === 'keyword_wait'){");
            sb.AppendLine(@"      return { type:'keyword_wait', name:step.name || 'wait_keyword', keyword:String(step.keyword || ''), timeoutMs:Math.max(100, Number(step.timeoutMs) || 12000), pollMs:Math.max(100, Number(step.pollMs) || 350), source:String(step.source || 'user'), caseSensitive:!!step.caseSensitive, onlyNew:step.onlyNew !== false };");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    return { type:'unknown' };");
            sb.AppendLine(@"  });");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function syncFlowJson(){ flowJsonEl.value = JSON.stringify(exportFlowData(), null, 2); }");
            sb.AppendLine(@"function appendFlowLog(line){");
            sb.AppendLine(@"  const ts = new Date().toLocaleTimeString();");
            sb.AppendLine(@"  const nextLine = `[${ts}] ${line}`;");
            sb.AppendLine(@"  if(!flowLogEl.textContent){ flowLogEl.textContent = nextLine; }");
            sb.AppendLine(@"  else { flowLogEl.textContent += '\n' + nextLine; }");
            sb.AppendLine(@"  flowLogEl.scrollTop = flowLogEl.scrollHeight;");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function moveStepBefore(stepId, targetId){");
            sb.AppendLine(@"  const srcIndex = flowSteps.findIndex(s => s.id === stepId);");
            sb.AppendLine(@"  const targetIndex = flowSteps.findIndex(s => s.id === targetId);");
            sb.AppendLine(@"  if(srcIndex < 0 || targetIndex < 0 || srcIndex === targetIndex) return;");
            sb.AppendLine(@"  const src = flowSteps[srcIndex];");
            sb.AppendLine(@"  flowSteps.splice(srcIndex, 1);");
            sb.AppendLine(@"  const nextTargetIndex = flowSteps.findIndex(s => s.id === targetId);");
            sb.AppendLine(@"  flowSteps.splice(nextTargetIndex, 0, src);");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function moveStepToEnd(stepId){");
            sb.AppendLine(@"  const idx = flowSteps.findIndex(s => s.id === stepId);");
            sb.AppendLine(@"  if(idx < 0) return;");
            sb.AppendLine(@"  const src = flowSteps[idx];");
            sb.AppendLine(@"  flowSteps.splice(idx, 1);");
            sb.AppendLine(@"  flowSteps.push(src);");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function keywordMatch(text, keyword, caseSensitive){");
            sb.AppendLine(@"  const src = String(text || '');");
            sb.AppendLine(@"  const kw = String(keyword || '').trim();");
            sb.AppendLine(@"  if(!kw) return false;");
            sb.AppendLine(@"  if(caseSensitive) return src.includes(kw);");
            sb.AppendLine(@"  return src.toLowerCase().includes(kw.toLowerCase());");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function fetchLatestLogEntry(sourceMode){");
            sb.AppendLine(@"  const resp = await fetch('/api/logs');");
            sb.AppendLine(@"  if(!resp.ok) return null;");
            sb.AppendLine(@"  const data = await resp.json();");
            sb.AppendLine(@"  const entries = Array.isArray(data.entries) ? data.entries : [];");
            sb.AppendLine(@"  const mode = String(sourceMode || 'user').toLowerCase();");
            sb.AppendLine(@"  for(let i = entries.length - 1; i >= 0; i--){");
            sb.AppendLine(@"    const e = entries[i] || {};");
            sb.AppendLine(@"    const role = String(e.role || '').toLowerCase();");
            sb.AppendLine(@"    const source = String(e.source || '').toLowerCase();");
            sb.AppendLine(@"    const message = String(e.message || e.text || '').trim();");
            sb.AppendLine(@"    if(!message) continue;");
            sb.AppendLine(@"    if(mode === 'user'){");
            sb.AppendLine(@"      const isUserLike = role === 'user' || source.includes('whisper') || source.includes('voice');");
            sb.AppendLine(@"      if(!isUserLike) continue;");
            sb.AppendLine(@"    } else if(mode === 'coach'){");
            sb.AppendLine(@"      const isCoachLike = role === 'coach' || source.includes('dialog');");
            sb.AppendLine(@"      if(!isCoachLike) continue;");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    const ts = String(e.timestamp || '');");
            sb.AppendLine(@"    return { text:message, role:role, source:source, timestamp:ts, key:ts + '|' + message };");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  return null;");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function renderTemplates(){");
            sb.AppendLine(@"  flowTemplatesEl.innerHTML = templateCatalog.map(t => `<div class=""template-item ${t.kind === 'api' ? '' : 'utility'}"" draggable=""true"" data-template=""${t.key}"">${escapeHtml(t.label)}</div>`).join('');");
            sb.AppendLine(@"  flowTemplatesEl.querySelectorAll('.template-item').forEach(el => {");
            sb.AppendLine(@"    el.addEventListener('dragstart', ev => {");
            sb.AppendLine(@"      dragTemplateKey = ev.currentTarget.getAttribute('data-template') || '';");
            sb.AppendLine(@"      dragStepId = '';");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"  });");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function renderFlowCanvas(){");
            sb.AppendLine(@"  if(!flowSteps.length){");
            sb.AppendLine(@"    flowCanvasEl.innerHTML = '<div class=""flow-empty"">Drop templates here to build a flow.</div>';");
            sb.AppendLine(@"    syncFlowJson();");
            sb.AppendLine(@"    renderStepEditor();");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  flowCanvasEl.innerHTML = flowSteps.map((step, index) => {");
            sb.AppendLine(@"    const cls = ['flow-step'];");
            sb.AppendLine(@"    if(step.id === flowSelectedId) cls.push('selected');");
            sb.AppendLine(@"    if(step._state === 'ok') cls.push('ok');");
            sb.AppendLine(@"    if(step._state === 'error') cls.push('error');");
            sb.AppendLine(@"    if(step._state === 'running') cls.push('running');");
            sb.AppendLine(@"    const chipClass = step.type === 'api' ? 'api' : (step.type === 'delay' ? 'delay' : (step.type === 'keyword_wait' ? 'keyword' : 'cond'));");
            sb.AppendLine(@"    const title = step.type === 'api' ? step.name : (step.name || step.type);");
            sb.AppendLine(@"    const summary = step.type === 'api' ? (step.endpoint || '') : (step.type === 'condition' ? (step.expression || '') : (step.type === 'keyword_wait' ? (`keyword=""${step.keyword || ''}"" timeout=${Number(step.timeoutMs || 12000)}ms`) : (String(step.delayMs || 0) + ' ms')));");
            sb.AppendLine(@"    return `<div class=""${cls.join(' ')}"" draggable=""true"" data-step-id=""${step.id}""><div class=""flow-head""><div><span class=""flow-chip ${chipClass}"">${step.type}</span> <span class=""flow-title"">${index + 1}. ${escapeHtml(title)}</span></div><div class=""flow-actions""><button type=""button"" data-action=""select"" data-id=""${step.id}"">Edit</button><button type=""button"" data-action=""up"" data-id=""${step.id}"">Up</button><button type=""button"" data-action=""down"" data-id=""${step.id}"">Down</button><button type=""button"" data-action=""delete"" data-id=""${step.id}"">Delete</button></div></div><div class=""flow-small"">${escapeHtml(summary)}</div></div>`;");
            sb.AppendLine(@"  }).join('');");
            sb.AppendLine(@"  flowCanvasEl.querySelectorAll('.flow-step').forEach(el => {");
            sb.AppendLine(@"    el.addEventListener('dragstart', ev => {");
            sb.AppendLine(@"      dragStepId = ev.currentTarget.getAttribute('data-step-id') || '';");
            sb.AppendLine(@"      dragTemplateKey = '';");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    el.addEventListener('dragover', ev => ev.preventDefault());");
            sb.AppendLine(@"    el.addEventListener('drop', ev => {");
            sb.AppendLine(@"      ev.preventDefault();");
            sb.AppendLine(@"      ev.stopPropagation();");
            sb.AppendLine(@"      const targetId = ev.currentTarget.getAttribute('data-step-id');");
            sb.AppendLine(@"      if(dragTemplateKey){");
            sb.AppendLine(@"        const step = createStepFromTemplate(dragTemplateKey);");
            sb.AppendLine(@"        if(step){");
            sb.AppendLine(@"          const idx = flowSteps.findIndex(s => s.id === targetId);");
            sb.AppendLine(@"          const insertAt = idx < 0 ? flowSteps.length : idx;");
            sb.AppendLine(@"          flowSteps.splice(insertAt, 0, step);");
            sb.AppendLine(@"          flowSelectedId = step.id;");
            sb.AppendLine(@"          renderFlowCanvas();");
            sb.AppendLine(@"        }");
            sb.AppendLine(@"      } else if(dragStepId && dragStepId !== targetId){");
            sb.AppendLine(@"        moveStepBefore(dragStepId, targetId);");
            sb.AppendLine(@"        renderFlowCanvas();");
            sb.AppendLine(@"      }");
            sb.AppendLine(@"      dragTemplateKey = '';");
            sb.AppendLine(@"      dragStepId = '';");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"  });");
            sb.AppendLine(@"  syncFlowJson();");
            sb.AppendLine(@"  renderStepEditor();");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function renderStepEditor(){");
            sb.AppendLine(@"  const step = flowSteps.find(s => s.id === flowSelectedId);");
            sb.AppendLine(@"  if(!step){");
            sb.AppendLine(@"    flowEditorEl.innerHTML = '<div class=""flow-empty"">Select a step to edit.</div>';");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(step.type === 'api'){");
            sb.AppendLine(@"    flowEditorEl.innerHTML = `<div class=""flow-editor""><div class=""flow-small"">Type: API</div><label>Step Name</label><input id=""editName"" value=""${escapeHtml(step.name || '')}""><label>Endpoint</label><input id=""editEndpoint"" value=""${escapeHtml(step.endpoint || '')}""><label>HTTP Method</label><select id=""editMethod""><option value=""POST"">POST</option><option value=""GET"">GET</option></select><div class=""flow-inline""><input type=""checkbox"" id=""editContinue""><label for=""editContinue"" style=""margin:0;"">Continue on error</label></div><label>Payload JSON</label><textarea id=""editPayload""></textarea><div id=""editMsg"" class=""flow-small mono""></div></div>`;");
            sb.AppendLine(@"    const nameEl = document.getElementById('editName');");
            sb.AppendLine(@"    const endpointEditEl = document.getElementById('editEndpoint');");
            sb.AppendLine(@"    const methodEditEl = document.getElementById('editMethod');");
            sb.AppendLine(@"    const continueEl = document.getElementById('editContinue');");
            sb.AppendLine(@"    const payloadEditEl = document.getElementById('editPayload');");
            sb.AppendLine(@"    const msgEl = document.getElementById('editMsg');");
            sb.AppendLine(@"    methodEditEl.value = String(step.method || 'POST').toUpperCase();");
            sb.AppendLine(@"    continueEl.checked = !!step.continueOnError;");
            sb.AppendLine(@"    payloadEditEl.value = JSON.stringify(step.payload || {}, null, 2);");
            sb.AppendLine(@"    nameEl.addEventListener('input', () => { step.name = nameEl.value; renderFlowCanvas(); });");
            sb.AppendLine(@"    endpointEditEl.addEventListener('input', () => { step.endpoint = endpointEditEl.value; syncFlowJson(); renderFlowCanvas(); });");
            sb.AppendLine(@"    methodEditEl.addEventListener('change', () => { step.method = methodEditEl.value; syncFlowJson(); renderFlowCanvas(); });");
            sb.AppendLine(@"    continueEl.addEventListener('change', () => { step.continueOnError = continueEl.checked; syncFlowJson(); });");
            sb.AppendLine(@"    payloadEditEl.addEventListener('change', () => {");
            sb.AppendLine(@"      try {");
            sb.AppendLine(@"        step.payload = JSON.parse(payloadEditEl.value || '{}');");
            sb.AppendLine(@"        msgEl.textContent = 'Payload JSON valid.';");
            sb.AppendLine(@"        syncFlowJson();");
            sb.AppendLine(@"      } catch(err){");
            sb.AppendLine(@"        msgEl.textContent = 'Invalid JSON: ' + err;");
            sb.AppendLine(@"      }");
            sb.AppendLine(@"    });");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(step.type === 'delay'){");
            sb.AppendLine(@"    flowEditorEl.innerHTML = `<div class=""flow-editor""><div class=""flow-small"">Type: Delay</div><label>Step Name</label><input id=""editDelayName"" value=""${escapeHtml(step.name || 'delay(ms)')}""><label>Delay (ms)</label><input id=""editDelayMs"" type=""number"" min=""0"" step=""10"" value=""${Number(step.delayMs || 0)}""></div>`;");
            sb.AppendLine(@"    const delayNameEl = document.getElementById('editDelayName');");
            sb.AppendLine(@"    const delayMsEl = document.getElementById('editDelayMs');");
            sb.AppendLine(@"    delayNameEl.addEventListener('input', () => { step.name = delayNameEl.value; renderFlowCanvas(); });");
            sb.AppendLine(@"    delayMsEl.addEventListener('input', () => { step.delayMs = Math.max(0, Number(delayMsEl.value) || 0); syncFlowJson(); renderFlowCanvas(); });");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(step.type === 'keyword_wait'){");
            sb.AppendLine(@"    flowEditorEl.innerHTML = `<div class=""flow-editor""><div class=""flow-small"">Type: Wait Keyword</div><label>Step Name</label><input id=""editKeywordName"" value=""${escapeHtml(step.name || 'wait_keyword')}""><label>Keyword</label><input id=""editKeywordValue"" value=""${escapeHtml(step.keyword || '')}""><label>Source</label><select id=""editKeywordSource""><option value=""user"">User Speech</option><option value=""coach"">Coach Reply</option><option value=""any"">Any Message</option></select><label>Timeout (ms)</label><input id=""editKeywordTimeout"" type=""number"" min=""100"" step=""100"" value=""${Number(step.timeoutMs || 12000)}""><label>Poll Interval (ms)</label><input id=""editKeywordPoll"" type=""number"" min=""100"" step=""50"" value=""${Number(step.pollMs || 350)}""><div class=""flow-inline""><input type=""checkbox"" id=""editKeywordCase""><label for=""editKeywordCase"" style=""margin:0;"">Case sensitive</label></div><div class=""flow-inline""><input type=""checkbox"" id=""editKeywordOnlyNew""><label for=""editKeywordOnlyNew"" style=""margin:0;"">Only match new recognized text</label></div></div>`;");
            sb.AppendLine(@"    const nameEl = document.getElementById('editKeywordName');");
            sb.AppendLine(@"    const valueEl = document.getElementById('editKeywordValue');");
            sb.AppendLine(@"    const sourceEl = document.getElementById('editKeywordSource');");
            sb.AppendLine(@"    const timeoutEl = document.getElementById('editKeywordTimeout');");
            sb.AppendLine(@"    const pollEl = document.getElementById('editKeywordPoll');");
            sb.AppendLine(@"    const caseEl = document.getElementById('editKeywordCase');");
            sb.AppendLine(@"    const onlyNewEl = document.getElementById('editKeywordOnlyNew');");
            sb.AppendLine(@"    sourceEl.value = String(step.source || 'user').toLowerCase();");
            sb.AppendLine(@"    caseEl.checked = !!step.caseSensitive;");
            sb.AppendLine(@"    onlyNewEl.checked = step.onlyNew !== false;");
            sb.AppendLine(@"    nameEl.addEventListener('input', () => { step.name = nameEl.value; renderFlowCanvas(); });");
            sb.AppendLine(@"    valueEl.addEventListener('input', () => { step.keyword = valueEl.value; syncFlowJson(); renderFlowCanvas(); });");
            sb.AppendLine(@"    sourceEl.addEventListener('change', () => { step.source = sourceEl.value; syncFlowJson(); renderFlowCanvas(); });");
            sb.AppendLine(@"    timeoutEl.addEventListener('input', () => { step.timeoutMs = Math.max(100, Number(timeoutEl.value) || 12000); syncFlowJson(); renderFlowCanvas(); });");
            sb.AppendLine(@"    pollEl.addEventListener('input', () => { step.pollMs = Math.max(100, Number(pollEl.value) || 350); syncFlowJson(); renderFlowCanvas(); });");
            sb.AppendLine(@"    caseEl.addEventListener('change', () => { step.caseSensitive = caseEl.checked; syncFlowJson(); });");
            sb.AppendLine(@"    onlyNewEl.addEventListener('change', () => { step.onlyNew = onlyNewEl.checked; syncFlowJson(); });");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  flowEditorEl.innerHTML = `<div class=""flow-editor""><div class=""flow-small"">Type: Condition</div><label>Step Name</label><input id=""editCondName"" value=""${escapeHtml(step.name || 'condition(expr)')}""><label>Expression (JS)</label><textarea id=""editConditionExpr"">${escapeHtml(step.expression || '')}</textarea><div class=""flow-small"">Use <span class=""mono"">ctx.lastStatus</span>, <span class=""mono"">ctx.lastJson</span>, <span class=""mono"">ctx.lastRaw</span>, <span class=""mono"">ctx.lastRecognized</span>.</div></div>`;");
            sb.AppendLine(@"  const condNameEl = document.getElementById('editCondName');");
            sb.AppendLine(@"  const condExprEl = document.getElementById('editConditionExpr');");
            sb.AppendLine(@"  condNameEl.addEventListener('input', () => { step.name = condNameEl.value; renderFlowCanvas(); });");
            sb.AppendLine(@"  condExprEl.addEventListener('change', () => { step.expression = condExprEl.value; syncFlowJson(); renderFlowCanvas(); });");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function evaluateCondition(expr, ctx){");
            sb.AppendLine(@"  const source = String(expr || '').trim();");
            sb.AppendLine(@"  if(!source){ return true; }");
            sb.AppendLine(@"  const body = source.includes('return') ? source : `return (${source});`;");
            sb.AppendLine(@"  const fn = new Function('ctx', body);");
            sb.AppendLine(@"  return !!fn(ctx);");
            sb.AppendLine(@"}");
            sb.AppendLine(@"async function runFlow(){");
            sb.AppendLine(@"  if(flowRunning){ flowStatusEl.textContent = 'Flow already running.'; return; }");
            sb.AppendLine(@"  if(!flowSteps.length){ flowStatusEl.textContent = 'No steps in flow.'; return; }");
            sb.AppendLine(@"  flowRunning = true;");
            sb.AppendLine(@"  flowRunToken += 1;");
            sb.AppendLine(@"  const token = flowRunToken;");
            sb.AppendLine(@"  flowStatusEl.textContent = 'Running...';");
            sb.AppendLine(@"  appendFlowLog('Flow started with ' + flowSteps.length + ' steps.');");
            sb.AppendLine(@"  flowSteps.forEach(s => { s._state = ''; });");
            sb.AppendLine(@"  const ctx = { lastStatus:0, lastJson:null, lastRaw:'', lastRecognized:'', lastRecognizedEntry:null };");
            sb.AppendLine(@"  for(let i = 0; i < flowSteps.length; i++){");
            sb.AppendLine(@"    if(token !== flowRunToken){");
            sb.AppendLine(@"      flowStatusEl.textContent = 'Stopped.';");
            sb.AppendLine(@"      appendFlowLog('Flow stopped.');");
            sb.AppendLine(@"      flowRunning = false;");
            sb.AppendLine(@"      renderFlowCanvas();");
            sb.AppendLine(@"      return;");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    const step = flowSteps[i];");
            sb.AppendLine(@"    step._state = 'running';");
            sb.AppendLine(@"    flowSelectedId = step.id;");
            sb.AppendLine(@"    renderFlowCanvas();");
            sb.AppendLine(@"    try {");
            sb.AppendLine(@"      if(step.type === 'api'){");
            sb.AppendLine(@"        const result = await callEndpoint(step.endpoint, step.method, step.payload);");
            sb.AppendLine(@"        ctx.lastStatus = result.status;");
            sb.AppendLine(@"        ctx.lastJson = result.json;");
            sb.AppendLine(@"        ctx.lastRaw = result.raw;");
            sb.AppendLine(@"        if(result.ok){");
            sb.AppendLine(@"          step._state = 'ok';");
            sb.AppendLine(@"          appendFlowLog(`Step ${i + 1} OK: ${step.method} ${step.endpoint} -> HTTP ${result.status}`);");
            sb.AppendLine(@"        } else if(step.continueOnError){");
            sb.AppendLine(@"          step._state = 'error';");
            sb.AppendLine(@"          appendFlowLog(`Step ${i + 1} HTTP ${result.status} but continueOnError=true.`);");
            sb.AppendLine(@"        } else {");
            sb.AppendLine(@"          throw new Error(`HTTP ${result.status} ${result.statusText}`);");
            sb.AppendLine(@"        }");
            sb.AppendLine(@"      } else if(step.type === 'delay'){");
            sb.AppendLine(@"        const ms = Math.max(0, Number(step.delayMs) || 0);");
            sb.AppendLine(@"        await new Promise(resolve => setTimeout(resolve, ms));");
            sb.AppendLine(@"        if(token !== flowRunToken){ throw new Error('Stopped'); }");
            sb.AppendLine(@"        step._state = 'ok';");
            sb.AppendLine(@"        appendFlowLog(`Step ${i + 1} OK: delay ${ms}ms`);");
            sb.AppendLine(@"      } else if(step.type === 'keyword_wait'){");
            sb.AppendLine(@"        const keyword = String(step.keyword || '').trim();");
            sb.AppendLine(@"        const timeoutMs = Math.max(100, Number(step.timeoutMs) || 12000);");
            sb.AppendLine(@"        const pollMs = Math.max(100, Number(step.pollMs) || 350);");
            sb.AppendLine(@"        const source = String(step.source || 'user').toLowerCase();");
            sb.AppendLine(@"        const caseSensitive = !!step.caseSensitive;");
            sb.AppendLine(@"        const onlyNew = step.onlyNew !== false;");
            sb.AppendLine(@"        if(!keyword){ throw new Error('keyword required'); }");
            sb.AppendLine(@"        let baselineKey = '';");
            sb.AppendLine(@"        try {");
            sb.AppendLine(@"          const baseline = await fetchLatestLogEntry(source);");
            sb.AppendLine(@"          baselineKey = baseline ? baseline.key : '';");
            sb.AppendLine(@"        } catch(err) { }");
            sb.AppendLine(@"        const waitStart = Date.now();");
            sb.AppendLine(@"        let matched = null;");
            sb.AppendLine(@"        appendFlowLog(`Step ${i + 1} waiting keyword: ${keyword}`);");
            sb.AppendLine(@"        while((Date.now() - waitStart) <= timeoutMs){");
            sb.AppendLine(@"          if(token !== flowRunToken){ throw new Error('Stopped'); }");
            sb.AppendLine(@"          try {");
            sb.AppendLine(@"            const latest = await fetchLatestLogEntry(source);");
            sb.AppendLine(@"            if(latest){");
            sb.AppendLine(@"              const isFresh = !onlyNew || latest.key !== baselineKey;");
            sb.AppendLine(@"              if(isFresh && keywordMatch(latest.text, keyword, caseSensitive)){");
            sb.AppendLine(@"                matched = latest;");
            sb.AppendLine(@"                break;");
            sb.AppendLine(@"              }");
            sb.AppendLine(@"            }");
            sb.AppendLine(@"          } catch(err) { }");
            sb.AppendLine(@"          await new Promise(resolve => setTimeout(resolve, pollMs));");
            sb.AppendLine(@"        }");
            sb.AppendLine(@"        if(!matched){ throw new Error(`Keyword not detected within ${timeoutMs}ms: ${keyword}`); }");
            sb.AppendLine(@"        ctx.lastRecognized = matched.text;");
            sb.AppendLine(@"        ctx.lastRecognizedEntry = matched;");
            sb.AppendLine(@"        step._state = 'ok';");
            sb.AppendLine(@"        appendFlowLog(`Step ${i + 1} OK: keyword matched -> ${matched.text}`);");
            sb.AppendLine(@"      } else if(step.type === 'condition'){");
            sb.AppendLine(@"        const pass = evaluateCondition(step.expression, ctx);");
            sb.AppendLine(@"        if(!pass){ throw new Error('Condition returned false'); }");
            sb.AppendLine(@"        step._state = 'ok';");
            sb.AppendLine(@"        appendFlowLog(`Step ${i + 1} OK: condition true`);");
            sb.AppendLine(@"      } else {");
            sb.AppendLine(@"        throw new Error('Unknown step type: ' + step.type);");
            sb.AppendLine(@"      }");
            sb.AppendLine(@"    } catch(err){");
            sb.AppendLine(@"      if(String(err) === 'Error: Stopped'){");
            sb.AppendLine(@"        flowStatusEl.textContent = 'Stopped.';");
            sb.AppendLine(@"        flowRunning = false;");
            sb.AppendLine(@"        renderFlowCanvas();");
            sb.AppendLine(@"        return;");
            sb.AppendLine(@"      }");
            sb.AppendLine(@"      if(step.type === 'api' && step.continueOnError){");
            sb.AppendLine(@"        step._state = 'error';");
            sb.AppendLine(@"        appendFlowLog(`Step ${i + 1} error ignored: ${err}`);");
            sb.AppendLine(@"      } else {");
            sb.AppendLine(@"        step._state = 'error';");
            sb.AppendLine(@"        flowStatusEl.textContent = `Failed at step ${i + 1}: ${err}`;");
            sb.AppendLine(@"        appendFlowLog(`Flow failed at step ${i + 1}: ${err}`);");
            sb.AppendLine(@"        flowRunning = false;");
            sb.AppendLine(@"        renderFlowCanvas();");
            sb.AppendLine(@"        return;");
            sb.AppendLine(@"      }");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    renderFlowCanvas();");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  flowStatusEl.textContent = 'Completed.';");
            sb.AppendLine(@"  appendFlowLog('Flow completed.');");
            sb.AppendLine(@"  flowRunning = false;");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function stopFlow(){");
            sb.AppendLine(@"  if(!flowRunning){ flowStatusEl.textContent = 'Flow is not running.'; return; }");
            sb.AppendLine(@"  flowRunToken += 1;");
            sb.AppendLine(@"  flowRunning = false;");
            sb.AppendLine(@"  flowStatusEl.textContent = 'Stop requested...';");
            sb.AppendLine(@"  appendFlowLog('Stop requested.');");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function clearFlow(){");
            sb.AppendLine(@"  if(flowRunning){ flowStatusEl.textContent = 'Stop flow first.'; return; }");
            sb.AppendLine(@"  flowSteps = [];");
            sb.AppendLine(@"  flowSelectedId = '';");
            sb.AppendLine(@"  flowLogEl.textContent = '';");
            sb.AppendLine(@"  flowStatusEl.textContent = 'Flow cleared.';");
            sb.AppendLine(@"  renderFlowCanvas();");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function exportFlow(){");
            sb.AppendLine(@"  syncFlowJson();");
            sb.AppendLine(@"  flowStatusEl.textContent = 'Flow exported to JSON box.';");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function importFlow(){");
            sb.AppendLine(@"  if(flowRunning){ flowStatusEl.textContent = 'Stop flow first.'; return; }");
            sb.AppendLine(@"  let data;");
            sb.AppendLine(@"  try { data = JSON.parse(flowJsonEl.value || '[]'); }");
            sb.AppendLine(@"  catch(err){ flowStatusEl.textContent = 'Import failed: invalid JSON'; return; }");
            sb.AppendLine(@"  if(!Array.isArray(data)){ flowStatusEl.textContent = 'Import failed: JSON root must be array'; return; }");
            sb.AppendLine(@"  const nextSteps = [];");
            sb.AppendLine(@"  for(let i = 0; i < data.length; i++){");
            sb.AppendLine(@"    const raw = data[i] || {};");
            sb.AppendLine(@"    if(raw.type === 'api'){");
            sb.AppendLine(@"      nextSteps.push({");
            sb.AppendLine(@"        id: makeStepId(),");
            sb.AppendLine(@"        type: 'api',");
            sb.AppendLine(@"        name: String(raw.name || 'api'),");
            sb.AppendLine(@"        endpoint: String(raw.endpoint || ''),");
            sb.AppendLine(@"        method: String(raw.method || 'POST').toUpperCase(),");
            sb.AppendLine(@"        payload: (raw.payload && typeof raw.payload === 'object') ? raw.payload : {},");
            sb.AppendLine(@"        continueOnError: !!raw.continueOnError");
            sb.AppendLine(@"      });");
            sb.AppendLine(@"      continue;");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    if(raw.type === 'delay'){");
            sb.AppendLine(@"      nextSteps.push({ id: makeStepId(), type:'delay', name:String(raw.name || 'delay(ms)'), delayMs:Math.max(0, Number(raw.delayMs) || 0) });");
            sb.AppendLine(@"      continue;");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    if(raw.type === 'condition'){");
            sb.AppendLine(@"      nextSteps.push({ id: makeStepId(), type:'condition', name:String(raw.name || 'condition(expr)'), expression:String(raw.expression || '') });");
            sb.AppendLine(@"      continue;");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    if(raw.type === 'keyword_wait'){");
            sb.AppendLine(@"      nextSteps.push({");
            sb.AppendLine(@"        id: makeStepId(),");
            sb.AppendLine(@"        type:'keyword_wait',");
            sb.AppendLine(@"        name:String(raw.name || 'wait_keyword'),");
            sb.AppendLine(@"        keyword:String(raw.keyword || ''),");
            sb.AppendLine(@"        timeoutMs:Math.max(100, Number(raw.timeoutMs) || 12000),");
            sb.AppendLine(@"        pollMs:Math.max(100, Number(raw.pollMs) || 350),");
            sb.AppendLine(@"        source:String(raw.source || 'user').toLowerCase(),");
            sb.AppendLine(@"        caseSensitive:!!raw.caseSensitive,");
            sb.AppendLine(@"        onlyNew:raw.onlyNew !== false");
            sb.AppendLine(@"      });");
            sb.AppendLine(@"      continue;");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"    flowStatusEl.textContent = `Import warning: skipped unknown type at index ${i}`;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  flowSteps = nextSteps;");
            sb.AppendLine(@"  flowSelectedId = flowSteps.length ? flowSteps[0].id : '';");
            sb.AppendLine(@"  flowStatusEl.textContent = `Imported ${flowSteps.length} steps.`;");
            sb.AppendLine(@"  renderFlowCanvas();");
            sb.AppendLine(@"}");
            sb.AppendLine(@"flowCanvasEl.addEventListener('dragover', ev => ev.preventDefault());");
            sb.AppendLine(@"flowCanvasEl.addEventListener('drop', ev => {");
            sb.AppendLine(@"  ev.preventDefault();");
            sb.AppendLine(@"  if(dragTemplateKey){");
            sb.AppendLine(@"    const step = createStepFromTemplate(dragTemplateKey);");
            sb.AppendLine(@"    if(step){");
            sb.AppendLine(@"      flowSteps.push(step);");
            sb.AppendLine(@"      flowSelectedId = step.id;");
            sb.AppendLine(@"      renderFlowCanvas();");
            sb.AppendLine(@"    }");
            sb.AppendLine(@"  } else if(dragStepId){");
            sb.AppendLine(@"    moveStepToEnd(dragStepId);");
            sb.AppendLine(@"    renderFlowCanvas();");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  dragTemplateKey = '';");
            sb.AppendLine(@"  dragStepId = '';");
            sb.AppendLine(@"});");
            sb.AppendLine(@"flowCanvasEl.addEventListener('click', ev => {");
            sb.AppendLine(@"  const btn = ev.target.closest('button[data-action]');");
            sb.AppendLine(@"  if(!btn) return;");
            sb.AppendLine(@"  const action = btn.getAttribute('data-action');");
            sb.AppendLine(@"  const id = btn.getAttribute('data-id');");
            sb.AppendLine(@"  if(!id) return;");
            sb.AppendLine(@"  const idx = flowSteps.findIndex(s => s.id === id);");
            sb.AppendLine(@"  if(idx < 0) return;");
            sb.AppendLine(@"  if(action === 'select'){");
            sb.AppendLine(@"    flowSelectedId = id;");
            sb.AppendLine(@"    renderFlowCanvas();");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(action === 'delete'){");
            sb.AppendLine(@"    flowSteps.splice(idx, 1);");
            sb.AppendLine(@"    if(flowSelectedId === id){ flowSelectedId = flowSteps.length ? flowSteps[Math.max(0, idx - 1)].id : ''; }");
            sb.AppendLine(@"    renderFlowCanvas();");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(action === 'up' && idx > 0){");
            sb.AppendLine(@"    const tmp = flowSteps[idx - 1];");
            sb.AppendLine(@"    flowSteps[idx - 1] = flowSteps[idx];");
            sb.AppendLine(@"    flowSteps[idx] = tmp;");
            sb.AppendLine(@"    renderFlowCanvas();");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(action === 'down' && idx < flowSteps.length - 1){");
            sb.AppendLine(@"    const tmp = flowSteps[idx + 1];");
            sb.AppendLine(@"    flowSteps[idx + 1] = flowSteps[idx];");
            sb.AppendLine(@"    flowSteps[idx] = tmp;");
            sb.AppendLine(@"    renderFlowCanvas();");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"});");
            sb.AppendLine(@"flowRunBtn.addEventListener('click', runFlow);");
            sb.AppendLine(@"flowStopBtn.addEventListener('click', stopFlow);");
            sb.AppendLine(@"flowClearBtn.addEventListener('click', clearFlow);");
            sb.AppendLine(@"flowExportBtn.addEventListener('click', exportFlow);");
            sb.AppendLine(@"flowImportBtn.addEventListener('click', importFlow);");
            sb.AppendLine(@"methodEl.addEventListener('change', syncMethod);");
            sb.AppendLine(@"syncMethod();");
            sb.AppendLine(@"renderTemplates();");
            sb.AppendLine(@"renderFlowCanvas();");
            sb.AppendLine(@"</script>");
            sb.AppendLine(@"</body>");
            sb.AppendLine(@"</html>");
            return sb.ToString();
        }

        private static void AddCorsHeaders(HttpListenerResponse response)
        {
            response.Headers["Access-Control-Allow-Origin"] = "*";
            response.Headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS";
            response.Headers["Access-Control-Allow-Headers"] = "Content-Type";
        }

        private static bool IsHexColor(string value)
        {
            if (string.IsNullOrEmpty(value) || value.Length != 7 || value[0] != '#')
            {
                return false;
            }

            for (int i = 1; i < value.Length; i++)
            {
                var c = value[i];
                var isHex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
                if (!isHex)
                {
                    return false;
                }
            }

            return true;
        }

        private static string NormalizeHex(string value)
        {
            if (string.IsNullOrWhiteSpace(value))
            {
                return "#000000";
            }

            value = value.Trim();
            if (value.Length == 7 && value[0] == '#')
            {
                return value.ToUpperInvariant();
            }

            if (value.Length == 6)
            {
                return "#" + value.ToUpperInvariant();
            }

            return value;
        }

        private void TryStartCamera()
        {
            if (!enableCameraPreview)
            {
                return;
            }
            // If using external texture (e.g., MediaPipe), we don't open WebCamTexture here
            if (useExternalCameraTexture)
            {
                return;
            }

            try
            {
                var availableSources = WebCamTexture.devices;
                if (availableSources == null || availableSources.Length == 0)
                {
                    Debug.LogWarning("[UserTestPanel] No camera devices found for preview");
                    return;
                }

                // 优先尝试 OBS 虚拟摄像头，其次任何名称包含 "virtual" 的设备
                var dev = availableSources.FirstOrDefault(d =>
                    (!string.IsNullOrEmpty(d.name) &&
                     (d.name.IndexOf("obs", StringComparison.OrdinalIgnoreCase) >= 0 ||
                      d.name.IndexOf("virtual", StringComparison.OrdinalIgnoreCase) >= 0)));
                if (string.IsNullOrEmpty(dev.name))
                {
                    dev = availableSources[0];
                }

                if (preferredCameraDeviceIndex >= 0 && preferredCameraDeviceIndex < availableSources.Length)
                {
                    dev = availableSources[preferredCameraDeviceIndex];
                }
                else if (!string.IsNullOrWhiteSpace(preferredCameraDeviceName))
                {
                    var preferredName = preferredCameraDeviceName.Trim();
                    var preferred = availableSources.FirstOrDefault(d =>
                        !string.IsNullOrEmpty(d.name) &&
                        d.name.IndexOf(preferredName, StringComparison.OrdinalIgnoreCase) >= 0);
                    if (!string.IsNullOrEmpty(preferred.name))
                    {
                        dev = preferred;
                    }
                }
                _webcam = new WebCamTexture(dev.name, Mathf.Max(16, cameraWidth), Mathf.Max(16, cameraHeight), Mathf.Max(1, cameraFps));
                _webcam.Play();
                _nextCaptureRealtime = Time.realtimeSinceStartup;
            }
            catch (System.Exception ex)
            {
                Debug.LogWarning($"[UserTestPanel] Failed to start camera: {ex.Message}");
            }
        }

        private void StopCamera()
        {
            try
            {
                if (_webcam != null)
                {
                    if (_webcam.isPlaying) _webcam.Stop();
                    _webcam = null;
                }
            }
            catch (System.Exception) { }

            if (_cameraTexture != null)
            {
                Destroy(_cameraTexture);
                _cameraTexture = null;
            }

            lock (_cameraLock) { _latestJpeg = null; }
        }

        private Texture GetExternalCameraTexture()
        {
            if (externalCameraRawImage != null && externalCameraRawImage.texture != null)
            {
                return externalCameraRawImage.texture;
            }
            if (externalCameraRenderer != null && externalCameraRenderer.material != null)
            {
                var tex = externalCameraRenderer.material.mainTexture;
                if (tex != null) return tex;
            }
            if (Time.realtimeSinceStartup - _externalTextureSearchTs > 1f)
            {
                _externalTextureSearchTs = Time.realtimeSinceStartup;
                var allRawImages = Resources.FindObjectsOfTypeAll<RawImage>();
                if (allRawImages != null && allRawImages.Length > 0)
                {
                    RawImage preferred = null;
                    foreach (var ri in allRawImages)
                    {
                        if (ri == null || ri.texture == null) continue;
                        var go = ri.gameObject;
                        if (go == null || !go.activeInHierarchy) continue;
                        var n = (go.name ?? string.Empty).ToLowerInvariant();
                        if (n.Contains("annotatable") || n.Contains("mediapipe") || n.Contains("screen"))
                        {
                            preferred = ri;
                            break;
                        }
                        if (preferred == null)
                        {
                            preferred = ri;
                        }
                    }
                    if (preferred != null)
                    {
                        externalCameraRawImage = preferred;
                        _hasExternalRawImageBinding = true;
                        return preferred.texture;
                    }
                }

                // Reflection fallback: read texture from MediaPipe image source directly.
                try
                {
                    Texture mpTex = TryGetMediaPipeImageSourceTexture();
                    if (mpTex != null)
                    {
                        return mpTex;
                    }
                }
                catch { }
            }
            return null;
        }

        private static Texture TryGetMediaPipeImageSourceTexture()
        {
            var providerType = FindType("Mediapipe.Unity.Sample.ImageSourceProvider");
            if (providerType == null) return null;
            var imageSourceProp = providerType.GetProperty("ImageSource", BindingFlags.Public | BindingFlags.Static);
            if (imageSourceProp == null) return null;
            var imageSourceObj = imageSourceProp.GetValue(null, null);
            if (imageSourceObj == null) return null;
            var m = imageSourceObj.GetType().GetMethod("GetCurrentTexture", BindingFlags.Public | BindingFlags.Instance);
            if (m == null) return null;
            return m.Invoke(imageSourceObj, null) as Texture;
        }

        private static Type FindType(string fullName)
        {
            if (string.IsNullOrWhiteSpace(fullName)) return null;
            var assemblies = AppDomain.CurrentDomain.GetAssemblies();
            foreach (var asm in assemblies)
            {
                try
                {
                    var t = asm.GetType(fullName, false);
                    if (t != null) return t;
                }
                catch { }
            }
            return null;
        }

        private void CaptureTextureToJpeg(Texture source)
        {
            try
            {
                var width = Mathf.Max(2, source.width);
                var height = Mathf.Max(2, source.height);
                var targetWidth = width;
                var targetHeight = height;
                var isWebCamSource = source is WebCamTexture;
                if (useExternalCameraTexture && optimizeExternalPreviewForTracking && !isWebCamSource)
                {
                    var maxWidth = Mathf.Max(64, externalPreviewMaxWidth);
                    if (targetWidth > maxWidth)
                    {
                        var scale = maxWidth / (float)targetWidth;
                        targetWidth = maxWidth;
                        targetHeight = Mathf.Max(2, Mathf.RoundToInt(targetHeight * scale));
                    }
                }

                if (_cameraTexture == null || _cameraTexture.width != targetWidth || _cameraTexture.height != targetHeight)
                {
                    if (_cameraTexture != null) Destroy(_cameraTexture);
                    _cameraTexture = new Texture2D(targetWidth, targetHeight, TextureFormat.RGB24, false);
                }

                if (source is WebCamTexture webcamTexture && targetWidth == width && targetHeight == height)
                {
                    _cameraTexture.SetPixels32(webcamTexture.GetPixels32());
                    _cameraTexture.Apply(false, false);
                }
                else
                {
                    // Blit to a temporary RenderTexture then ReadPixels to CPU.
                    var tmp = RenderTexture.GetTemporary(targetWidth, targetHeight, 0, RenderTextureFormat.ARGB32);
                    Graphics.Blit(source, tmp);
                    var prev = RenderTexture.active;
                    RenderTexture.active = tmp;
                    _cameraTexture.ReadPixels(new Rect(0, 0, targetWidth, targetHeight), 0, 0, false);
                    _cameraTexture.Apply(false, false);
                    RenderTexture.active = prev;
                    RenderTexture.ReleaseTemporary(tmp);
                }

                var quality = Mathf.Clamp(cameraJpegQuality, 1, 100);
                if (useExternalCameraTexture && optimizeExternalPreviewForTracking)
                {
                    quality = Mathf.Min(quality, 45);
                }
                var jpg = _cameraTexture.EncodeToJPG(quality);
                lock (_cameraLock)
                {
                    _latestJpeg = jpg;
                    _cameraFrameCount++;
                    _cameraLastFrameTs = Time.realtimeSinceStartup;
                    _cameraLastFrameUtcTicks = DateTime.UtcNow.Ticks;
                }
            }
            catch (System.Exception) { }
        }
    }
}
