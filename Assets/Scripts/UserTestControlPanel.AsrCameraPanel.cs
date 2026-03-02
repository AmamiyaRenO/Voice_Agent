using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.UI;

namespace RobotVoice
{
    public sealed partial class UserTestControlPanel
    {
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
                mode = "moonshine-medium",
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
                    parsed.mode = "moonshine-medium";
                }
                if (parsed.available_modes == null || parsed.available_modes.Length == 0)
                {
                    parsed.available_modes = new[] { "whisper-large-v3", "moonshine-small", "moonshine-medium", "api" };
                }
                parsed.mode = NormalizeAsrMode(parsed.mode);
                if (string.IsNullOrWhiteSpace(parsed.mode))
                {
                    parsed.mode = "moonshine-medium";
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
            TryStartCamera();
            await WriteJsonAsync(context.Response, 200, "ok", "camera heartbeat").ConfigureAwait(false);
        }

        private async Task HandleCameraJpegAsync(HttpListenerContext context)
        {
            TouchCameraClientHeartbeat();
            TryStartCamera();
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
            TryStartCamera();
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

        private static async Task RespondWithMemoryHtmlAsync(HttpListenerResponse response)
        {
            var html = BuildMemoryHtml();
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

        private static string BuildMemoryHtml()
        {
            return LoadPanelTemplateOrFallback("memory.html");
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
            if (releaseCameraWhenAppNotVisible && !_appIsVisible)
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
        }    }
}
