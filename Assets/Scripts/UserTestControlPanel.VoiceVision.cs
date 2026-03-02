using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RobotVoice
{
    public sealed partial class UserTestControlPanel
    {
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

    }
}
