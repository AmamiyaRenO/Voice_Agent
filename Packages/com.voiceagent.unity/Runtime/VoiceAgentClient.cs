using System;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace VoiceAgent.Unity
{
    public sealed class VoiceAgentClient : IDisposable
    {
        private readonly HttpClient httpClient;
        private readonly bool ownsHttpClient;

        public VoiceAgentClient(VoiceAgentSettings settings = null, HttpClient httpClient = null)
        {
            Settings = settings ?? new VoiceAgentSettings();
            this.httpClient = httpClient ?? new HttpClient();
            ownsHttpClient = httpClient == null;
            this.httpClient.Timeout = TimeSpan.FromSeconds(Math.Max(0.5f, Settings.requestTimeoutSeconds));
        }

        public VoiceAgentSettings Settings { get; }

        public string BaseUrl => $"http://{(string.IsNullOrWhiteSpace(Settings.host) ? "127.0.0.1" : Settings.host.Trim())}:{Settings.panelPort}";

        public Task<VoiceAgentApiResult> GetLogsAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Get, "/api/logs", null, cancellationToken);

        public Task<VoiceAgentApiResult> GetTtsOptionsAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Get, "/api/voice/options", null, cancellationToken);

        public Task<VoiceAgentApiResult> GetTtsOptionsAsync(string googleCloudApiKey, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Get, "/api/voice/options", null, cancellationToken, googleCloudApiKey);

        public Task<VoiceAgentApiResult> GetKokoroOptionsAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Get, "/api/kokoro/options", null, cancellationToken);

        public Task<VoiceAgentApiResult> GetFaceOptionsAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Get, "/api/face/options", null, cancellationToken);

        public Task<VoiceAgentApiResult> SpeakAsync(VoiceAgentSpeechRequest request, CancellationToken cancellationToken = default)
        {
            request = request ?? new VoiceAgentSpeechRequest();
            var payload = JsonUtility.ToJson(new SpeakPayload
            {
                text = (request.text ?? string.Empty).Trim(),
                voice = string.IsNullOrWhiteSpace(request.voice) ? Settings.defaultVoice : request.voice,
                backend = string.IsNullOrWhiteSpace(request.backend) ? Settings.defaultBackend : request.backend,
                model = string.IsNullOrWhiteSpace(request.model) ? Settings.defaultTtsModel : request.model,
                googleCloudApiKey = request.googleCloudApiKey ?? string.Empty,
                speed = request.speed,
                volume = request.volume,
            });
            return SendJsonAsync(HttpMethod.Post, "/api/speak", payload, cancellationToken);
        }

        public Task<VoiceAgentApiResult> SetVoiceAsync(string voice, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/voice", JsonUtility.ToJson(new VoiceActionPayload
            {
                action = "set",
                voice = voice ?? string.Empty,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> SetTtsModelAsync(string model, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/voice", JsonUtility.ToJson(new VoiceActionPayload
            {
                action = "set_model",
                model = model ?? string.Empty,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> SetTtsBackendAsync(string backend, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/voice", JsonUtility.ToJson(new VoiceActionPayload
            {
                action = "set_backend",
                backend = backend ?? string.Empty,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> SetKokoroVoiceAsync(string voice, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/voice", JsonUtility.ToJson(new VoiceActionPayload
            {
                action = "set_kokoro_voice",
                voice = voice ?? string.Empty,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> SetGoogleCloudVoiceAsync(string voice, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/voice", JsonUtility.ToJson(new VoiceActionPayload
            {
                action = "set_google_cloud_voice",
                voice = voice ?? string.Empty,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> KokoroSpeakAsync(string text, string voice = null, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/kokoro/speak", JsonUtility.ToJson(new KokoroSpeakPayload
            {
                text = text ?? string.Empty,
                voice = voice ?? string.Empty,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> GetLlmPromptAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Get, "/api/llm/prompt", null, cancellationToken);

        public Task<VoiceAgentApiResult> SetLlmPromptAsync(string prompt, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/llm/prompt", JsonUtility.ToJson(new PromptPayload
            {
                prompt = prompt ?? string.Empty,
                reset = false,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> ResetLlmPromptAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/llm/prompt", JsonUtility.ToJson(new PromptPayload
            {
                reset = true,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> GetRuntimeConfigAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Get, "/api/runtime/config", null, cancellationToken);

        public Task<VoiceAgentApiResult> SetLocalModelAsync(string model, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/runtime/config", JsonUtility.ToJson(new RuntimeConfigPayload
            {
                ollama_model = model ?? string.Empty,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> GetAsrStatusAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Get, "/api/asr", null, cancellationToken);

        public async Task<VoiceAgentTypedResult<VoiceAgentAsrStatus>> GetAsrStatusTypedAsync(CancellationToken cancellationToken = default)
        {
            var result = await GetAsrStatusAsync(cancellationToken).ConfigureAwait(false);
            TryParseJson(result != null ? result.RawBody : null, out VoiceAgentAsrStatus payload);
            return new VoiceAgentTypedResult<VoiceAgentAsrStatus>
            {
                ApiResult = result,
                Payload = payload,
            };
        }

        public Task<VoiceAgentApiResult> SetAsrModeAsync(string mode, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/asr", JsonUtility.ToJson(new AsrPayload
            {
                action = "set_mode",
                mode = mode ?? string.Empty,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> SetBackendAsrModeAsync(string mode, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/asr/backend", JsonUtility.ToJson(new AsrPayload
            {
                action = "set_mode",
                mode = mode ?? string.Empty,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> SetListeningEnabledAsync(bool listeningEnabled, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/asr", JsonUtility.ToJson(new AsrListeningPayload
            {
                action = "set_listening",
                listening = listeningEnabled,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> SetConversationDispatchEnabledAsync(bool enabled, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/asr", JsonUtility.ToJson(new AsrDispatchPayload
            {
                action = "set_conversation_dispatch_enabled",
                enabled = enabled,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> StartListeningAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/asr", "{\"action\":\"start_listening\"}", cancellationToken);

        public Task<VoiceAgentApiResult> PauseListeningAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/asr", "{\"action\":\"pause_listening\"}", cancellationToken);

        public Task<VoiceAgentApiResult> DescribeCurrentCameraAsync(string prompt, string model = null, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/vision/describe", JsonUtility.ToJson(new VisionPayload
            {
                prompt = prompt ?? string.Empty,
                model = model ?? string.Empty,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> LaunchGameAsync(string name, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/game", JsonUtility.ToJson(new GamePayload
            {
                action = "launch",
                name = name ?? string.Empty,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> ExitGameAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/game", "{\"action\":\"exit\"}", cancellationToken);

        public Task<VoiceAgentApiResult> FacePresetAsync(VoiceAgentFacePreset preset, float seconds = 3f, CancellationToken cancellationToken = default) =>
            FacePresetAsync(ToFaceModeName(preset), seconds, cancellationToken);

        public Task<VoiceAgentApiResult> FacePresetAsync(string mode, float seconds = 3f, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/face", JsonUtility.ToJson(new FacePayload
            {
                mode = mode ?? string.Empty,
                seconds = seconds,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> FaceCustomAsync(string value, float seconds = 3f, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/face", JsonUtility.ToJson(new FaceCustomPayload
            {
                mode = "custom",
                value = value ?? string.Empty,
                seconds = seconds,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> LedBreatheAsync(Color color, float brightness = 0.8f, float period = 2f, float duration = 0f, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/led", JsonUtility.ToJson(new LedPayload
            {
                mode = "breathe",
                color = "#" + ColorUtility.ToHtmlStringRGB(color),
                brightness = brightness,
                period = period,
                duration = duration,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> LedSolidAsync(Color color, float brightness = 0.8f, float duration = 0f, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/led", JsonUtility.ToJson(new LedPayload
            {
                mode = "solid",
                color = "#" + ColorUtility.ToHtmlStringRGB(color),
                brightness = brightness,
                duration = duration,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> LedRandomAsync(float duration = 0f, CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/led", JsonUtility.ToJson(new LedPayload
            {
                mode = "random",
                duration = duration,
            }), cancellationToken);

        public Task<VoiceAgentApiResult> LedOffAsync(CancellationToken cancellationToken = default) =>
            SendJsonAsync(HttpMethod.Post, "/api/led", "{\"mode\":\"off\"}", cancellationToken);

        public Task<VoiceAgentApiResult> FlowerOpenAsync(CancellationToken cancellationToken = default) =>
            FlowerActionAsync("open", cancellationToken);

        public Task<VoiceAgentApiResult> FlowerCloseAsync(CancellationToken cancellationToken = default) =>
            FlowerActionAsync("close", cancellationToken);

        public Task<VoiceAgentApiResult> FlowerStopAsync(CancellationToken cancellationToken = default) =>
            FlowerActionAsync("stop", cancellationToken);

        public Task<VoiceAgentApiResult> FlowerOpenSlowAsync(CancellationToken cancellationToken = default) =>
            FlowerActionAsync("open_slow", cancellationToken);

        public Task<VoiceAgentApiResult> FlowerCloseSlowAsync(CancellationToken cancellationToken = default) =>
            FlowerActionAsync("close_slow", cancellationToken);

        public async Task<VoiceAgentConnectionHealth> CheckConnectionHealthAsync(CancellationToken cancellationToken = default)
        {
            var healthz = await SendJsonAsync(HttpMethod.Get, "/healthz", null, cancellationToken).ConfigureAwait(false);
            var voice = await GetTtsOptionsAsync(cancellationToken).ConfigureAwait(false);
            var asr = await GetAsrStatusAsync(cancellationToken).ConfigureAwait(false);
            return new VoiceAgentConnectionHealth
            {
                IsReachable = healthz.Success || voice.Success || asr.Success,
                HealthEndpointOk = healthz.Success,
                VoiceOptionsOk = voice.Success,
                AsrStatusOk = asr.Success,
                Summary = (healthz.Success || voice.Success || asr.Success)
                    ? $"healthz={healthz.Success}, voice={voice.Success}, asr={asr.Success}"
                    : "Voice-agent runtime is unreachable.",
            };
        }

        public async Task StreamAsrEventsAsync(
            Action onConnected,
            Action<VoiceAgentAsrStatus> onEvent,
            Action<string> onError,
            CancellationToken cancellationToken = default)
        {
            using (var streamClient = new HttpClient())
            using (var request = new HttpRequestMessage(HttpMethod.Get, BaseUrl + "/api/asr/events"))
            {
                streamClient.Timeout = Timeout.InfiniteTimeSpan;
                request.Headers.Accept.ParseAdd("text/event-stream");
                using (var response = await streamClient.SendAsync(
                           request,
                           HttpCompletionOption.ResponseHeadersRead,
                           cancellationToken).ConfigureAwait(false))
                {
                    if (!response.IsSuccessStatusCode)
                    {
                        var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                        onError?.Invoke(ExtractMessage(body, response.ReasonPhrase));
                        return;
                    }

                    using (cancellationToken.Register(() => response.Dispose()))
                    using (var stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false))
                    using (var reader = new StreamReader(stream))
                    {
                        onConnected?.Invoke();
                        var data = new StringBuilder();
                        while (!reader.EndOfStream && !cancellationToken.IsCancellationRequested)
                        {
                            var line = await reader.ReadLineAsync().ConfigureAwait(false);
                            if (line == null)
                            {
                                break;
                            }

                            if (line.StartsWith(":", StringComparison.Ordinal))
                            {
                                continue;
                            }

                            if (line.StartsWith("data:", StringComparison.OrdinalIgnoreCase))
                            {
                                data.AppendLine(line.Substring(5).TrimStart());
                                continue;
                            }

                            if (line.Length != 0)
                            {
                                continue;
                            }

                            var payload = data.ToString().Trim();
                            data.Length = 0;
                            if (string.IsNullOrWhiteSpace(payload))
                            {
                                continue;
                            }

                            if (TryParseJson(payload, out VoiceAgentAsrStatus status))
                            {
                                onEvent?.Invoke(status);
                            }
                        }
                    }
                }
            }
        }

        public void Dispose()
        {
            if (ownsHttpClient)
            {
                httpClient.Dispose();
            }
        }

        private Task<VoiceAgentApiResult> FlowerActionAsync(string action, CancellationToken cancellationToken)
        {
            return SendJsonAsync(HttpMethod.Post, "/api/flower", JsonUtility.ToJson(new FlowerPayload
            {
                action = action ?? string.Empty,
            }), cancellationToken);
        }

        private async Task<VoiceAgentApiResult> SendJsonAsync(
            HttpMethod method,
            string path,
            string payload,
            CancellationToken cancellationToken,
            string googleCloudApiKey = null)
        {
            try
            {
                using (var request = new HttpRequestMessage(method, BaseUrl + path))
                {
                    if (!string.IsNullOrWhiteSpace(googleCloudApiKey))
                    {
                        request.Headers.TryAddWithoutValidation("X-Google-Cloud-TTS-Api-Key", googleCloudApiKey.Trim());
                    }
                    if (!string.IsNullOrWhiteSpace(payload))
                    {
                        request.Content = new StringContent(payload, Encoding.UTF8, "application/json");
                    }

                    using (var response = await httpClient.SendAsync(request, cancellationToken).ConfigureAwait(false))
                    {
                        var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                        if (!response.IsSuccessStatusCode)
                        {
                            return VoiceAgentApiResult.Fail(ExtractMessage(body, response.ReasonPhrase), (int)response.StatusCode, body);
                        }

                        return VoiceAgentApiResult.Ok(ExtractMessage(body, "ok"), (int)response.StatusCode, body);
                    }
                }
            }
            catch (Exception ex)
            {
                return VoiceAgentApiResult.Fail(ex.Message);
            }
        }

        internal static bool TryParseJson<T>(string rawBody, out T payload) where T : class
        {
            payload = null;
            if (string.IsNullOrWhiteSpace(rawBody))
            {
                return false;
            }

            try
            {
                payload = JsonUtility.FromJson<T>(rawBody);
                return payload != null;
            }
            catch
            {
                payload = null;
                return false;
            }
        }

        private static string ExtractMessage(string rawBody, string fallback)
        {
            if (string.IsNullOrWhiteSpace(rawBody))
            {
                return fallback ?? string.Empty;
            }

            try
            {
                var envelope = JsonUtility.FromJson<ResponseEnvelope>(rawBody);
                if (!string.IsNullOrWhiteSpace(envelope.message))
                {
                    return envelope.message;
                }

                if (!string.IsNullOrWhiteSpace(envelope.detail))
                {
                    return envelope.detail;
                }

                if (!string.IsNullOrWhiteSpace(envelope.status))
                {
                    return envelope.status;
                }
            }
            catch
            {
            }

            return rawBody;
        }

        private static string ToFaceModeName(VoiceAgentFacePreset preset)
        {
            switch (preset)
            {
                case VoiceAgentFacePreset.Happy: return "happy";
                case VoiceAgentFacePreset.Sad: return "sad";
                case VoiceAgentFacePreset.VerySad: return "verySad";
                case VoiceAgentFacePreset.Excited: return "excited";
                default: return "neutral";
            }
        }

        [Serializable]
        private struct ResponseEnvelope
        {
            public string status;
            public string message;
            public string detail;
        }

        [Serializable] private struct SpeakPayload { public string text; public string voice; public string backend; public string model; public string googleCloudApiKey; public float speed; public float volume; }
        [Serializable] private struct KokoroSpeakPayload { public string text; public string voice; }
        [Serializable] private struct VoiceActionPayload { public string action; public string voice; public string backend; public string model; }
        [Serializable] private struct PromptPayload { public string prompt; public bool reset; }
        [Serializable] private struct RuntimeConfigPayload { public string ollama_model; }
        [Serializable] private struct AsrPayload { public string action; public string mode; }
        [Serializable] private struct AsrListeningPayload { public string action; public bool listening; }
        [Serializable] private struct AsrDispatchPayload { public string action; public bool enabled; }
        [Serializable] private struct VisionPayload { public string prompt; public string model; }
        [Serializable] private struct GamePayload { public string action; public string name; }
        [Serializable] private struct FacePayload { public string mode; public float seconds; }
        [Serializable] private struct FaceCustomPayload { public string mode; public string value; public float seconds; }
        [Serializable] private struct LedPayload { public string mode; public string color; public float brightness; public float period; public float duration; }
        [Serializable] private struct FlowerPayload { public string action; }
    }
}
