using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using System.Text.RegularExpressions;
using System.Globalization;
using UnityEngine;
using UnityEngine.Networking;
using UnityEngine.UI;

namespace RobotVoice
{
    public class VoiceGameLauncher : MonoBehaviour
    {
        [Header("Dependencies")]
        [SerializeField] private MqttIntentPublisher publisher;
        [SerializeField] private VoskSpeechToText speechToText;
		[SerializeField] private PiMessageHub piHub;

        [Header("Configuration")]
        [SerializeField] private TextAsset intentConfigJson;
        [SerializeField] private string wakeWord = "hi rachel";
        [SerializeField] private bool requireWakeWord = true;
        [SerializeField] private bool requireLaunchKeyword = false;
        [SerializeField] private string[] launchKeywords = { "open", "play" };
        [SerializeField] private string[] exitKeywords = { "quit", "back to lobby" };
        [SerializeField] private SynonymOverride[] synonymOverrides = Array.Empty<SynonymOverride>();
        [SerializeField] private float intentCooldownSeconds = 1.5f;
        [SerializeField] private bool logDebugMessages = true;
        [Header("Transcript Filtering")]
        [SerializeField, Tooltip("Normalized RMS level required to accept single-word transcripts"), Range(0f, 1f)]
        private float speechEnergyNoiseGate = 0.05f;
        [SerializeField, Tooltip("Drop transcripts whose average log probability is lower than this value")]
        private float noiseAverageLogProbThreshold = -0.6f;
        [SerializeField, Tooltip("Seconds to suppress repeated transcripts"), Min(0f)]
        private float duplicateSuppressionSeconds = 2f;
        [Header("Wake Word Interaction")]
        [SerializeField] private string wakeWordPrompt = "Listening";
        [SerializeField] private AudioSource wakeWordPromptSource;
        [SerializeField] private AudioClip wakeWordPromptClip;
        [SerializeField] private GameObject wakeListeningIndicatorRoot;
        [SerializeField] private Image wakeListeningProgressImage;
        [SerializeField] private Text wakeListeningCountdownText;
        [Header("TTS (Piper)")]
        [SerializeField] private string piperSpeakUrl = "http://127.0.0.1:5005/speak";
        [SerializeField, Tooltip("Prompt text sent to LLM when wake word is detected. Keep it short.")]
        private string wakeAcknowledgeUserText = "Wake word detected. Reply briefly that you are listening.";
        [Header("Backend Voice Pipeline")]
        [SerializeField, Tooltip("Auto-enable MQTT publishing on the MqttIntentPublisher so speech can reach intent_service/dialog_service.")]
        private bool autoEnableMqttPublishing = true;
        [SerializeField, Tooltip("Topic for raw recognised speech text. intent_service subscribes to this topic.")]
        private string voiceTextTopic = "robot/voice/text";
        [Header("Wake/First Command")]
        [SerializeField, Min(0.5f)] private float firstCommandListenSeconds = 4.5f;
        private float lastIntentTime = -999f;
        private VoiceIntentConfig runtimeConfig;
        private readonly List<KeywordPhrase> keywordPhrases = new List<KeywordPhrase>();
        private Coroutine wakeListeningIndicatorCoroutine;
        private string lastDeliveredTranscript = string.Empty;
        private float lastDeliveredTranscriptTime = -999f;

        private static readonly HashSet<string> NoiseSingles = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "you",
            "uh",
            "um",
            "yeah",
            "hmm",
        };

