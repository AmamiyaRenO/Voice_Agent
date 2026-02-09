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
        private float cameraClientActiveWindowSeconds = 3f;

        [Header("Dialogue")]
        [SerializeField, Tooltip("Available LLM dialogue styles exposed in the tester UI")]
        private string[] availableDialogStyles = new[] { "Supportive", "Minimalist", "Energetic" };

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
                    case "/sdk":
                    case "/sdk.html":
                        await RespondWithSdkHtmlAsync(context.Response).ConfigureAwait(false);
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
                    case "/api/llm/style":
                        await HandleLlmStyleAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/game":
                        await HandleGameAsync(context).ConfigureAwait(false);
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
        private struct LlmStyleRequest
        {
            public string style;
        }

        [Serializable]
        private struct GameRequest
        {
            public string action;
            public string name;
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

        private async Task HandleLlmStyleAsync(HttpListenerContext context)
        {
            var request = ParseJsonBody<LlmStyleRequest>(context.Request);
            var style = (request.style ?? string.Empty).Trim();
            if (string.IsNullOrEmpty(style))
            {
                await WriteJsonAsync(context.Response, 400, "error", "style required").ConfigureAwait(false);
                return;
            }
            if (voiceLauncher == null)
            {
                await WriteJsonAsync(context.Response, 503, "error", "VoiceGameLauncher not assigned").ConfigureAwait(false);
                return;
            }

            var toSend = style;
            PostToMainThread(() => voiceLauncher.SetDialogStyleForTester(toSend));
            await WriteJsonAsync(context.Response, 200, "ok", $"dialog style set: {style}").ConfigureAwait(false);
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

            byte[] jpeg = null;
            lock (_cameraLock)
            {
                if (_latestJpeg != null && _latestJpeg.Length > 0)
                {
                    // Create a shallow copy to avoid locking while sending
                    jpeg = new byte[_latestJpeg.Length];
                    System.Buffer.BlockCopy(_latestJpeg, 0, jpeg, 0, _latestJpeg.Length);
                }
            }

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

            return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
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


        private static string BuildPanelHtml()
        {
            var sb = new StringBuilder(4096);
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
            sb.AppendLine(@"<h2>Dialogue Style</h2>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<button onclick=""setLlmStyle('Supportive')"">Supportive</button>");
            sb.AppendLine(@"<button onclick=""setLlmStyle('Minimalist')"">Minimalist</button>");
            sb.AppendLine(@"<button onclick=""setLlmStyle('Energetic')"">Energetic</button>");
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
            sb.AppendLine(@"<small style=""opacity:0.7"">If the image does not update, ensure the camera is available and enabled.</small>");
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
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'error: ' + err;");
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
            sb.AppendLine(@"function setLlmStyle(value){");
            sb.AppendLine(@"  if(!value) return;");
            sb.AppendLine(@"  send('/api/llm/style',{style:value});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function launchGame(){");
            sb.AppendLine(@"  const name = document.getElementById('gameName').value||'';");
            sb.AppendLine(@"  send('/api/game',{action:'launch',name:name});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function exitGame(){");
            sb.AppendLine(@"  send('/api/game',{action:'exit'});");
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
            sb.AppendLine(@"let cameraPolling = false;");
            sb.AppendLine(@"let cameraSeq = 0;");
            sb.AppendLine(@"let cameraLastLoadAt = 0;");
            sb.AppendLine(@"let cameraLastSetAt = 0;");
            sb.AppendLine(@"let cameraPullTimer = null;");
            sb.AppendLine(@"let cameraWatchdogTimer = null;");
            sb.AppendLine(@"let cameraHeartbeatTimer = null;");
            sb.AppendLine(@"let cameraAutoStart = false;");
            sb.AppendLine(@"function cameraHeartbeat(){");
            sb.AppendLine(@"  fetch('/api/camera/ping',{method:'POST',cache:'no-store'}).catch(()=>{});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function pullCameraFrame(){");
            sb.AppendLine(@"  if(!cameraView) return;");
            sb.AppendLine(@"  cameraSeq++;");
            sb.AppendLine(@"  cameraLastSetAt = Date.now();");
            sb.AppendLine(@"  cameraView.src = '/camera.jpg?t=' + cameraLastSetAt + '&n=' + cameraSeq;");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function cameraWatchdog(){");
            sb.AppendLine(@"  if(!cameraView) return;");
            sb.AppendLine(@"  const now = Date.now();");
            sb.AppendLine(@"  if(cameraLastSetAt > 0 && (now - cameraLastSetAt) > 2200 && (now - cameraLastLoadAt) > 2200){");
            sb.AppendLine(@"    pullCameraFrame();");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function startPolling(){");
            sb.AppendLine(@"  if(cameraPolling) return;");
            sb.AppendLine(@"  cameraPolling = true;");
            sb.AppendLine(@"  cameraView.onload = () => { cameraLastLoadAt = Date.now(); };");
            sb.AppendLine(@"  cameraView.onerror = () => { setTimeout(pullCameraFrame, 250); };");
            sb.AppendLine(@"  cameraHeartbeat();");
            sb.AppendLine(@"  pullCameraFrame();");
            sb.AppendLine(@"  cameraPullTimer = setInterval(pullCameraFrame, 1000);");
            sb.AppendLine(@"  cameraWatchdogTimer = setInterval(cameraWatchdog, 1000);");
            sb.AppendLine(@"  cameraHeartbeatTimer = setInterval(cameraHeartbeat, 1000);");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function stopPolling(){");
            sb.AppendLine(@"  cameraPolling = false;");
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
            sb.AppendLine(@"  if(document.hidden){");
            sb.AppendLine(@"    stopPolling();");
            sb.AppendLine(@"    return;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"  if(cameraAutoStart){");
            sb.AppendLine(@"    startPolling();");
            sb.AppendLine(@"    setTimeout(pullCameraFrame, 30);");
            sb.AppendLine(@"    setTimeout(pullCameraFrame, 200);");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"});");
            sb.AppendLine(@"window.addEventListener('beforeunload', stopPolling);");
            sb.AppendLine(@"function initCamera(){ if(!cameraView) return; if(!document.hidden && cameraAutoStart){ startPolling(); } }");
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
            sb.AppendLine(@"@media(max-width:900px){.wrap{grid-template-columns:1fr;}}");
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
            sb.AppendLine(@"<script>");
            sb.AppendLine(@"const sdkMap = {");
            sb.AppendLine(@"  'speak(text,voice,model,speed,volume)': { endpoint:'/api/speak', payload:{ text:'Hello from SDK visualizer', voice:'en_US', model:'', speed:1.0, volume:1.0 } },");
            sb.AppendLine(@"  'qwen_speak(text,speaker,instruct)': { endpoint:'/api/qwen/speak', payload:{ text:'Hello from Qwen', speaker:'Ryan', instruct:'friendly' } },");
            sb.AppendLine(@"  'set_tts_options(voice,model)': { endpoint:'/api/voice', payload:{ action:'set', voice:'en_US' } },");
            sb.AppendLine(@"  'set_tts_model(model)': { endpoint:'/api/voice', payload:{ action:'set_model', model:'' } },");
            sb.AppendLine(@"  'set_dialog_style(style)': { endpoint:'/api/llm/style', payload:{ style:'Supportive' } },");
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
            sb.AppendLine(@"methodEl.innerHTML = methods.map(m => `<option value=""${m}"">${m}</option>`).join('');");
            sb.AppendLine(@"function syncMethod(){");
            sb.AppendLine(@"  const m = methodEl.value;");
            sb.AppendLine(@"  const cfg = sdkMap[m];");
            sb.AppendLine(@"  endpointEl.value = cfg.endpoint;");
            sb.AppendLine(@"  payloadEl.value = JSON.stringify(cfg.payload, null, 2);");
            sb.AppendLine(@"  respEl.textContent = '';");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function resetPayload(){ syncMethod(); }");
            sb.AppendLine(@"async function invokeSdk(){");
            sb.AppendLine(@"  const endpoint = endpointEl.value;");
            sb.AppendLine(@"  let payload = {};");
            sb.AppendLine(@"  try { payload = JSON.parse(payloadEl.value || '{}'); }");
            sb.AppendLine(@"  catch(err){ statusEl.textContent = 'Invalid JSON: ' + err; return; }");
            sb.AppendLine(@"  statusEl.textContent = 'POST ' + endpoint + ' ...';");
            sb.AppendLine(@"  try {");
            sb.AppendLine(@"    const resp = await fetch(endpoint,{ method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });");
            sb.AppendLine(@"    const txt = await resp.text();");
            sb.AppendLine(@"    statusEl.textContent = `HTTP ${resp.status} ${resp.statusText}`;");
            sb.AppendLine(@"    try { respEl.textContent = JSON.stringify(JSON.parse(txt), null, 2); }");
            sb.AppendLine(@"    catch { respEl.textContent = txt; }");
            sb.AppendLine(@"  } catch(err){");
            sb.AppendLine(@"    statusEl.textContent = 'Request failed: ' + err;");
            sb.AppendLine(@"  }");
            sb.AppendLine(@"}");
            sb.AppendLine(@"methodEl.addEventListener('change', syncMethod);");
            sb.AppendLine(@"syncMethod();");
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
