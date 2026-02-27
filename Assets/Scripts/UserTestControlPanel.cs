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
        private string voiceServiceUrl = VoiceAgentDefaults.PiperSpeakUrl;
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
        private string llmServiceBaseUrl = VoiceAgentDefaults.AsrBaseUrl;
        [SerializeField, Tooltip("Ollama base URL used by /api/vision/describe.")]
        private string ollamaBaseUrl = VoiceAgentDefaults.OllamaBaseUrl;
        [SerializeField, Tooltip("Default multimodal model for camera description.")]
        private string defaultVisionModel = VoiceAgentDefaults.DefaultVisionModel;
        [SerializeField, Tooltip("Default prompt used when /api/vision/describe request has empty prompt.")]
        private string defaultVisionPrompt = "Describe what you see in this camera frame in 2-4 concise sentences.";
        [SerializeField, Tooltip("Telemetry dashboard base URL served by telemetry_service.")]
        private string telemetryDashboardUrl = VoiceAgentDefaults.TelemetryDashboardUrl;

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
                // 浼樺厛鍙栫幆澧冨彉閲忚鐩栵細VOICE_MODELS_DIR 鎴?PIPER_MODELS_DIR
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

            if (useExternalCameraTexture && optimizeExternalPreviewForTracking && !IsCameraClientActive())
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
                if (tex is WebCamTexture extCam && !extCam.isPlaying)
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
                case "verysad": // verySad 鈫?lower-cased
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
                    // Unknown mode: treat it as a preset name (same path as "happy"/etc).
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

            // Prefer Unity local playback via VoiceGameLauncher -> Piper /speak.
            if (voiceLauncher != null)
            {
                ConversationLog.AddEntry(ConversationRole.Wizard, text, "tester_panel");
                var voiceToSend = requestedVoice;
                var modelToSend = requestedModel;
                PostToMainThread(() => voiceLauncher.TriggerManualTesterSpeak(text, voiceToSend, modelToSend, requestedInstruct));
                await WriteJsonAsync(context.Response, 200, "ok", "playing locally").ConfigureAwait(false);
                return;
            }

            // Fallback: if VoiceGameLauncher is not bound, call voice service directly (no local playback).
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
                url = VoiceAgentDefaults.AsrBaseUrl;
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
                url = VoiceAgentDefaults.OllamaBaseUrl;
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
            return VoiceAgentDefaults.DefaultVisionModel;
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
            bool boolValue;
            if (TryReadOptionalBool(requestObj, "use_llm_intent_classifier", out boolValue))
            {
                SetOrRemoveString(intentObj, "use_llm_classifier", boolValue ? "true" : "false");
            }
            if (TryReadOptionalBool(requestObj, "use_moonshine_intent_recognizer", out boolValue))
            {
                SetOrRemoveString(intentObj, "use_moonshine_intent_recognizer", boolValue ? "true" : "false");
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
            var useLlmIntentClassifier = ReadOptionalBool(intentObj, "use_llm_classifier", false);
            var useMoonshineIntentRecognizer = ReadOptionalBool(
                intentObj,
                "use_moonshine_intent_recognizer",
                false);

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
                ollamaModel = VoiceAgentDefaults.DefaultVisionModel;
            }
            payload["ollama_model"] = ollamaModel;
            payload["launch_triggers"] = string.Join(", ", launchTriggers);
            payload["exit_keywords"] = string.Join(", ", exitKeywords);
            payload["use_llm_intent_classifier"] = useLlmIntentClassifier;
            payload["use_moonshine_intent_recognizer"] = useMoonshineIntentRecognizer;
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
                model = VoiceAgentDefaults.DefaultVisionModel;
            }

            var url = (baseUrl ?? string.Empty).Trim().TrimEnd('/');
            if (string.IsNullOrWhiteSpace(url))
            {
                url = VoiceAgentDefaults.OllamaBaseUrl;
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

            return VoiceAgentDefaults.DefaultVisionModel;
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

        private static bool TryReadOptionalBool(JSONObject obj, string key, out bool value)
        {
            value = false;
            if (obj == null || string.IsNullOrWhiteSpace(key) || !obj.HasKey(key))
            {
                return false;
            }

            var raw = (obj[key]?.Value ?? string.Empty).Trim();
            return TryParseBool(raw, out value);
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

        private static bool ReadOptionalBool(JSONObject obj, string key, bool fallback)
        {
            if (obj == null || string.IsNullOrWhiteSpace(key) || !obj.HasKey(key))
            {
                return fallback;
            }

            var raw = (obj[key]?.Value ?? string.Empty).Trim();
            bool value;
            return TryParseBool(raw, out value) ? value : fallback;
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
                .Replace('\uFF0C', ',')
                .Replace('\uFF1B', ';')
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
                        await WriteJsonAsync(
                            context.Response,
                            400,
                            "error",
                            "mode must be whisper-large-v3, moonshine-small, moonshine-medium, or api"
                        ).ConfigureAwait(false);
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
                modes = new[] { "whisper-large-v3", "moonshine-small", "moonshine-medium", "api" };
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
                case "whisper-large-v3":
                case "offline":
                case "local":
                case "whisper":
                case "faster-whisper":
                case "large-v3":
                    return "whisper-large-v3";
                case "moonshine-small":
                case "moonshine_small":
                case "small":
                    return "moonshine-small";
                case "moonshine-medium":
                case "moonshine_medium":
                case "moonshine":
                case "medium":
                    return "moonshine-medium";
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
                mode = "whisper-large-v3",
                available_modes = new[] { "whisper-large-v3", "moonshine-small", "moonshine-medium", "api" },
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
                    parsed.mode = "whisper-large-v3";
                }
                if (parsed.available_modes == null || parsed.available_modes.Length == 0)
                {
                    parsed.available_modes = new[] { "whisper-large-v3", "moonshine-small", "moonshine-medium", "api" };
                }
                parsed.mode = NormalizeAsrMode(parsed.mode);
                if (string.IsNullOrWhiteSpace(parsed.mode))
                {
                    parsed.mode = "whisper-large-v3";
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
                ? VoiceAgentDefaults.TelemetryDashboardUrl
                : telemetryDashboardUrl.Trim();

            var html = LoadPanelTemplate("telemetry.html");
            if (string.IsNullOrEmpty(html))
            {
                return BuildMissingTemplateHtml("telemetry.html");
            }

            return html.Replace("{{TELEMETRY_DASHBOARD_URL}}", EscapeJson(dashboardBase));
        }

        private static string BuildGameConfigHtml()
        {
            return LoadPanelTemplateOrFallback("games.html");
        }

        private static string BuildRuntimeConfigHtml()
        {
            return LoadPanelTemplateOrFallback("runtime.html");
        }

        private static string BuildSetupWizardHtml()
        {
            return LoadPanelTemplateOrFallback("setup.html");
        }

        private static string BuildPanelHtml()
        {
            return LoadPanelTemplateOrFallback("panel.html");
        }

        private static string BuildSdkPanelHtml()
        {
            return LoadPanelTemplateOrFallback("sdk.html");
        }

        private static string LoadPanelTemplateOrFallback(string fileName)
        {
            var html = LoadPanelTemplate(fileName);
            if (!string.IsNullOrEmpty(html))
            {
                return html;
            }

            return BuildMissingTemplateHtml(fileName);
        }

        private static string LoadPanelTemplate(string fileName)
        {
            if (string.IsNullOrWhiteSpace(fileName))
            {
                return string.Empty;
            }

            try
            {
                var root = Path.Combine(Application.streamingAssetsPath, "panel");
                var fullPath = Path.Combine(root, fileName);
                if (!File.Exists(fullPath))
                {
                    return string.Empty;
                }

                return File.ReadAllText(fullPath, Encoding.UTF8);
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[UserTestPanel] Failed to load panel template '{fileName}': {ex.Message}");
                return string.Empty;
            }
        }

        private static string BuildMissingTemplateHtml(string fileName)
        {
            var safeName = string.IsNullOrWhiteSpace(fileName) ? "unknown" : EscapeJson(fileName.Trim());
            return "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><title>Template Missing</title></head><body>"
                + "<h2>UserTestPanel template missing</h2>"
                + "<p>Missing file: " + safeName + "</p>"
                + "<p>Expected under Assets/StreamingAssets/panel/</p>"
                + "</body></html>";
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

                // Prefer OBS/virtual camera first; fall back to the first available device.
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