        private static readonly Regex CommandKeywordRegex = new Regex(
            @"\b(open|launch|start|stop|robot)\b",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

        [Serializable]
        private struct CoachRespondPayload
        {
            public string text;
        }

        private struct RecognitionMetadata
        {
            public float AvgLogProb;
            public float MaxAmplitude;
            public float Rms;
            public string Text;
        }

        private sealed class KeywordPhrase
        {
            public string Text = string.Empty;
            public string LowerInvariant = string.Empty;
        }

        private void Awake()
        {
            if (speechToText == null)
            {
                speechToText = GetComponent<VoskSpeechToText>();
            }
            if (piHub == null)
            {
                piHub = FindObjectOfType<PiMessageHub>();
            }
            Application.runInBackground = true;
            ApplyFullscreenMode();
            runtimeConfig = BuildRuntimeConfig();
            ApplySpeechKeyPhrases();

            // Ensure Unity can actually publish to MQTT; otherwise the backend never receives transcripts.
            if (autoEnableMqttPublishing && publisher != null)
            {
                try
                {
                    publisher.SetPublishingEnabled(true);
                }
                catch { }
            }
        }

        private void Start()
        {
            // Piper 统一出声，无需 Windows TTS 初始化
        }

        private void OnDestroy()
        {
            StopWakeWordListeningIndicator();
            // Piper 统一出声，无需 Windows TTS 释放
        }

#if UNITY_EDITOR
        private void OnValidate()
        {
            runtimeConfig = BuildRuntimeConfig();
            ApplySpeechKeyPhrases();
        }
#endif

        private void ApplyFullscreenMode()
        {
            if (Application.isEditor)
            {
                return;
            }

            var resolution = Screen.currentResolution;
            Screen.fullScreenMode = FullScreenMode.ExclusiveFullScreen;
            Screen.SetResolution(resolution.width, resolution.height, true);
        }

        private VoiceIntentConfig BuildRuntimeConfig()
        {
            VoiceIntentConfig config = null;
            if (intentConfigJson != null && !string.IsNullOrWhiteSpace(intentConfigJson.text))
            {
                config = VoiceIntentConfig.LoadFromJson(intentConfigJson.text);
            }

            if (config == null)
            {
                config = new VoiceIntentConfig();
            }

            if (config.LaunchKeywords == null || config.LaunchKeywords.Length == 0)
            {
                config.LaunchKeywords = launchKeywords != null && launchKeywords.Length > 0
                    ? launchKeywords
                    : new[] { "open", "play" };
            }

            if (config.ExitKeywords == null || config.ExitKeywords.Length == 0)
            {
                config.ExitKeywords = exitKeywords != null && exitKeywords.Length > 0
                    ? exitKeywords
                    : new[] { "back", "quit", "close", "shut down" };
            }

            if (config.SynonymOverrides == null || config.SynonymOverrides.Length == 0)
            {
                config.SynonymOverrides = synonymOverrides ?? Array.Empty<SynonymOverride>();
            }

            if (string.IsNullOrWhiteSpace(config.WakeWord))
            {
                config.WakeWord = wakeWord ?? string.Empty;
            }

            return config;
        }

        private void ApplySpeechKeyPhrases()
        {
            var unique = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var aggregated = new List<string>();

            void TryAdd(string value)
            {
                if (string.IsNullOrWhiteSpace(value))
                {
                    return;
                }

                var trimmed = value.Trim();
                if (trimmed.Length == 0)
                {
                    return;
                }

                if (unique.Add(trimmed))
                {
                    aggregated.Add(trimmed);
                }
            }

            void TryAddRange(IEnumerable<string> values)
            {
                if (values == null)
                {
                    return;
                }

                foreach (var value in values)
                {
                    TryAdd(value);
                }
            }

            if (runtimeConfig != null)
            {
                TryAddRange(runtimeConfig.LaunchKeywords);
                TryAddRange(runtimeConfig.ExitKeywords);

                if (runtimeConfig.SynonymOverrides != null)
                {
                    for (int i = 0; i < runtimeConfig.SynonymOverrides.Length; i++)
                    {
                        var synonym = runtimeConfig.SynonymOverrides[i];
                        if (synonym == null)
                        {
                            continue;
                        }

                        TryAdd(synonym.Canonical);
                        TryAddRange(synonym.Variants);
                    }
                }
            }

            RebuildKeywordPhrases(aggregated);
        }

        public void HandleVoskResult(string message)
        {
            if (publisher == null)
            {
                Debug.LogError("[RobotVoice] VoiceGameLauncher missing MqttIntentPublisher reference");
                return;
            }

            var metadata = ExtractRecognitionMetadata(message);
            var masked = FilterTranscript(message, out var rawRecognisedText);
            var hasKeywordMatch = !string.IsNullOrWhiteSpace(masked) && masked != "*";

            if (string.IsNullOrWhiteSpace(masked) && string.IsNullOrWhiteSpace(rawRecognisedText))
            {
                return;
            }

            if (logDebugMessages)
            {
                var debugText = hasKeywordMatch ? masked : rawRecognisedText;
                if (!string.IsNullOrWhiteSpace(debugText))
                {
                    Debug.Log($"[RobotVoice] Recognised: {debugText.Trim()}");
                }
            }

            var recognised = hasKeywordMatch ? RemoveMaskPlaceholders(masked) : string.Empty;

            var rawRecognised = string.IsNullOrWhiteSpace(rawRecognisedText)
                ? string.Empty
                : rawRecognisedText.Trim();

            recognised = recognised.Trim();

            if (string.IsNullOrWhiteSpace(rawRecognised) && !string.IsNullOrWhiteSpace(recognised))
            {
                rawRecognised = recognised;
            }

            var candidateText = SelectCandidateText(rawRecognised, recognised, masked, metadata.Text);
            var normalizedCandidate = NormalizeTranscript(candidateText);

            if (!hasKeywordMatch && ShouldIgnoreTranscriptAsNoise(candidateText, metadata))
            {
                if (logDebugMessages && !string.IsNullOrWhiteSpace(candidateText))
                {
                    Debug.Log($"[RobotVoice] Ignored low-confidence speech \"{candidateText.Trim()}\"");
                }
                // 即便被噪声过滤，仍记录到日志以便观察
                if (!string.IsNullOrWhiteSpace(candidateText))
                {
                    ConversationLog.AddEntry(ConversationRole.User, candidateText, "filtered_noise");
                }
                return;
            }

            if (IsDuplicateTranscript(normalizedCandidate))
            {
                if (logDebugMessages && !string.IsNullOrWhiteSpace(candidateText))
                {
                    Debug.Log($"[RobotVoice] Ignored duplicate speech \"{candidateText.Trim()}\"");
                }

                return;
            }

            if (!string.IsNullOrEmpty(normalizedCandidate))
            {
                RegisterTranscriptUsage(normalizedCandidate);
            }
            if (IsOnCooldown())
            {
                if (logDebugMessages)
                {
                    Debug.Log("[RobotVoice] Ignoring speech because of cooldown");
                }
                return;
            }

            // 去掉唤醒词门控，直接使用候选文本
            var processed = string.IsNullOrWhiteSpace(candidateText) ? rawRecognised : candidateText;

            if (hasKeywordMatch)
            {
                if (IsExitIntent(processed))
                {
                    PublishExit(rawRecognised);
                    return;
                }

                if (TryExtractGameName(processed, out var gameName))
                {
                    PublishLaunch(gameName, rawRecognised);
                    return;
                }

                if (!requireLaunchKeyword && !string.IsNullOrWhiteSpace(processed))
                {
                    var resolved = runtimeConfig != null ? runtimeConfig.ResolveGameName(processed) : processed;
                    // 若没有解析出游戏名，也记录一下用户指令文本，避免“open”等未被记录
                    if (string.IsNullOrWhiteSpace(resolved) && !string.IsNullOrWhiteSpace(rawRecognised))
                    {
                        ConversationLog.AddEntry(ConversationRole.User, rawRecognised, "no_game_resolved");
                    }
                    PublishLaunch(resolved, rawRecognised);
                    return;
                }
            }

            var textForCoach = string.IsNullOrWhiteSpace(processed) ? rawRecognised : processed;
            if (!string.IsNullOrWhiteSpace(textForCoach))
            {
                ConversationLog.AddEntry(ConversationRole.User, textForCoach);
                PublishVoiceText(textForCoach, metadata);
            }
        }

        // Wake flow removed

        public void TriggerLaunchForTester(string gameName)
        {
            var trimmed = string.IsNullOrWhiteSpace(gameName) ? string.Empty : gameName.Trim();
            var resolved = string.IsNullOrWhiteSpace(trimmed)
                ? string.Empty
                : (runtimeConfig != null ? runtimeConfig.ResolveGameName(trimmed) : trimmed);
            PublishLaunch(resolved, string.IsNullOrWhiteSpace(trimmed) ? "tester_panel" : $"tester_panel:{trimmed}");
        }

        public void TriggerExitForTester(string reason = "tester_panel")
        {
            PublishExit(string.IsNullOrWhiteSpace(reason) ? "tester_panel" : reason.Trim());
        }

        public void TriggerSpeakForTester(string text, string voiceCode = null, string modelPath = null)
        {
            var trimmed = string.IsNullOrWhiteSpace(text) ? string.Empty : text.Trim();
            if (string.IsNullOrWhiteSpace(trimmed))
            {
                return;
            }

            // 不再记录 Wizard 覆盖到日志，避免干扰对话流展示

            // 在播放前触发呼吸灯效果，提示“正在说话”
            if (piHub != null)
            {
                _ = piHub.SendLedBreathAsync();
            }

            if (!string.IsNullOrWhiteSpace(piperSpeakUrl))
            {
                StartCoroutine(PlayTtsFromPiper(trimmed, voiceCode, modelPath));
            }
        }

        private bool IsOnCooldown()
        {
            return Time.realtimeSinceStartup - lastIntentTime < Mathf.Max(0.1f, intentCooldownSeconds);
        }

        private void PublishVoiceText(string text, RecognitionMetadata metadata)
        {
            if (publisher == null) return;
            if (string.IsNullOrWhiteSpace(text)) return;

            var payload = new StringBuilder(256)
                .Append("{\"text\":\"").Append(EscapeJson(text.Trim())).Append('"')
                .Append(",\"source\":\"unity_whisper\"")
                .Append(",\"ts\":").Append(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds());

            if (!float.IsNaN(metadata.AvgLogProb))
            {
                payload.Append(",\"avg_logprob\":").Append(metadata.AvgLogProb.ToString(CultureInfo.InvariantCulture));
            }
            if (metadata.Rms > 0f)
            {
                payload.Append(",\"rms\":").Append(metadata.Rms.ToString(CultureInfo.InvariantCulture));
            }
            if (metadata.MaxAmplitude > 0f)
            {
                payload.Append(",\"max_amplitude\":").Append(metadata.MaxAmplitude.ToString(CultureInfo.InvariantCulture));
            }
            payload.Append('}');

            if (logDebugMessages && publisher.DisablePublishing)
            {
                Debug.LogWarning("[RobotVoice] MQTT publishing is disabled; enable MqttIntentPublisher or set autoEnableMqttPublishing=true.");
            }

            _ = publisher.PublishRawAsync(voiceTextTopic, payload.ToString());
        }

        // Wake-only flow removed

        // Wake recording window removed

        // Wake prompt flow removed

        // Wake ack flow removed

        // Wake window state removed

        // Wake window activation removed

        // Wake window clear removed

        // Wake word application removed

        // Wake word normalization and matching removed

        private bool IsExitIntent(string recognised)
        {
            var keywords = runtimeConfig.ExitKeywords;
            if (keywords == null)
            {
                return false;
            }

            foreach (var keyword in keywords)
            {
                if (string.IsNullOrWhiteSpace(keyword))
                {
                    continue;
                }

                if (recognised.IndexOf(keyword.Trim(), StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    return true;
                }
            }

            return false;
        }

        private bool TryExtractGameName(string recognised, out string gameName)
        {
            gameName = string.Empty;
            var keywords = runtimeConfig.LaunchKeywords;
            if (keywords != null)
            {
                foreach (var keyword in keywords)
                {
                    if (string.IsNullOrWhiteSpace(keyword))
                    {
                        continue;
                    }

                    var index = recognised.IndexOf(keyword.Trim(), StringComparison.OrdinalIgnoreCase);
                    if (index >= 0)
                    {
                        var candidate = recognised.Substring(index + keyword.Length).Trim();
                        if (!string.IsNullOrWhiteSpace(candidate))
                        {
                            gameName = runtimeConfig.ResolveGameName(candidate);
                            return true;
                        }
                    }
                }
            }

            return false;
        }

        private void PublishLaunch(string gameName, string rawText)
        {
            if (string.IsNullOrWhiteSpace(gameName))
            {
                if (logDebugMessages)
                {
                    Debug.Log("[RobotVoice] Launch intent ignored because the game name is empty");
                }
                return;
            }

            if (!string.IsNullOrWhiteSpace(rawText) &&
                !rawText.StartsWith("tester_panel", StringComparison.OrdinalIgnoreCase))
            {
                ConversationLog.AddEntry(ConversationRole.User, rawText);
            }

                        // 启动游戏时在屏幕上显示笑脸
                        if (piHub != null)
                        {
                                _ = piHub.SendFaceHappyAsync();
			}

            lastIntentTime = Time.realtimeSinceStartup;
            _ = publisher.PublishLaunchIntentAsync(gameName, rawText);
            // Coach reply handled by external dialog_service
        }

        private void PublishExit(string rawText)
        {
            lastIntentTime = Time.realtimeSinceStartup;
            if (!string.IsNullOrWhiteSpace(rawText) &&
                !rawText.StartsWith("tester_panel", StringComparison.OrdinalIgnoreCase))
            {
                ConversationLog.AddEntry(ConversationRole.User, rawText);
            }
            _ = publisher.PublishExitIntentAsync(rawText);
            // Coach reply handled by external dialog_service
        }

        // --- Dialogue style (LLM persona) ---
        public void SetDialogStyleForTester(string styleIdOrName)
        {
            var style = string.IsNullOrWhiteSpace(styleIdOrName) ? string.Empty : styleIdOrName.Trim();
            if (publisher != null && !string.IsNullOrEmpty(style))
            {
                var payload = "{\"style\":\"" + EscapeJson(style) + "\"}";
                _ = publisher.PublishRawAsync("robot/dialog/style", payload);
            }
            ConversationLog.AddEntry(ConversationRole.System, $"Dialogue style = {style}", "tester_panel");
        }

        // --- TTS options (voice / model) ---
        public void SetTtsOptionsForTester(string voiceCode, string modelPath)
        {
            var voice = string.IsNullOrWhiteSpace(voiceCode) ? string.Empty : voiceCode.Trim();
            var model = string.IsNullOrWhiteSpace(modelPath) ? string.Empty : modelPath.Trim();
            if (publisher != null)
            {
                var sb = new StringBuilder(128);
                sb.Append('{');
                if (!string.IsNullOrEmpty(voice))
                {
                    sb.Append("\"voice\":\"").Append(EscapeJson(voice)).Append('\"');
                }
                if (!string.IsNullOrEmpty(model))
                {
                    if (sb[sb.Length - 1] != '{') sb.Append(',');
                    sb.Append("\"model\":\"").Append(EscapeJson(model)).Append('\"');
                }
                sb.Append('}');
                _ = publisher.PublishRawAsync("robot/tts/options", sb.ToString());
            }
            ConversationLog.AddEntry(ConversationRole.System, $"TTS options updated (voice={voice}, model={model})", "tester_panel");
        }

        private void PresentWakeWordPrompt() { }

        private void PlayWakeWordPromptClip() { }

        // Unity-side coach reply logic removed; dialog_service is the sole responder.

        // Utility: minimal JSON escaper for inline payload building
        private static string EscapeJson(string value)
        {
            if (string.IsNullOrEmpty(value))
            {
                return string.Empty;
            }

            var sb = new StringBuilder(value.Length + 16);
            for (int i = 0; i < value.Length; i++)
            {
                var ch = value[i];
                switch (ch)
                {
                    case '\\': sb.Append("\\\\"); break;
                    case '\"': sb.Append("\\\""); break;
                    case '\n': sb.Append("\\n"); break;
                    case '\r': sb.Append("\\r"); break;
                    case '\t': sb.Append("\\t"); break;
                    default: sb.Append(ch); break;
                }
            }
            return sb.ToString();
        }


        private void StartWakeWordListeningIndicator()
        {
            // 不再显示倒计时指示
        }

        private void StopWakeWordListeningIndicator()
        {
            if (wakeListeningIndicatorCoroutine != null)
            {
                StopCoroutine(wakeListeningIndicatorCoroutine);
                wakeListeningIndicatorCoroutine = null;
            }

            if (wakeListeningProgressImage != null)
            {
                wakeListeningProgressImage.fillAmount = 0f;
            }

            if (wakeListeningCountdownText != null)
            {
                wakeListeningCountdownText.text = string.Empty;
            }

            if (wakeListeningIndicatorRoot != null)
            {
                wakeListeningIndicatorRoot.SetActive(false);
            }
        }

        private IEnumerator UpdateWakeWordListeningIndicator(float durationSeconds) { yield break; }

        private void UpdateWakeWordIndicatorVisuals(float durationSeconds, float remainingSeconds)
        {
            if (wakeListeningProgressImage != null)
            {
                var progress = Mathf.Clamp01(1f - (remainingSeconds / durationSeconds));
                wakeListeningProgressImage.fillAmount = progress;
            }

            if (wakeListeningCountdownText != null)
            {
                var rounded = Mathf.CeilToInt(remainingSeconds);
                if (rounded < 0)
                {
                    rounded = 0;
                }

                var prefix = string.IsNullOrWhiteSpace(wakeWordPrompt)
                    ? string.Empty
                    : wakeWordPrompt.Trim();

                var countdown = rounded.ToString(CultureInfo.InvariantCulture);
                wakeListeningCountdownText.text = string.IsNullOrEmpty(prefix)
                    ? countdown
                    : $"{prefix} {countdown}";
            }
        }

#if !UNITY_STANDALONE_WIN && !UNITY_EDITOR_WIN
        private void RequestCoachSpeech(string recognisedText, string fallbackGameName)
        {
        }
#endif

        private IEnumerator PlayTtsFromPiper(string text, string voiceCode = null, string modelOverride = null)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                yield break;
            }

            var url = string.IsNullOrWhiteSpace(piperSpeakUrl) ? string.Empty : piperSpeakUrl.Trim();
            if (string.IsNullOrWhiteSpace(url))
            {
                yield break;
            }

            // 通知外部监听者（如 LiveCaptionsListener）进入 TTS 播放，便于抑制回声
            if (publisher != null)
            {
                var payload = "{\"speaking\":true,\"text\":\"" + EscapeJson(text) + "\"}";
                _ = publisher.PublishRawAsync("robot/tts/state", payload);
            }

            // 改用 GET 直取 WAV，避免 POST 卡住
            var separator = url.Contains("?") ? "&" : "?";
            var query = new List<string> { "text=" + UnityWebRequest.EscapeURL(text) };
            if (!string.IsNullOrWhiteSpace(voiceCode))
            {
                query.Add("voice=" + UnityWebRequest.EscapeURL(voiceCode));
            }
            if (!string.IsNullOrWhiteSpace(modelOverride))
            {
                query.Add("model=" + UnityWebRequest.EscapeURL(modelOverride));
            }
            var fullUrl = url + separator + string.Join("&", query);

            // 告知采集端静音，避免自我回录
            if (speechToText != null)
            {
                speechToText.SendMessage("SetPlaybackMute", true, SendMessageOptions.DontRequireReceiver);
            }
            using (var request = UnityWebRequestMultimedia.GetAudioClip(fullUrl, AudioType.WAV))
            {
                request.timeout = 60;
                yield return request.SendWebRequest();

                if (request.result != UnityWebRequest.Result.Success)
                {
                    if (logDebugMessages)
                    {
                        Debug.LogWarning($"[RobotVoice] Piper TTS GET failed: {request.error}");
                    }
                    if (speechToText != null)
                    {
                        speechToText.SendMessage("SetPlaybackMute", false, SendMessageOptions.DontRequireReceiver);
                    }
                    yield break;
                }

                var clip = DownloadHandlerAudioClip.GetContent(request);
                PlayClipOnSource(clip);

                // 等待播放结束后再解除采集静音，避免自我回录
                if (clip != null)
                {
                    if (wakeWordPromptSource != null)
                    {
                        yield return new WaitWhile(() => wakeWordPromptSource.isPlaying);
                    }
                    else
                    {
                        yield return new WaitForSeconds(Mathf.Max(0.05f, clip.length));
                    }
                }
            }
            // 播放完毕后解除静音
            if (speechToText != null)
            {
                speechToText.SendMessage("SetPlaybackMute", false, SendMessageOptions.DontRequireReceiver);
            }

            // 通知外部监听者 TTS 结束
            if (publisher != null)
            {
                var payloadEnd = "{\"speaking\":false}";
                _ = publisher.PublishRawAsync("robot/tts/state", payloadEnd);
            }
        }

