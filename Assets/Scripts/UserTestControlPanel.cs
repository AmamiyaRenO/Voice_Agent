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
using UnityEngine;
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

        private HttpListener listener;
        private CancellationTokenSource shutdownToken;
        private Task listenLoopTask;
        private string activeVoiceCode;
        private string activeTtsModel;
        private static readonly HttpClient SharedHttpClient = new HttpClient();
        private const float DefaultFaceSeconds = 3f;
        private SynchronizationContext mainThreadContext;

        private void Awake()
        {
            mainThreadContext = SynchronizationContext.Current;
            activeVoiceCode = DetermineInitialVoiceCode();
            activeTtsModel = DetermineInitialTtsModel();
            if (autoStart)
            {
                StartServer();
            }
        }

        private void OnDestroy()
        {
            StopServer();
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
                    case "/api/logs":
                        await HandleLogsAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/speak":
                        await HandleSpeakAsync(context).ConfigureAwait(false);
                        return;
                    case "/api/game":
                        await HandleGameAsync(context).ConfigureAwait(false);
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
            public float speed;
            public float volume;
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
                case "happy":
                    await piHub.SendFacePresetAsync("happy", duration).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "face set to happy").ConfigureAwait(false);
                    return;
                case "neutral":
                    await piHub.SendFacePresetAsync("neutral", duration).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "face set to neutral").ConfigureAwait(false);
                    return;
                case "angry":
                    await piHub.SendFacePresetAsync("angry", duration).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "face set to angry").ConfigureAwait(false);
                    return;
                case "sad":
                    await piHub.SendFacePresetAsync("sad", duration).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "face set to sad").ConfigureAwait(false);
                    return;
                case "surprised":
                    await piHub.SendFacePresetAsync("surprised", duration).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", "face set to surprised").ConfigureAwait(false);
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
                    await WriteJsonAsync(context.Response, 400, "error", "unknown face mode").ConfigureAwait(false);
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
                    await piHub.SendLedBreathAsync(color, brightness, period).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", $"led breathe {color}").ConfigureAwait(false);
                    return;
                case "solid":
                    var solidColor = string.IsNullOrWhiteSpace(request.color) ? "#FFFFFF" : NormalizeHex(request.color);
                    if (!IsHexColor(solidColor))
                    {
                        await WriteJsonAsync(context.Response, 400, "error", "invalid color").ConfigureAwait(false);
                        return;
                    }
                    await piHub.SendLedSolidAsync(solidColor, brightness).ConfigureAwait(false);
                    await WriteJsonAsync(context.Response, 200, "ok", $"led solid {solidColor}").ConfigureAwait(false);
                    return;
                case "random":
                    await piHub.SendLedRandomAsync().ConfigureAwait(false);
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

        private async Task HandleVoiceAsync(HttpListenerContext context)
        {
            var request = ParseJsonBody<VoiceRequest>(context.Request);
            var action = (request.action ?? string.Empty).Trim().ToLowerInvariant();

            switch (action)
            {
                case "wake":
                    if (voiceLauncher == null)
                    {
                        await WriteJsonAsync(context.Response, 503, "error", "VoiceGameLauncher not assigned").ConfigureAwait(false);
                        return;
                    }
                    voiceLauncher.TriggerWakeWordForTester();
                    await WriteJsonAsync(context.Response, 200, "ok", "wake flow started").ConfigureAwait(false);
                    return;
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

            var requestedVoice = string.IsNullOrWhiteSpace(request.voice) ? activeVoiceCode : request.voice.Trim();
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

            // 优先让 Unity 侧直接播放（经由 VoiceGameLauncher → Piper /speak）
            if (voiceLauncher != null)
            {
                var voiceToSend = requestedVoice;
                var modelToSend = requestedModel;
                PostToMainThread(() => voiceLauncher.TriggerSpeakForTester(text, voiceToSend, modelToSend));
                await WriteJsonAsync(context.Response, 200, "ok", "playing locally").ConfigureAwait(false);
                return;
            }

            ConversationLog.AddEntry(ConversationRole.Wizard, text, "Wizard Override");

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

                await WriteJsonAsync(context.Response, 200, "ok", "synthesis complete (no local playback)").ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                await WriteJsonAsync(context.Response, 500, "error", $"voice request failed: {ex.Message}").ConfigureAwait(false);
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
            sb.AppendLine(@"<h2>Expressions</h2>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<label for=""faceSeconds"">Duration (s)</label>");
            sb.AppendLine(@"<input id=""faceSeconds"" type=""number"" min=""0"" step=""0.5"" value=""3"">");
            sb.AppendLine(@"<input id=""faceCustom"" type=""text"" placeholder=""custom preset name"">");
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<button onclick=""facePreset('happy')"">Happy</button>");
            sb.AppendLine(@"<button onclick=""facePreset('neutral')"">Neutral</button>");
            sb.AppendLine(@"<button onclick=""facePreset('angry')"">Angry</button>");
            sb.AppendLine(@"<button onclick=""facePreset('sad')"">Sad</button>");
            sb.AppendLine(@"<button onclick=""facePreset('surprised')"">Surprised</button>");
            sb.AppendLine(@"<button onclick=""facePreset('idle')"">Idle</button>");
            sb.AppendLine(@"<button onclick=""customFace()"">Send Custom</button>");
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
            sb.AppendLine(@"</div>");
            sb.AppendLine(@"<div class=""controls"">");
            sb.AppendLine(@"<button onclick=""ledBreathe()"">Breathe</button>");
            sb.AppendLine(@"<button onclick=""ledSolid()"">Solid</button>");
            sb.AppendLine(@"<button onclick=""send('/api/led',{mode:'random'})"">Random</button>");
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
            sb.AppendLine(@"<button onclick=""send('/api/voice',{action:'wake'})"">Start Wake Flow</button>");
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
            sb.AppendLine(@"const logContainer = document.getElementById('transcriptLog');");
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
            sb.AppendLine(@"    const role = typeof entry.role === 'string' ? entry.role.toLowerCase() : 'user';");
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
            sb.AppendLine(@"function ledBreathe(){");
            sb.AppendLine(@"  const color = document.getElementById('ledColor').value;");
            sb.AppendLine(@"  const brightness = parseFloat(document.getElementById('ledBrightness').value)||0.8;");
            sb.AppendLine(@"  const period = parseFloat(document.getElementById('ledPeriod').value)||2;");
            sb.AppendLine(@"  send('/api/led',{mode:'breathe',color:color,brightness:brightness,period:period});");
            sb.AppendLine(@"}");
            sb.AppendLine(@"function ledSolid(){");
            sb.AppendLine(@"  const color = document.getElementById('ledColor').value;");
            sb.AppendLine(@"  const brightness = parseFloat(document.getElementById('ledBrightness').value)||0.8;");
            sb.AppendLine(@"  send('/api/led',{mode:'solid',color:color,brightness:brightness});");
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
            sb.AppendLine(@"if(logContainer){ renderLog([]); }");
            sb.AppendLine(@"refreshLog();");
            sb.AppendLine(@"setInterval(refreshLog, 4000);");
            sb.AppendLine(@"loadVoiceOptions();");
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
    }
}
