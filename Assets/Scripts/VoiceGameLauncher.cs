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
        [SerializeField, Tooltip("Additional variants accepted as wake word prefixes (case-insensitive)")]
        private string[] wakeWordVariants = new[] { "hi rachel", "hey rachel", "hi richel", "hey richel" };
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
        // removed fixed listening window: we act on first command after wake word
        [SerializeField] private AudioSource wakeWordPromptSource;
        [SerializeField] private AudioClip wakeWordPromptClip;
        [SerializeField] private GameObject wakeListeningIndicatorRoot;
        [SerializeField] private Image wakeListeningProgressImage;
        [SerializeField] private Text wakeListeningCountdownText;
        [Header("Coach Agent")]
        [SerializeField] private string coachRespondUrl = "http://127.0.0.1:8000/respond";
        [SerializeField] private float coachResponseTimeoutSeconds = 10f;
        [SerializeField] private GameObject coachResponsePanel;
        [SerializeField] private Text coachResponseText;
        [SerializeField] [Min(0f)] private float coachResponseDisplaySeconds = 6f;
        [Header("TTS (Piper)")]
        [SerializeField] private string piperSpeakUrl = "http://127.0.0.1:5005/speak";
        [SerializeField, Tooltip("Prompt text sent to LLM when wake word is detected. Keep it short.")]
        private string wakeAcknowledgeUserText = "Wake word detected. Reply briefly that you are listening.";
        [Header("Wake/First Command")]
        [SerializeField, Min(0.5f)] private float firstCommandListenSeconds = 3f;
        private float lastIntentTime = -999f;
        private VoiceIntentConfig runtimeConfig;
        private readonly List<KeywordPhrase> keywordPhrases = new List<KeywordPhrase>();
        private bool awaitingFirstCommand;
        private Coroutine coachSpeechCoroutine;
        private Coroutine coachResponseVisibilityCoroutine;
        // removed expiry timestamp: awaitingFirstCommand controls lifecycle
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
            ApplyFullscreenMode();
            runtimeConfig = BuildRuntimeConfig();
            ApplySpeechKeyPhrases();
            ResetCoachResponseDisplay();
        }

        private void Start()
        {
            // Piper 统一出声，无需 Windows TTS 初始化
        }

        private void OnDestroy()
        {
            if (coachSpeechCoroutine != null)
            {
                StopCoroutine(coachSpeechCoroutine);
                coachSpeechCoroutine = null;
            }
            if (coachResponseVisibilityCoroutine != null)
            {
                StopCoroutine(coachResponseVisibilityCoroutine);
                coachResponseVisibilityCoroutine = null;
            }
            ResetCoachResponseDisplay();
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
            var speech = GetComponent<VoskSpeechToText>();
            if (speech == null)
            {
                return;
            }

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
                TryAdd(runtimeConfig.WakeWord);
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

            TryAddRange(speech.KeyPhrases);

            speech.KeyPhrases = aggregated;
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
            var wakeWordSource = hasKeywordMatch ? recognised : rawRecognised;
            if (string.IsNullOrWhiteSpace(wakeWordSource))
            {
                wakeWordSource = recognised;
            }

            var configuredWakeWord = runtimeConfig?.WakeWord?.Trim();
            var hasWakeWordPrefix = !string.IsNullOrWhiteSpace(wakeWordSource) && MatchesWakeWordPrefix(wakeWordSource, configuredWakeWord);
            var containsWakeWord = !string.IsNullOrWhiteSpace(wakeWordSource) && ContainsWakeWord(wakeWordSource, configuredWakeWord);
            // 仅用于日志观察，不再使用时间窗口

            var textAfterWakeWord = hasWakeWordPrefix
                ? wakeWordSource.Substring(configuredWakeWord.Length).TrimStart()
                : wakeWordSource;

            if ((hasWakeWordPrefix && string.IsNullOrWhiteSpace(textAfterWakeWord)) ||
                (containsWakeWord && string.Equals(wakeWordSource.Trim(), configuredWakeWord, StringComparison.OrdinalIgnoreCase)))
            {
                // 立即进入首命令等待，避免协程调度带来的竞态
                awaitingFirstCommand = true;
                HandleWakeWordOnlyDetected();
                return;
            }

            if (IsOnCooldown())
            {
                if (logDebugMessages)
                {
                    Debug.Log("[RobotVoice] Ignoring speech because of cooldown");
                }
                return;
            }

            // 强制唤醒词门控：未在唤醒窗口且本段不含唤醒前缀，则直接忽略
            if (requireWakeWord && !awaitingFirstCommand && !hasWakeWordPrefix)
            {
                return;
            }

            var processed = ApplyWakeWord(wakeWordSource, hasWakeWordPrefix, textAfterWakeWord);
            if (processed == null && awaitingFirstCommand)
            {
                // 唤醒后无条件放行下一句
                processed = wakeWordSource;
            }
            if (processed == null)
            {
                return;
            }

            if (hasKeywordMatch && (awaitingFirstCommand || hasWakeWordPrefix))
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
                    PublishLaunch(runtimeConfig.ResolveGameName(processed), rawRecognised);
                    return;
                }
            }

            var textForCoach = string.IsNullOrWhiteSpace(processed) ? rawRecognised : processed;
            if (!string.IsNullOrWhiteSpace(textForCoach))
            {
                // 未被唤醒时不允许触发 LLM 回复
                if (!(awaitingFirstCommand || hasWakeWordPrefix))
                {
                    return;
                }

                ClearWakeWordWindow();
                RequestCoachSpeech(textForCoach, string.Empty);
            }
        }

        private bool IsOnCooldown()
        {
            // 唤醒后的第一条命令不受冷却限制
            if (awaitingFirstCommand && IsWakeWordWindowActive())
            {
                return false;
            }

            return Time.realtimeSinceStartup - lastIntentTime < Mathf.Max(0.1f, intentCooldownSeconds);
        }

        private void HandleWakeWordOnlyDetected()
        {
			// 唤醒词响应：同时开花并亮灯（短暂常亮）
			if (piHub != null)
			{
				_ = piHub.OpenFlowerHoldAsync();
				_ = piHub.SendLedRandomAsync();
			}
			// 先让 LLM 回复一句“我在听”（TTS 播放），再开启首条命令监听
			StartCoroutine(WakeThenListenFlow());
        }

        private void TriggerWakeWordRecordingWindow()
        {
            if (speechToText == null)
            {
                return;
            }

            // 不再使用固定时长窗口，由 awaitingFirstCommand 控制生命周期
            speechToText.StartWakeWordWindow(Mathf.Max(0.5f, firstCommandListenSeconds)); // 仍需触发录音启动，给一个很短的窗口
        }

        private IEnumerator PlayPromptThenOpenWakeWindow()
        {
            // 立即开启录音窗口，不等待提示音播放完成
            ActivateWakeWordWindow();
            awaitingFirstCommand = true;
            TriggerWakeWordRecordingWindow();
            yield break;
        }

        private IEnumerator WakeThenListenFlow()
        {
            var ack = string.IsNullOrWhiteSpace(wakeAcknowledgeUserText)
                ? "I'm listening."
                : wakeAcknowledgeUserText.Trim();

            // 显示 + 播放 TTS（若可用）
            ShowCoachResponseOnScreen(ack);
            if (!string.IsNullOrWhiteSpace(piperSpeakUrl))
            {
                yield return PlayTtsFromPiper(ack);
            }

            // TTS 播放后开始监听第一条命令
            ActivateWakeWordWindow();
            awaitingFirstCommand = true;
            TriggerWakeWordRecordingWindow();
        }

        private bool IsWakeWordWindowActive()
        {
            // 改为“只等待第一条命令”，不再依赖倒计时窗口
            return awaitingFirstCommand;
        }

        private void ActivateWakeWordWindow()
        {
            awaitingFirstCommand = true;
        }

        private void ClearWakeWordWindow()
        {
            StopWakeWordListeningIndicator();
            awaitingFirstCommand = false;
        }

        private string ApplyWakeWord(string recognised, bool hasWakeWordPrefix, string textAfterWakeWord)
        {
            var configuredWakeWord = runtimeConfig.WakeWord?.Trim();
            if (string.IsNullOrEmpty(configuredWakeWord))
            {
                return recognised;
            }

            if (hasWakeWordPrefix)
            {
                return textAfterWakeWord;
            }

            if (IsWakeWordWindowActive())
            {
                return recognised;
            }

            if (requireWakeWord && !awaitingFirstCommand)
            {
                if (logDebugMessages)
                {
                    Debug.Log($"[RobotVoice] Wake word '{configuredWakeWord}' missing in '{recognised}'");
                }
                return null;
            }

            return recognised;
        }

        private bool MatchesWakeWordPrefix(string recognised, string configured)
        {
            if (string.IsNullOrWhiteSpace(recognised))
            {
                return false;
            }

            if (!string.IsNullOrEmpty(configured) && recognised.StartsWith(configured, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            if (wakeWordVariants != null)
            {
                for (int i = 0; i < wakeWordVariants.Length; i++)
                {
                    var v = wakeWordVariants[i];
                    if (!string.IsNullOrWhiteSpace(v) && recognised.StartsWith(v.Trim(), StringComparison.OrdinalIgnoreCase))
                    {
                        return true;
                    }
                }
            }

            return false;
        }

        private bool ContainsWakeWord(string recognised, string configured)
        {
            if (string.IsNullOrWhiteSpace(recognised))
            {
                return false;
            }

            if (!string.IsNullOrEmpty(configured) && recognised.IndexOf(configured, StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return true;
            }

            if (wakeWordVariants != null)
            {
                for (int i = 0; i < wakeWordVariants.Length; i++)
                {
                    var v = wakeWordVariants[i];
                    if (!string.IsNullOrWhiteSpace(v) && recognised.IndexOf(v.Trim(), StringComparison.OrdinalIgnoreCase) >= 0)
                    {
                        return true;
                    }
                }
            }

            return false;
        }

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

			// 启动游戏时在屏幕上显示笑脸
			if (piHub != null)
			{
				_ = piHub.SendFaceHappyAsync();
			}

            awaitingFirstCommand = false;
            ClearWakeWordWindow();
            lastIntentTime = Time.realtimeSinceStartup;
            _ = publisher.PublishLaunchIntentAsync(gameName, rawText);
            RequestCoachSpeech(rawText, gameName);
        }

        private void PublishExit(string rawText)
        {
            awaitingFirstCommand = false;
            ClearWakeWordWindow();
            lastIntentTime = Time.realtimeSinceStartup;
            _ = publisher.PublishExitIntentAsync(rawText);
            RequestCoachSpeech(rawText, string.Empty);
        }

        private void PresentWakeWordPrompt() { }

        private void PlayWakeWordPromptClip() { }

        private void RequestCoachSpeech(string recognisedText, string fallbackGameName)
        {
            var trimmedRecognised = string.IsNullOrWhiteSpace(recognisedText)
                ? string.Empty
                : recognisedText.Trim();

            if (string.IsNullOrWhiteSpace(trimmedRecognised))
            {
                trimmedRecognised = string.IsNullOrWhiteSpace(fallbackGameName)
                    ? string.Empty
                    : fallbackGameName.Trim();
            }

            if (string.IsNullOrWhiteSpace(trimmedRecognised))
            {
                return;
            }

            if (coachSpeechCoroutine != null)
            {
                StopCoroutine(coachSpeechCoroutine);
                coachSpeechCoroutine = null;
            }

            coachSpeechCoroutine = StartCoroutine(GenerateAndSpeakCoachReply(trimmedRecognised));
        }

		private IEnumerator GenerateAndSpeakCoachReply(string recognisedText)
        {
            try
            {
                var targetUrl = string.IsNullOrWhiteSpace(coachRespondUrl)
                    ? string.Empty
                    : coachRespondUrl.Trim();

                if (string.IsNullOrWhiteSpace(targetUrl))
                {
                    yield break;
                }

                var payload = new CoachRespondPayload
                {
                    text = recognisedText
                };

                var json = JsonUtility.ToJson(payload);

                using (var request = new UnityWebRequest(targetUrl, UnityWebRequest.kHttpVerbPOST))
                {
                    var bodyRaw = Encoding.UTF8.GetBytes(json);
                    request.uploadHandler = new UploadHandlerRaw(bodyRaw);
                    request.downloadHandler = new DownloadHandlerBuffer();
                    request.SetRequestHeader("Content-Type", "application/json");
                    request.timeout = Mathf.Clamp(Mathf.CeilToInt(Mathf.Max(1f, coachResponseTimeoutSeconds)), 1, 600);

                    yield return request.SendWebRequest();

                    if (request.result == UnityWebRequest.Result.Success)
                    {
                        var reply = ExtractCoachReply(request.downloadHandler?.text);
                        if (!string.IsNullOrWhiteSpace(reply))
                        {
                            var finalReply = reply.Trim();
								ShowCoachResponseOnScreen(finalReply);
								Debug.Log($"[RobotVoice] Coach: {finalReply}");
								if (!string.IsNullOrWhiteSpace(piperSpeakUrl))
								{
									// 在 LLM 的 TTS 播放前触发呼吸灯
									if (piHub != null)
									{
										_ = piHub.SendLedBreathAsync();
									}
									StartCoroutine(PlayTtsFromPiper(finalReply));
								}
                        }
                        else
                        {
                            ResetCoachResponseDisplay();
                            if (logDebugMessages)
                            {
                                Debug.LogWarning("[RobotVoice] Coach reply was empty");
                            }
                        }
                    }
                    else
                    {
                        ResetCoachResponseDisplay();
                        if (logDebugMessages)
                        {
                            var error = string.IsNullOrWhiteSpace(request.error)
                                ? $"HTTP {(int)request.responseCode}"
                                : request.error;
                            Debug.LogWarning($"[RobotVoice] Failed to fetch coach reply: {error}");
                        }
                    }
                }
            }
            finally
            {
                coachSpeechCoroutine = null;
            }
        }

        private string ExtractCoachReply(string json)
        {
            if (string.IsNullOrWhiteSpace(json))
            {
                return string.Empty;
            }

            try
            {
                var node = JSONNode.Parse(json);
                return node?["text"]?.Value ?? string.Empty;
            }
            catch (Exception ex)
            {
                if (logDebugMessages)
                {
                    Debug.LogWarning($"[RobotVoice] Failed to parse coach reply: {ex.Message}");
                }
            }

            return string.Empty;
        }

        private void ShowCoachResponseOnScreen(string message)
        {
            if (coachResponseText == null && coachResponsePanel == null)
            {
                return;
            }

            if (coachResponseVisibilityCoroutine != null)
            {
                StopCoroutine(coachResponseVisibilityCoroutine);
                coachResponseVisibilityCoroutine = null;
            }

            var trimmedMessage = string.IsNullOrWhiteSpace(message) ? string.Empty : message.Trim();

            if (coachResponseText != null)
            {
                coachResponseText.text = trimmedMessage;
            }

            var root = coachResponsePanel != null
                ? coachResponsePanel
                : coachResponseText != null
                    ? coachResponseText.gameObject
                    : null;

            if (root != null)
            {
                root.SetActive(!string.IsNullOrEmpty(trimmedMessage));
            }

            if (!string.IsNullOrEmpty(trimmedMessage) && coachResponseDisplaySeconds > 0f)
            {
                coachResponseVisibilityCoroutine = StartCoroutine(HideCoachResponseAfterDelay(coachResponseDisplaySeconds));
            }
        }

        private IEnumerator HideCoachResponseAfterDelay(float delaySeconds)
        {
            if (delaySeconds > 0f)
            {
                yield return new WaitForSeconds(delaySeconds);
            }

            if (coachResponseText != null)
            {
                coachResponseText.text = string.Empty;
            }

            var root = coachResponsePanel != null
                ? coachResponsePanel
                : coachResponseText != null
                    ? coachResponseText.gameObject
                    : null;

            if (root != null)
            {
                root.SetActive(false);
            }

            coachResponseVisibilityCoroutine = null;
        }

        private void ResetCoachResponseDisplay()
        {
            if (coachResponseVisibilityCoroutine != null)
            {
                StopCoroutine(coachResponseVisibilityCoroutine);
                coachResponseVisibilityCoroutine = null;
            }

            if (coachResponseText != null)
            {
                coachResponseText.text = string.Empty;
            }

            var root = coachResponsePanel != null
                ? coachResponsePanel
                : coachResponseText != null
                    ? coachResponseText.gameObject
                    : null;

            if (root != null)
            {
                root.SetActive(false);
            }
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

        private IEnumerator PlayTtsFromPiper(string text)
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

            // 改用 GET 直取 WAV，避免 POST 卡住
            var fullUrl = url + (url.Contains("?") ? "&" : "?") + "text=" + UnityWebRequest.EscapeURL(text);

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