        private AudioClip ParsePiperBase64ToClip(string json)
        {
            if (string.IsNullOrWhiteSpace(json))
            {
                return null;
            }

            try
            {
                var node = JSONNode.Parse(json);
                var b64 = node?["audio_wav_base64"]?.Value;
                var sr = node?["sample_rate"]?.AsInt ?? 22050;
                if (string.IsNullOrWhiteSpace(b64))
                {
                    return null;
                }

                var bytes = Convert.FromBase64String(b64);
                return WavToAudioClip(bytes, sr);
            }
            catch (Exception ex)
            {
                if (logDebugMessages)
                {
                    Debug.LogWarning($"[RobotVoice] Failed to parse Piper TTS JSON: {ex.Message}");
                }
                return null;
            }
        }

        private void PlayClipOnSource(AudioClip clip)
        {
            if (clip == null)
            {
                return;
            }

            if (wakeWordPromptSource != null)
            {
                wakeWordPromptSource.Stop();
                wakeWordPromptSource.clip = clip;
                wakeWordPromptSource.loop = false;
                wakeWordPromptSource.Play();
                return;
            }

            var listener = FindObjectOfType<AudioListener>();
            if (listener != null)
            {
                AudioSource.PlayClipAtPoint(clip, listener.transform.position);
            }
        }

