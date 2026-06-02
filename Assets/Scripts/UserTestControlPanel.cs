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
    public sealed partial class UserTestControlPanel : MonoBehaviour
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
        [Header("Kokoro TTS")]
        [SerializeField, Tooltip("Default Kokoro voice used by the tester UI")]
        private string defaultKokoroVoice = "af_heart";
        [SerializeField, Tooltip("Additional Kokoro voices shown in the dropdown")]
        private string[] availableKokoroVoices = new[] { "af_heart" };
        [SerializeField, Tooltip("Default Piper/Coqui model identifier exposed in the tester UI")]
        private string defaultTtsModel = "piper-zh";
        [SerializeField, Tooltip("Additional model identifiers shown in the dropdown")]
        private string[] availableTtsModels = new[] { "piper-zh", "piper-en" };
		[SerializeField, Tooltip("Directory to scan for Piper .onnx models to populate the dropdown")]
		private string modelsDirectory = @"D:\piper\models";
		[SerializeField, Tooltip("Whether to recursively include subdirectories when scanning modelsDirectory")]
		private bool scanModelsRecursively = true;

        [Header("Server")]
        [SerializeField, Tooltip("Enable the legacy built-in HTTP control panel inside Unity. Leave off when desktop_runtime provides the panel.")]
        private bool enableEmbeddedHttpServer = false;
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
        [SerializeField, Tooltip("Release local webcam when this Unity app is not focused/visible so other apps can use the camera.")]
        private bool releaseCameraWhenAppNotVisible = true;

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
        private string activeTtsBackend;
        private string activeKokoroVoice;
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
            activeTtsBackend = "piper";
            activeKokoroVoice = DetermineInitialKokoroVoice();
            if (voiceLauncher != null)
            {
                voiceLauncher.SetTtsOptionsForTester(activeVoiceCode, activeTtsModel);
                ApplySavedConversationRuntimeConfig();
            }
            _hasExternalRawImageBinding = externalCameraRawImage != null;
            _hasExternalRendererBinding = externalCameraRenderer != null;
            if (enableEmbeddedHttpServer && autoStart)
            {
                StartServer();
            }
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
            if (!hasFocus && releaseCameraWhenAppNotVisible && !useExternalCameraTexture && _webcam != null)
            {
                StopCamera();
            }
        }

        private void OnApplicationPause(bool pauseStatus)
        {
            _appIsVisible = !pauseStatus;
            if (pauseStatus && releaseCameraWhenAppNotVisible && !useExternalCameraTexture && _webcam != null)
            {
                StopCamera();
            }
        }

        public void StartServer()
        {
            if (!enableEmbeddedHttpServer)
            {
                Debug.Log("[UserTestPanel] Embedded HTTP control panel is disabled; desktop_runtime should provide the panel.");
                return;
            }

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
                        context.Response.Redirect("/index.html");
                        context.Response.Close();
                        return;
                    case "/index.html":
                        await RespondWithHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/panel.html":
                        context.Response.Redirect("/index.html");
                        context.Response.Close();
                        return;
                    case "/games":
                    case "/games.html":
                        await RespondWithGameConfigHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/runtime":
                    case "/runtime.html":
                        await RespondWithRuntimeConfigHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/memory":
                    case "/memory.html":
                        await RespondWithMemoryHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/setup":
                    case "/setup.html":
                        await RespondWithSetupWizardHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/sdk":
                    case "/sdk.html":
                        await RespondWithSdkHtmlAsync(context.Response).ConfigureAwait(false);
                        return;
                    case "/sdk-manifest":
                    case "/sdk-manifest.json":
                        await RespondWithSdkManifestAsync(context.Response).ConfigureAwait(false);
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
                    case "/api/kokoro/options":
                        await HandleKokoroOptionsAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/logs":
                        await HandleLogsAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/speak":
                        await HandleSpeakAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/kokoro/speak":
                        await HandleKokoroSpeakAsync(context).ConfigureAwait(false);
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
                    case "/api/memory":
                        await HandleMemoryAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/asr":
                        await HandleAsrAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/asr/backend":
                        await HandleAsrBackendAsync(context).ConfigureAwait(false);
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

            if (!useExternalCameraTexture)
            {
                if (releaseCameraWhenAppNotVisible && !_appIsVisible)
                {
                    if (_webcam != null)
                    {
                        StopCamera();
                    }
                    return;
                }

                // Keep physical webcam ownership on-demand only.
                if (IsCameraClientActive())
                {
                    if (_webcam == null || !_webcam.isPlaying)
                    {
                        TryStartCamera();
                    }
                }
                else if (_webcam != null)
                {
                    StopCamera();
                    return;
                }
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
            public string backend;
        }

        [Serializable]
        private struct SpeakRequest
        {
            public string text;
            public string voice;
            public string model;
            public string backend;
            public float speed;
            public float volume;
        }

        [Serializable]
        private struct KokoroSpeakRequest
        {
            public string text;
            public string voice;
            public string speaker;
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


    }
}