        // Minimal WAV decoder (PCM16 mono) -> AudioClip
        private AudioClip WavToAudioClip(byte[] wavData, int sampleRate)
        {
            if (wavData == null || wavData.Length < 44)
            {
                return null;
            }

            // Parse headers (44-byte PCM WAV header assumption)
            int channels = BitConverter.ToInt16(wavData, 22);
            int bitsPerSample = BitConverter.ToInt16(wavData, 34);
            int dataStart = 44;
            int bytesPerSample = bitsPerSample / 8;
            int sampleCount = (wavData.Length - dataStart) / bytesPerSample;
            int unityChannels = Mathf.Max(1, channels);

            if (bitsPerSample != 16)
            {
                if (logDebugMessages)
                {
                    Debug.LogWarning($"[RobotVoice] Piper WAV not 16-bit PCM: {bitsPerSample}b");
                }
            }

            float[] samples = new float[sampleCount];
            int offset = dataStart;
            for (int i = 0; i < sampleCount && offset + 1 < wavData.Length; i++, offset += bytesPerSample)
            {
                short s = BitConverter.ToInt16(wavData, offset);
                samples[i] = s / 32768f;
            }

            var clip = AudioClip.Create("piper_tts", sampleCount / unityChannels, unityChannels, Mathf.Max(8000, sampleRate), false);
            clip.SetData(samples, 0);
            return clip;
        }

        private void RebuildKeywordPhrases(List<string> phrases)
        {
            keywordPhrases.Clear();
            if (phrases == null)
            {
                return;
            }

            foreach (var phrase in phrases)
            {
                if (string.IsNullOrWhiteSpace(phrase))
                {
                    continue;
                }

                var trimmed = phrase.Trim();
                if (trimmed.Length == 0)
                {
                    continue;
                }

                keywordPhrases.Add(new KeywordPhrase
                {
                    Text = trimmed,
                    LowerInvariant = trimmed.ToLowerInvariant()
                });
            }
        }

        private RecognitionMetadata ExtractRecognitionMetadata(string message)
        {
            var metadata = new RecognitionMetadata
            {
                AvgLogProb = float.NaN,
                MaxAmplitude = 0f,
                Rms = 0f,
                Text = string.Empty,
            };

            if (string.IsNullOrWhiteSpace(message))
            {
                return metadata;
            }

            var trimmed = message.Trim();
            if (!trimmed.StartsWith("{", StringComparison.Ordinal))
            {
                metadata.Text = trimmed;
                return metadata;
            }

            try
            {
                var node = JSONNode.Parse(message);
                var obj = node?.AsObject;
                if (obj == null)
                {
                    return metadata;
                }

                if (obj.HasKey("text"))
                {
                    var value = obj["text"].Value;
                    metadata.Text = string.IsNullOrWhiteSpace(value) ? string.Empty : value.Trim();
                }

                if (obj.HasKey("avg_logprob"))
                {
                    var avgNode = obj["avg_logprob"];
                    if (avgNode != null && avgNode.IsNumber)
                    {
                        metadata.AvgLogProb = avgNode.AsFloat;
                    }
                }

                if (obj.HasKey("rms"))
                {
                    var rmsNode = obj["rms"];
                    if (rmsNode != null && rmsNode.IsNumber)
                    {
                        metadata.Rms = Mathf.Clamp01(rmsNode.AsFloat);
                    }
                }

                if (obj.HasKey("max_amplitude"))
                {
                    var amplitudeNode = obj["max_amplitude"];
                    if (amplitudeNode != null && amplitudeNode.IsNumber)
                    {
                        metadata.MaxAmplitude = Mathf.Clamp01(amplitudeNode.AsFloat);
                    }
                }
            }
            catch
            {
                // Ignore malformed metadata and fall back to defaults.
            }

            return metadata;
        }

        private string FilterTranscript(string message, out string rawRecognised)
        {
            rawRecognised = ExtractRecognisedText(message);

            var transcript = ExtractTranscriptFromJson(message);
            if (string.IsNullOrWhiteSpace(transcript))
            {
                transcript = rawRecognised;
            }

            if (string.IsNullOrWhiteSpace(transcript))
            {
                return string.Empty;
            }

            if (keywordPhrases.Count == 0)
            {
                return transcript;
            }

            var filtered = MaskTranscriptToKeywords(transcript);
            if (string.IsNullOrWhiteSpace(filtered))
            {
                return transcript;
            }

            return filtered;
        }

        private string ExtractTranscriptFromJson(string message)
        {
            if (string.IsNullOrWhiteSpace(message))
            {
                return string.Empty;
            }

            var trimmed = message.Trim();
            if (!trimmed.StartsWith("{"))
            {
                return string.Empty;
            }

            try
            {
                var node = JSONNode.Parse(message);
                var obj = node?.AsObject;
                if (obj == null)
                {
                    return string.Empty;
                }

                if (obj.HasKey("text"))
                {
                    return obj["text"].Value;
                }

                if (obj.HasKey("partial"))
                {
                    return obj["partial"].Value;
                }

                if (obj.HasKey("result"))
                {
                    var array = obj["result"].AsArray;
                    if (array != null)
                    {
                        var words = new List<string>();
                        foreach (var item in array.Children)
                        {
                            var word = item["word"]?.Value;
                            if (!string.IsNullOrWhiteSpace(word))
                            {
                                words.Add(word.Trim());
                            }
                        }

                        if (words.Count > 0)
                        {
                            return string.Join(" ", words);
                        }
                    }
                }
            }
            catch
            {
                // Ignore parsing errors and fall back to raw text.
            }

            return string.Empty;
        }

        private string MaskTranscriptToKeywords(string transcript)
        {
            if (string.IsNullOrWhiteSpace(transcript))
            {
                return string.Empty;
            }

            var text = transcript;
            var lower = text.ToLowerInvariant();
            var keep = new bool[text.Length];
            var hasKeyword = false;

            for (int i = 0; i < keywordPhrases.Count; i++)
            {
                var phrase = keywordPhrases[i];
                if (string.IsNullOrEmpty(phrase.LowerInvariant))
                {
                    continue;
                }

                var keyword = phrase.LowerInvariant;
                var searchIndex = 0;

                while (searchIndex < lower.Length)
                {
                    var matchIndex = lower.IndexOf(keyword, searchIndex, StringComparison.Ordinal);
                    if (matchIndex < 0)
                    {
                        break;
                    }

                    for (int j = 0; j < keyword.Length && matchIndex + j < keep.Length; j++)
                    {
                        keep[matchIndex + j] = true;
                    }

                    hasKeyword = true;
                    searchIndex = matchIndex + 1;
                }
            }

            if (!hasKeyword)
            {
                return "*";
            }

            var builder = new StringBuilder(text.Length);
            var lastWasMask = false;

            for (int i = 0; i < text.Length; i++)
            {
                var ch = text[i];
                if (char.IsWhiteSpace(ch))
                {
                    builder.Append(ch);
                    lastWasMask = false;
                }
                else if (keep[i])
                {
                    builder.Append(ch);
                    lastWasMask = false;
                }
                else if (!lastWasMask)
                {
                    builder.Append('*');
                    lastWasMask = true;
                }
            }

            var result = builder.ToString().Trim();

            return string.IsNullOrEmpty(result) ? "*" : result;
        }
        

        private static string RemoveMaskPlaceholders(string recognised)
        {
            if (string.IsNullOrWhiteSpace(recognised))
            {
                return recognised;
            }

            var builder = new StringBuilder(recognised.Length);
            var previousWasSpace = false;

            for (int i = 0; i < recognised.Length; i++)
            {
                var ch = recognised[i];
                if (ch == '*')
                {
                    continue;
                }

                if (char.IsWhiteSpace(ch))
                {
                    if (!previousWasSpace && builder.Length > 0)
                    {
                        builder.Append(' ');
                        previousWasSpace = true;
                    }
                }
                else
                {
                    builder.Append(ch);
                    previousWasSpace = false;
                }
            }

            return builder.ToString().Trim();
        }

        private static string ExtractRecognisedText(string message)
        {
            if (string.IsNullOrWhiteSpace(message))
            {
                return string.Empty;
            }

            var trimmed = message.Trim();
            if (!trimmed.StartsWith("{"))
            {
                return trimmed;
            }

            var key = "\"text\"";
            var keyIndex = trimmed.IndexOf(key, StringComparison.OrdinalIgnoreCase);
            if (keyIndex < 0)
            {
                return trimmed;
            }

            var colonIndex = trimmed.IndexOf(':', keyIndex + key.Length);
            if (colonIndex < 0)
            {
                return trimmed;
            }

            var index = colonIndex + 1;
            while (index < trimmed.Length && char.IsWhiteSpace(trimmed[index]))
            {
                index++;
            }

            if (index >= trimmed.Length || trimmed[index] != '\"')
            {
                return trimmed;
            }

            index++;
            var sb = new StringBuilder();
            while (index < trimmed.Length)
            {
                var ch = trimmed[index++];
                if (ch == '\\')
                {
                    if (index >= trimmed.Length)
                    {
                        break;
                    }

                    var escape = trimmed[index++];
                    switch (escape)
                    {
                        case '"':
                            sb.Append('"');
                            break;
                        case '\\':
                            sb.Append('\\');
                            break;
                        case '/':
                            sb.Append('/');
                            break;
                        case 'b':
                            sb.Append('\b');
                            break;
                        case 'f':
                            sb.Append('\f');
                            break;
                        case 'n':
                            sb.Append('\n');
                            break;
                        case 'r':
                            sb.Append('\r');
                            break;
                        case 't':
                            sb.Append('\t');
                            break;
                        case 'u':
                            if (index + 4 <= trimmed.Length)
                            {
                                var hex = trimmed.Substring(index, 4);
                                if (ushort.TryParse(hex, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var code))
                                {
                                    sb.Append(char.ConvertFromUtf32(code));
                                }
                                index += 4;
                            }
                            break;
                        default:
                            sb.Append(escape);
                            break;
                    }
                }
                else if (ch == '"')
                {
                    break;
                }
                else
                {
                    sb.Append(ch);
                }
            }

            return sb.ToString();
        }

        private string SelectCandidateText(string rawRecognised, string recognised, string masked, string metadataText)
        {
            if (!string.IsNullOrWhiteSpace(rawRecognised))
            {
                return rawRecognised.Trim();
            }

            if (!string.IsNullOrWhiteSpace(recognised))
            {
                return recognised.Trim();
            }

            if (!string.IsNullOrWhiteSpace(metadataText))
            {
                return metadataText.Trim();
            }

            if (!string.IsNullOrWhiteSpace(masked) && masked != "*")
            {
                return masked.Trim();
            }

            return string.Empty;
        }

        private static string NormalizeTranscript(string text)
        {
            return string.IsNullOrWhiteSpace(text) ? string.Empty : text.Trim().ToLowerInvariant();
        }

        private bool ShouldIgnoreTranscriptAsNoise(string text, RecognitionMetadata metadata)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                return true;
            }

            var trimmed = text.Trim();
            if (MatchesCommand(trimmed))
            {
                return false;
            }

            var tokens = trimmed.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
            if (tokens.Length <= 1 && trimmed.Length < 3)
            {
                return true;
            }

            var normalized = trimmed.ToLowerInvariant();
            var effectiveRms = metadata.Rms > 0f ? metadata.Rms : metadata.MaxAmplitude;

            if (NoiseSingles.Contains(normalized) && effectiveRms < speechEnergyNoiseGate)
            {
                return true;
            }

            if (!float.IsNaN(metadata.AvgLogProb) && metadata.AvgLogProb < noiseAverageLogProbThreshold)
            {
                return true;
            }

            return false;
        }

        private bool MatchesCommand(string text)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                return false;
            }

            if (CommandKeywordRegex.IsMatch(text))
            {
                return true;
            }

            if (runtimeConfig != null)
            {
                if (ContainsKeyword(runtimeConfig.LaunchKeywords, text))
                {
                    return true;
                }

                if (ContainsKeyword(runtimeConfig.ExitKeywords, text))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool ContainsKeyword(IEnumerable<string> keywords, string text)
        {
            if (keywords == null)
            {
                return false;
            }

            foreach (var keyword in keywords)
            {
                if (string.IsNullOrWhiteSpace(keyword))
                {
                    continue;
                }

                var trimmed = keyword.Trim();
                if (trimmed.Length == 0)
                {
                    continue;
                }

                if (text.IndexOf(trimmed, StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    return true;
                }
            }

            return false;
        }

        private bool IsDuplicateTranscript(string normalizedText)
        {
            if (string.IsNullOrEmpty(normalizedText))
            {
                return false;
            }

            if (string.IsNullOrEmpty(lastDeliveredTranscript))
            {
                return false;
            }

            var window = Mathf.Max(0.1f, duplicateSuppressionSeconds);
            var elapsed = Time.realtimeSinceStartup - lastDeliveredTranscriptTime;
            return normalizedText == lastDeliveredTranscript && elapsed <= window;
        }

        private void RegisterTranscriptUsage(string normalizedText)
        {
            if (string.IsNullOrEmpty(normalizedText))
            {
                return;
            }

            lastDeliveredTranscript = normalizedText;
            lastDeliveredTranscriptTime = Time.realtimeSinceStartup;
        }
    }
}
