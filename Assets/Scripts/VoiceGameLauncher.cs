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
        [SerializeField, Tooltip("Energy gate for common hallucinated short phrases (e.g., 'thank you') when user is not speaking.")]
        private float speechEnergyNoisePhraseGate = 0.01f;
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
        [SerializeField, Tooltip("Optional Qwen /speak endpoint for tester-only Qwen voices. Leave empty to use piperSpeakUrl.")]
        private string qwenSpeakUrl = "http://127.0.0.1:5006/speak";
        [SerializeField, Tooltip("Enable true streaming TTS via /speak_stream (PCM chunked transfer). Falls back to /speak when unavailable.")]
        private bool enableTrueStreamingTts = true;
        [SerializeField, Tooltip("HTTP endpoint for true streaming TTS (raw PCM chunks).")]
        private string piperSpeakStreamUrl = "http://127.0.0.1:5005/speak_stream";
        [SerializeField, Tooltip("Sample rate expected from the /speak_stream endpoint.")]
        private int ttsStreamSampleRate = 22050;
        [SerializeField, Tooltip("How much audio to buffer before starting playback (seconds)."), Range(0.02f, 1.0f)]
        private float ttsStreamStartBufferSeconds = 0.15f;
        [SerializeField, Tooltip("Extra realtime seconds to wait after stream buffer drains before stopping playback (prevents clipped final phonemes)."), Range(0f, 0.5f)]
        private float ttsStreamDrainTailSeconds = 0.12f;
        [SerializeField, Tooltip("In-memory PCM ring buffer size in seconds for true streaming playback."), Range(4, 60)]
        private int ttsStreamRingBufferSeconds = 20;
        [SerializeField, Tooltip("Force a fixed speaker for dialog answer playback (ignores dialog_service tts_speaker).")]
        private string fixedDialogTtsSpeaker = "en_US";
        [SerializeField, Tooltip("Force a fixed style for dialog answer playback (ignores dialog_service tts_instruct). Leave empty to disable instruct.")]
        private string fixedDialogTtsInstruct = string.Empty;
        [SerializeField, Tooltip("If true, dialog answers are always routed to a Piper speaker (Qwen speakers are ignored for main dialog).")]
        private bool forcePiperForDialogAnswers = true;
        [SerializeField, Tooltip("Prompt text sent to LLM when wake word is detected. Keep it short.")]
        private string wakeAcknowledgeUserText = "Wake word detected. Reply briefly that you are listening.";
        [SerializeField, Tooltip("Fallback: mute mic capture while TTS is playing (prevents echo if AEC is not active).")]
        private bool muteMicDuringTtsWhenAecInactive = true;
        [Header("TTS Pseudo-Streaming (CPU-friendly)")]
        [SerializeField, Tooltip("If true, split long TTS into sentence chunks and prefetch the next chunk while playing the current one.")]
        private bool enableTtsPseudoStreaming = true;
        [SerializeField, Tooltip("Only split when text length exceeds this many characters.")]
        [Range(0, 2000)]
        private int ttsStreamSplitMinChars = 110;
        [SerializeField, Tooltip("Maximum characters per chunk (roughly). Smaller chunks reduce time-to-first-audio.")]
        [Range(60, 600)]
        private int ttsStreamChunkMaxChars = 140;
        [SerializeField, Tooltip("If true, start downloading the next chunk while the current chunk is playing.")]
        private bool ttsStreamPrefetchNext = true;
        [SerializeField, Tooltip("For Qwen voices, cap the first TTS chunk length to reduce time-to-first-audio.")]
        [Range(30, 220)]
        private int qwenFirstChunkMaxChars = 52;
        [SerializeField, Tooltip("For Qwen voices, try to keep every chunk below this many chars.")]
        [Range(40, 220)]
        private int qwenChunkMaxChars = 72;
        [SerializeField, Tooltip("HTTP timeout (seconds) for Qwen /speak requests.")]
        [Range(30, 240)]
        private int qwenTtsRequestTimeoutSeconds = 120;
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
        private AudioSource ttsFallbackSource;
        [Header("Echo Rejection (post-AEC)")]
        [SerializeField, Tooltip("If true, drop ASR results that match the last TTS while TTS is playing (prevents self-conversation).")]
        private bool dropTtsEchoWhileSpeaking = true;
        [SerializeField, Tooltip("If true, suppress ASR routing while TTS is playing/tail (strong protection against self-trigger/hallucination).")]
        private bool suppressAsrDuringTtsWindow = true;
        [SerializeField, Tooltip("If true, command-like phrases can still pass during TTS window (barge-in).")]
        private bool allowCommandBargeInDuringTtsWindow = false;
        [SerializeField, Tooltip("If true, keep mic playback-mute enabled even when AEC is active.")]
        private bool muteMicDuringTtsEvenWithAec = true;
        [SerializeField, Tooltip("Extra seconds after TTS ends to keep ASR fully suppressed."), Range(0f, 3f)]
        private float suppressAsrAfterTtsSeconds = 1.2f;
        [SerializeField, Tooltip("If true, tester-panel manual TTS will not be routed back into ASR/dialog.")]
        private bool suppressAsrAfterManualTesterSpeak = true;
        [SerializeField, Tooltip("ASR suppression window (seconds) for tester-panel manual TTS."), Range(0f, 20f)]
        private float manualTesterSpeakSuppressSeconds = 8f;
        [SerializeField, Tooltip("Additional seconds after TTS ends to still drop likely echo.")]
        [Range(0f, 10f)]
        private float ttsEchoTailSeconds = 0.6f;
        [SerializeField, Tooltip("Minimum token overlap (0-1) to consider a transcript as TTS echo during playback.")]
        [Range(0.1f, 1f)]
        private float ttsEchoTokenOverlapThreshold = 0.6f;
        [SerializeField, Tooltip("Minimum characters to apply echo rejection to (avoid dropping short interjections).")]
        private int ttsEchoMinChars = 10;
        [SerializeField, Tooltip("Hard drop: if candidate strongly matches last TTS even after playback ended (handles late ASR buffering).")]
        [Range(0.5f, 1f)]
        private float ttsEchoHardMatchThreshold = 0.85f;
        [SerializeField, Tooltip("Hard drop only when max amplitude is below this (avoid blocking real user barge-in).")]
        [Range(0f, 1f)]
        private float ttsEchoHardMaxAmplitude = 0.25f;
        [SerializeField, Tooltip("How long after TTS started we still allow hard-drop (seconds).")]
        [Range(1f, 120f)]
        private float ttsEchoHardMaxAgeSeconds = 45f;

        private string lastTtsText = string.Empty;
        private string lastTtsTextNorm = string.Empty;
        private float lastTtsStartTime = -999f;
        private float lastTtsEndTime = -999f;
        private string pendingTtsText = string.Empty;
        private string pendingTtsCorrId = string.Empty;
        private bool ttsWasPlayingLastFrame = false;
        private Coroutine activeTtsCoroutine;
        private float manualTesterSpeakSuppressUntil = -999f;
        private PendingSpeakRequest queuedSpeakAfterCurrent;

        // Whisper/transcribe pipelines can emit text a few seconds AFTER playback ends due to buffering/segmentation.
        // Keep a conservative minimum tail window to avoid self-conversation.
        private const float MinEchoTailSeconds = 4f;

        // Correlation for debugging: corr_id -> user text (recent)
        private readonly Dictionary<string, (string text, float ts)> recentCorrToUserText = new Dictionary<string, (string, float)>();
        private readonly Queue<string> recentCorrOrder = new Queue<string>();
        [SerializeField, Tooltip("How many corr_id->user-text mappings to keep for debug logging.")]
        private int corrHistorySize = 32;
        private readonly HashSet<string> playedAnswerCorrIds = new HashSet<string>();
        private readonly Queue<string> playedAnswerOrder = new Queue<string>();
        [SerializeField, Tooltip("How many answer corr_ids to remember for de-duping playback.")]
        private int playedAnswerHistorySize = 64;

        private static readonly HashSet<string> NoiseSingles = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "you",
            "uh",
            "um",
            "yeah",
            "hmm",
        };

        private static readonly HashSet<string> NoisePhrases = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "thank you",
            "thanks",
            "thankyou",
        };

        private static readonly Regex CommandKeywordRegex = new Regex(
            @"\b(open|launch|start|stop|robot)\b",
            RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

        private static readonly HashSet<string> QwenVoices = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
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

        private sealed class PendingSpeakRequest
        {
            public string Text;
            public string VoiceCode;
            public string ModelPath;
            public string TtsInstruct;
            public bool FromTesterPanel;
        }

        private sealed class Pcm16RingBuffer
        {
            private readonly object gate = new object();
            private readonly float[] samples;
            private int readIndex;
            private int writeIndex;
            private int count;
            private bool inputCompleted;
            private bool hasPendingByte;
            private byte pendingByte;

            public Pcm16RingBuffer(int capacitySamples)
            {
                samples = new float[Mathf.Max(1024, capacitySamples)];
            }

            public int BufferedSamples
            {
                get
                {
                    lock (gate)
                    {
                        return count;
                    }
                }
            }

            public bool IsDrained
            {
                get
                {
                    lock (gate)
                    {
                        return inputCompleted && count == 0;
                    }
                }
            }

            public void EnqueuePcm16(byte[] data, int length)
            {
                if (data == null || length <= 0)
                {
                    return;
                }

                lock (gate)
                {
                    var index = 0;
                    if (hasPendingByte)
                    {
                        if (length > 0)
                        {
                            var s = (short)(pendingByte | (data[0] << 8));
                            WriteSampleUnsafe(s / 32768f);
                            index = 1;
                        }
                        hasPendingByte = false;
                    }

                    while (index + 1 < length)
                    {
                        var s = (short)(data[index] | (data[index + 1] << 8));
                        WriteSampleUnsafe(s / 32768f);
                        index += 2;
                    }

                    if (index < length)
                    {
                        pendingByte = data[index];
                        hasPendingByte = true;
                    }
                }
            }

            public void MarkInputCompleted()
            {
                lock (gate)
                {
                    inputCompleted = true;
                    hasPendingByte = false;
                }
            }

            public void ReadInto(float[] output)
            {
                if (output == null)
                {
                    return;
                }

                lock (gate)
                {
                    var toRead = Mathf.Min(output.Length, count);
                    for (var i = 0; i < toRead; i++)
                    {
                        output[i] = samples[readIndex];
                        readIndex++;
                        if (readIndex >= samples.Length) readIndex = 0;
                    }
                    count -= toRead;

                    for (var i = toRead; i < output.Length; i++)
                    {
                        output[i] = 0f;
                    }
                }
            }

            private void WriteSampleUnsafe(float sample)
            {
                if (count >= samples.Length)
                {
                    // Keep newest audio if producer outruns consumer.
                    readIndex++;
                    if (readIndex >= samples.Length) readIndex = 0;
                    count--;
                }

                samples[writeIndex] = sample;
                writeIndex++;
                if (writeIndex >= samples.Length) writeIndex = 0;
                count++;
            }
        }

        private sealed class PcmStreamingDownloadHandler : DownloadHandlerScript
        {
            private readonly Pcm16RingBuffer ringBuffer;

            public PcmStreamingDownloadHandler(Pcm16RingBuffer ringBuffer, int chunkBufferBytes = 8192)
                : base(new byte[Mathf.Max(1024, chunkBufferBytes)])
            {
                this.ringBuffer = ringBuffer;
            }

            protected override bool ReceiveData(byte[] data, int dataLength)
            {
                if (data == null || dataLength <= 0)
                {
                    return true;
                }

                ringBuffer.EnqueuePcm16(data, dataLength);
                return true;
            }

            protected override void CompleteContent()
            {
                ringBuffer.MarkInputCompleted();
            }
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
            // Piper playback is used for voice output; no Windows TTS initialization needed.
        }

        private void Update()
        {
            // Track actual end-of-playback based on AudioSource.isPlaying to avoid relying on clip.length timing.
            var playing =
                (wakeWordPromptSource != null && wakeWordPromptSource.isPlaying) ||
                (ttsFallbackSource != null && ttsFallbackSource.isPlaying);

            if (ttsWasPlayingLastFrame && !playing)
            {
                lastTtsEndTime = Time.realtimeSinceStartup;
            }

            ttsWasPlayingLastFrame = playing;
        }

        private void OnDestroy()
        {
            StopWakeWordListeningIndicator();
            queuedSpeakAfterCurrent = null;
            if (activeTtsCoroutine != null)
            {
                StopCoroutine(activeTtsCoroutine);
                activeTtsCoroutine = null;
            }
            if (wakeWordPromptSource != null) wakeWordPromptSource.Stop();
            if (ttsFallbackSource != null) ttsFallbackSource.Stop();
            // Piper 缁熶竴鍑哄０锛屾棤闇€ Windows TTS 閲婃斁
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

            // Drop very likely hallucinations/noise before any intent/LLM routing.
            if (ShouldIgnoreTranscriptAsNoise(candidateText, metadata))
            {
                if (logDebugMessages && !string.IsNullOrWhiteSpace(candidateText))
                {
                    Debug.Log($"[RobotVoice] Dropped low-signal transcript: \"{candidateText.Trim()}\" (rms={metadata.Rms:0.0000} amp={metadata.MaxAmplitude:0.0000} avg_logprob={metadata.AvgLogProb:0.000})");
                }
                return;
            }

            if (dropTtsEchoWhileSpeaking && IsLikelyTtsEcho(candidateText, metadata))
            {
                if (logDebugMessages && !string.IsNullOrWhiteSpace(candidateText))
                {
                    Debug.Log($"[RobotVoice] Dropped TTS echo: \"{candidateText.Trim()}\" (tts=\"{TruncateForLog(lastTtsText, 80)}\")");
                }
                ConversationLog.AddEntry(ConversationRole.System, candidateText, "dropped_tts_echo");
                return;
            }
            if (suppressAsrDuringTtsWindow && IsAsrSuppressionWindow())
            {
                var isCommandLike = MatchesCommand(candidateText);
                if (!allowCommandBargeInDuringTtsWindow || !isCommandLike)
                {
                    if (logDebugMessages && !string.IsNullOrWhiteSpace(candidateText))
                    {
                        Debug.Log($"[RobotVoice] Suppressed ASR during TTS window: \"{candidateText.Trim()}\"");
                    }
                    return;
                }
            }
            if (suppressAsrAfterManualTesterSpeak && IsManualTesterSpeakSuppressionWindow())
            {
                if (logDebugMessages && !string.IsNullOrWhiteSpace(candidateText))
                {
                    Debug.Log($"[RobotVoice] Suppressed ASR after tester-panel speak: \"{candidateText.Trim()}\"");
                }
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

            // Bypass wake-word gating and use the selected candidate text directly.
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
                    // 鑻ユ病鏈夎В鏋愬嚭娓告垙鍚嶏紝涔熻褰曚竴涓嬬敤鎴锋寚浠ゆ枃鏈紝閬垮厤鈥渙pen鈥濈瓑鏈璁板綍
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

        public void TriggerSpeakForTester(string text, string voiceCode = null, string modelPath = null, string ttsInstruct = null)
        {
            TriggerSpeakInternal(text, voiceCode, modelPath, ttsInstruct, fromTesterPanel: false);
        }

        public void TriggerManualTesterSpeak(string text, string voiceCode = null, string modelPath = null, string ttsInstruct = null)
        {
            TriggerSpeakInternal(text, voiceCode, modelPath, ttsInstruct, fromTesterPanel: true);
        }

        private void TriggerSpeakInternal(string text, string voiceCode, string modelPath, string ttsInstruct, bool fromTesterPanel)
        {
            var trimmed = string.IsNullOrWhiteSpace(text) ? string.Empty : text.Trim();
            if (string.IsNullOrWhiteSpace(trimmed))
            {
                return;
            }
            if (fromTesterPanel)
            {
                pendingTtsCorrId = string.Empty;
            }

            if (fromTesterPanel && suppressAsrAfterManualTesterSpeak)
            {
                manualTesterSpeakSuppressUntil =
                    Time.realtimeSinceStartup + Mathf.Max(0f, manualTesterSpeakSuppressSeconds);
                if (logDebugMessages)
                {
                    Debug.Log($"[RobotVoice] Tester-panel speak ASR suppression armed for {manualTesterSpeakSuppressSeconds:0.0}s");
                }
            }

            if (piHub != null)
            {
                _ = piHub.SendLedBreathAsync();
            }

            var selectedSpeakUrl = ResolveSpeakUrlForVoice(voiceCode);
            if (!string.IsNullOrWhiteSpace(selectedSpeakUrl))
            {
                if (activeTtsCoroutine != null)
                {
                    // Tester manual speak can interrupt immediately; dialog/auto speak is queued to avoid
                    // cutting off current audio midway.
                    if (!fromTesterPanel)
                    {
                        queuedSpeakAfterCurrent = new PendingSpeakRequest
                        {
                            Text = trimmed,
                            VoiceCode = voiceCode,
                            ModelPath = modelPath,
                            TtsInstruct = ttsInstruct,
                            FromTesterPanel = false
                        };
                        if (logDebugMessages)
                        {
                            Debug.Log($"[RobotVoice] TTS busy, queued next speak chars={trimmed.Length}");
                        }
                        return;
                    }

                    queuedSpeakAfterCurrent = null;
                    StopCoroutine(activeTtsCoroutine);
                    activeTtsCoroutine = null;
                }
                StartTtsCoroutine(trimmed, voiceCode, modelPath, ttsInstruct, selectedSpeakUrl);
            }
        }

        private void StartTtsCoroutine(string text, string voiceCode, string modelPath, string ttsInstruct, string selectedSpeakUrl)
        {
            if (string.IsNullOrWhiteSpace(text) || string.IsNullOrWhiteSpace(selectedSpeakUrl))
            {
                return;
            }

            if (wakeWordPromptSource != null) wakeWordPromptSource.Stop();
            if (ttsFallbackSource != null) ttsFallbackSource.Stop();

            var useTrueStreaming = enableTrueStreamingTts
                                   && !string.IsNullOrWhiteSpace(piperSpeakStreamUrl)
                                   && !IsLikelyQwenVoiceCode(voiceCode);
            if (useTrueStreaming)
            {
                activeTtsCoroutine = StartCoroutine(
                    PlayTtsFromPiperTrueStreaming(text, voiceCode, modelPath, ttsInstruct, selectedSpeakUrl));
            }
            else
            {
                activeTtsCoroutine = StartCoroutine(
                    PlayTtsFromPiper(text, voiceCode, modelPath, ttsInstruct, selectedSpeakUrl));
            }
        }

        private void TryPlayQueuedSpeak()
        {
            if (activeTtsCoroutine != null || queuedSpeakAfterCurrent == null)
            {
                return;
            }

            var queued = queuedSpeakAfterCurrent;
            queuedSpeakAfterCurrent = null;
            if (queued == null || string.IsNullOrWhiteSpace(queued.Text))
            {
                return;
            }

            var selectedSpeakUrl = ResolveSpeakUrlForVoice(queued.VoiceCode);
            if (string.IsNullOrWhiteSpace(selectedSpeakUrl))
            {
                return;
            }

            if (queued.FromTesterPanel && suppressAsrAfterManualTesterSpeak)
            {
                manualTesterSpeakSuppressUntil =
                    Time.realtimeSinceStartup + Mathf.Max(0f, manualTesterSpeakSuppressSeconds);
            }
            StartTtsCoroutine(queued.Text, queued.VoiceCode, queued.ModelPath, queued.TtsInstruct, selectedSpeakUrl);
        }

        private static bool IsLikelyQwenVoiceCode(string voiceCode)
        {
            var code = string.IsNullOrWhiteSpace(voiceCode) ? string.Empty : voiceCode.Trim();
            if (string.IsNullOrEmpty(code))
            {
                return false;
            }

            return QwenVoices.Contains(code);
        }

        private string ResolveSpeakUrlForVoice(string voiceCode)
        {
            if (IsLikelyQwenVoiceCode(voiceCode) && !string.IsNullOrWhiteSpace(qwenSpeakUrl))
            {
                return qwenSpeakUrl.Trim();
            }
            return string.IsNullOrWhiteSpace(piperSpeakUrl) ? string.Empty : piperSpeakUrl.Trim();
        }

        private bool IsOnCooldown()
        {
            return Time.realtimeSinceStartup - lastIntentTime < Mathf.Max(0.1f, intentCooldownSeconds);
        }

        private void PublishVoiceText(string text, RecognitionMetadata metadata)
        {
            if (publisher == null) return;
            if (string.IsNullOrWhiteSpace(text)) return;

            var corrId = Guid.NewGuid().ToString("N");
            TrackCorrId(corrId, text.Trim());

            var payload = new StringBuilder(256)
                .Append("{\"text\":\"").Append(EscapeJson(text.Trim())).Append('"')
                .Append(",\"source\":\"unity_whisper\"")
                .Append(",\"corr_id\":\"").Append(corrId).Append('"')
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

        private void TrackCorrId(string corrId, string userText)
        {
            if (string.IsNullOrWhiteSpace(corrId) || string.IsNullOrWhiteSpace(userText))
            {
                return;
            }

            recentCorrToUserText[corrId] = (userText, Time.realtimeSinceStartup);
            recentCorrOrder.Enqueue(corrId);
            while (recentCorrOrder.Count > Mathf.Max(4, corrHistorySize))
            {
                var old = recentCorrOrder.Dequeue();
                if (recentCorrToUserText.ContainsKey(old))
                {
                    recentCorrToUserText.Remove(old);
                }
            }
        }

        private bool MarkAnswerPlayed(string corrId)
        {
            if (string.IsNullOrWhiteSpace(corrId))
            {
                return false;
            }
            if (playedAnswerCorrIds.Contains(corrId))
            {
                return true;
            }
            playedAnswerCorrIds.Add(corrId);
            playedAnswerOrder.Enqueue(corrId);
            while (playedAnswerOrder.Count > Mathf.Max(8, playedAnswerHistorySize))
            {
                var old = playedAnswerOrder.Dequeue();
                playedAnswerCorrIds.Remove(old);
            }
            return false;
        }

        public void PlayDialogAnswerFromService(string answerText, string corrId, string ttsInstruct, string ttsSpeaker)
        {
            var trimmed = string.IsNullOrWhiteSpace(answerText) ? string.Empty : answerText.Trim();
            if (string.IsNullOrWhiteSpace(trimmed))
            {
                return;
            }

            // De-dupe: if the same corr_id answer arrives multiple times, only play once.
            if (MarkAnswerPlayed(corrId))
            {
                if (logDebugMessages)
                {
                    Debug.Log($"[RobotVoice] Skipping duplicate answer playback corr_id={corrId}");
                }
                return;
            }

            if (logDebugMessages)
            {
                string userText = string.Empty;
                if (!string.IsNullOrWhiteSpace(corrId) && recentCorrToUserText.TryGetValue(corrId, out var entry))
                {
                    userText = entry.text;
                    var llmMs = (Time.realtimeSinceStartup - entry.ts) * 1000f;
                    Debug.Log($"[RobotVoice] Dialog latency corr_id={corrId} llm={llmMs:0}ms");
                }
                Debug.Log($"[RobotVoice] Playing answer corr_id={corrId} replying_to=\"{userText}\"");
            }

            var speakerToUse = string.IsNullOrWhiteSpace(fixedDialogTtsSpeaker) ? null : fixedDialogTtsSpeaker.Trim();
            if (forcePiperForDialogAnswers && IsLikelyQwenVoiceCode(speakerToUse))
            {
                speakerToUse = "en_US";
            }
            var instructToUse = string.IsNullOrWhiteSpace(fixedDialogTtsInstruct) ? null : fixedDialogTtsInstruct.Trim();
            pendingTtsCorrId = string.IsNullOrWhiteSpace(corrId) ? string.Empty : corrId.Trim();
            TriggerSpeakForTester(trimmed, voiceCode: speakerToUse, modelPath: null, ttsInstruct: instructToUse);
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
            // Wake-listening indicator is intentionally disabled in this build.
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

        private bool BeginTtsPlaybackSession(string text)
        {
            // Notify external listeners (e.g., LiveCaptionsListener) that TTS is active.
            if (publisher != null)
            {
                var payload = "{\"speaking\":true,\"text\":\"" + EscapeJson(text) + "\"}";
                _ = publisher.PublishRawAsync("robot/tts/state", payload);
            }

            pendingTtsText = text;

            var shouldMute = muteMicDuringTtsWhenAecInactive;
            if (speechToText != null)
            {
                var vosk = speechToText as VoskSpeechToText;
                var aecActive = vosk != null && vosk.HasActiveAec();
                if (aecActive)
                {
                    shouldMute = muteMicDuringTtsEvenWithAec;
                }
            }

            if (shouldMute && speechToText != null)
            {
                speechToText.SendMessage("SetPlaybackMute", true, SendMessageOptions.DontRequireReceiver);
            }

            return shouldMute;
        }

        private void EndTtsPlaybackSession(bool shouldMute)
        {
            if (shouldMute && speechToText != null)
            {
                speechToText.SendMessage("SetPlaybackMute", false, SendMessageOptions.DontRequireReceiver);
            }

            if (publisher != null)
            {
                _ = publisher.PublishRawAsync("robot/tts/state", "{\"speaking\":false}");
            }

            lastTtsEndTime = Time.realtimeSinceStartup;
            pendingTtsCorrId = string.Empty;
        }

        private AudioSource GetOrCreateTtsOutputSource()
        {
            if (wakeWordPromptSource != null)
            {
                return wakeWordPromptSource;
            }

            var ttsSource = ttsFallbackSource;
            if (ttsSource == null)
            {
                var existing = GameObject.Find("TtsOutput");
                var host = existing != null ? existing : new GameObject("TtsOutput");
                ttsSource = host.GetComponent<AudioSource>();
                if (ttsSource == null)
                {
                    ttsSource = host.AddComponent<AudioSource>();
                }
                ttsSource.playOnAwake = false;
                ttsSource.loop = false;
                ttsSource.spatialBlend = 0f;
                ttsFallbackSource = ttsSource;
            }

            return ttsSource;
        }

        private static bool TryGetResponseHeaderInt(UnityWebRequest request, string key, out int value)
        {
            value = 0;
            if (request == null || string.IsNullOrWhiteSpace(key))
            {
                return false;
            }

            var raw = request.GetResponseHeader(key);
            if (string.IsNullOrWhiteSpace(raw))
            {
                return false;
            }

            return int.TryParse(raw.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out value);
        }

        private IEnumerator PlayTtsFromPiperTrueStreaming(
            string text,
            string voiceCode = null,
            string modelOverride = null,
            string ttsInstruct = null,
            string speakBaseUrl = null)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                yield break;
            }

            var streamBaseUrl = string.IsNullOrWhiteSpace(piperSpeakStreamUrl) ? string.Empty : piperSpeakStreamUrl.Trim();
            if (string.IsNullOrWhiteSpace(streamBaseUrl))
            {
                yield return PlayTtsFromPiper(text, voiceCode, modelOverride, ttsInstruct, speakBaseUrl);
                yield break;
            }

            var streamUrl = BuildSpeakStreamUrl(streamBaseUrl, text, voiceCode, modelOverride, ttsInstruct);
            if (string.IsNullOrWhiteSpace(streamUrl))
            {
                yield return PlayTtsFromPiper(text, voiceCode, modelOverride, ttsInstruct, speakBaseUrl);
                yield break;
            }

            var shouldMute = BeginTtsPlaybackSession(text);
            var endSessionInFinally = true;
            var playbackStarted = false;
            var sampleRate = Mathf.Clamp(ttsStreamSampleRate, 8000, 48000);
            var startBufferSeconds = Mathf.Clamp(ttsStreamStartBufferSeconds, 0.02f, 1.0f);
            var startBufferSamples = Mathf.Max(256, Mathf.RoundToInt(sampleRate * startBufferSeconds));
            var ringCapacity = Mathf.Max(sampleRate * 2, sampleRate * Mathf.Max(4, ttsStreamRingBufferSeconds));
            var ringBuffer = new Pcm16RingBuffer(ringCapacity);
            var ttsSource = GetOrCreateTtsOutputSource();

            AudioClip streamClip = null;
            UnityWebRequest request = null;

            try
            {
                var handler = new PcmStreamingDownloadHandler(ringBuffer);
                request = new UnityWebRequest(streamUrl, UnityWebRequest.kHttpVerbGET, handler, null);
                request.timeout = 0;
                request.SetRequestHeader("Accept", "audio/L16,application/octet-stream");
                var op = request.SendWebRequest();

                var responseHeadersRead = false;
                while (!op.isDone)
                {
                    if (!responseHeadersRead)
                    {
                        if (TryGetResponseHeaderInt(request, "X-Audio-Sample-Rate", out var serverSampleRate) &&
                            serverSampleRate >= 8000 &&
                            serverSampleRate <= 48000)
                        {
                            sampleRate = serverSampleRate;
                            startBufferSamples = Mathf.Max(256, Mathf.RoundToInt(sampleRate * startBufferSeconds));
                            responseHeadersRead = true;
                        }
                    }

                    if (!playbackStarted && ringBuffer.BufferedSamples >= startBufferSamples)
                    {
                        streamClip = AudioClip.Create(
                            "TtsPcmStream",
                            sampleRate * 300,
                            1,
                            sampleRate,
                            true,
                            ringBuffer.ReadInto,
                            _ => { });
                        ttsSource.Stop();
                        ttsSource.clip = streamClip;
                        ttsSource.loop = false;
                        ttsSource.Play();
                        MarkTtsStarted(pendingTtsText);
                        playbackStarted = true;
                    }

                    yield return null;
                }

                if (request.result != UnityWebRequest.Result.Success)
                {
                    if (logDebugMessages)
                    {
                        Debug.LogWarning($"[RobotVoice] True streaming TTS failed: code={request.responseCode} err={request.error}. Falling back to /speak");
                    }

                    if (playbackStarted)
                    {
                        ttsSource.Stop();
                    }
                    if (streamClip != null)
                    {
                        Destroy(streamClip);
                        streamClip = null;
                    }

                    EndTtsPlaybackSession(shouldMute);
                    endSessionInFinally = false;
                    activeTtsCoroutine = null;
                    yield return PlayTtsFromPiper(text, voiceCode, modelOverride, ttsInstruct, speakBaseUrl);
                    yield break;
                }

                ringBuffer.MarkInputCompleted();

                if (!playbackStarted && ringBuffer.BufferedSamples > 0)
                {
                    streamClip = AudioClip.Create(
                        "TtsPcmStream",
                        sampleRate * 300,
                        1,
                        sampleRate,
                        true,
                        ringBuffer.ReadInto,
                        _ => { });
                    ttsSource.Stop();
                    ttsSource.clip = streamClip;
                    ttsSource.loop = false;
                    ttsSource.Play();
                    MarkTtsStarted(pendingTtsText);
                    playbackStarted = true;
                }

                if (playbackStarted)
                {
                    var hardDeadline = Time.realtimeSinceStartup + 180f;
                    while (!ringBuffer.IsDrained)
                    {
                        if (Time.realtimeSinceStartup > hardDeadline)
                        {
                            if (logDebugMessages)
                            {
                                Debug.LogWarning("[RobotVoice] True streaming TTS drain timeout; stopping playback");
                            }
                            break;
                        }
                        yield return null;
                    }

                    var drainTailSeconds = Mathf.Clamp(ttsStreamDrainTailSeconds, 0f, 0.5f);
                    if (drainTailSeconds > 0f)
                    {
                        yield return new WaitForSecondsRealtime(drainTailSeconds);
                    }
                    ttsSource.Stop();
                }
            }
            finally
            {
                try { request?.Dispose(); } catch { }
                if (streamClip != null)
                {
                    Destroy(streamClip);
                }
                if (endSessionInFinally)
                {
                    EndTtsPlaybackSession(shouldMute);
                }
                activeTtsCoroutine = null;
                TryPlayQueuedSpeak();
            }
        }

        private IEnumerator PlayTtsFromPiper(
            string text,
            string voiceCode = null,
            string modelOverride = null,
            string ttsInstruct = null,
            string speakBaseUrl = null)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                yield break;
            }

            var url = string.IsNullOrWhiteSpace(speakBaseUrl)
                ? (string.IsNullOrWhiteSpace(piperSpeakUrl) ? string.Empty : piperSpeakUrl.Trim())
                : speakBaseUrl.Trim();
            if (string.IsNullOrWhiteSpace(url))
            {
                yield break;
            }

            var shouldMute = BeginTtsPlaybackSession(text);
            try
            {
                var segments = SplitTtsTextForStreaming(text);
                if (segments == null || segments.Count == 0)
                {
                    yield break;
                }
                var isQwenVoice = IsLikelyQwenVoiceCode(voiceCode);
                if (isQwenVoice)
                {
                    segments = OptimizeQwenFirstChunkForLatency(segments);
                    segments = OptimizeQwenSegmentSizes(segments);
                }
                var allowPrefetchNext = ttsStreamPrefetchNext && !isQwenVoice;
                var requestTimeoutSeconds = isQwenVoice ? Mathf.Clamp(qwenTtsRequestTimeoutSeconds, 30, 240) : 60;
                if (ttsStreamPrefetchNext && !allowPrefetchNext && logDebugMessages)
                {
                    Debug.Log($"[RobotVoice] Disable TTS prefetch for Qwen speaker '{voiceCode}'");
                }

                UnityWebRequest nextRequest = null;
                UnityWebRequestAsyncOperation nextOp = null;

                try
                {
                    for (int i = 0; i < segments.Count; i++)
                    {
                        var segText = segments[i];
                        if (string.IsNullOrWhiteSpace(segText)) continue;

                        // Use GET WAV (works for Piper and Qwen wrapper; extra params are ignored by Piper).
                        UnityWebRequest request;
                        UnityWebRequestAsyncOperation op;
                        if (nextRequest != null)
                        {
                            request = nextRequest;
                            op = nextOp;
                            nextRequest = null;
                            nextOp = null;
                            while (op != null && !op.isDone)
                            {
                                yield return null;
                            }
                        }
                        else
                        {
                            var fullUrl = BuildSpeakUrl(url, segText, voiceCode, modelOverride, ttsInstruct);
                            request = UnityWebRequestMultimedia.GetAudioClip(fullUrl, AudioType.WAV);
                            request.timeout = requestTimeoutSeconds;
                            var t0 = Time.realtimeSinceStartup;
                            yield return request.SendWebRequest();
                            if (logDebugMessages)
                            {
                                var dt = Time.realtimeSinceStartup - t0;
                                Debug.Log(
                                    $"[RobotVoice] TTS fetch seg={i + 1}/{segments.Count} chars={segText.Length} " +
                                    $"voice='{voiceCode}' elapsed={dt:0.00}s code={request.responseCode} err={request.error}");
                            }
                            if (request.result != UnityWebRequest.Result.Success &&
                                isQwenVoice &&
                                !string.IsNullOrWhiteSpace(request.error) &&
                                request.error.IndexOf("timed out", StringComparison.OrdinalIgnoreCase) >= 0)
                            {
                                if (logDebugMessages)
                                {
                                    Debug.LogWarning(
                                        $"[RobotVoice] Qwen TTS timeout, retrying once with longer timeout. segChars={segText.Length}");
                                }
                                request.Dispose();
                                request = UnityWebRequestMultimedia.GetAudioClip(fullUrl, AudioType.WAV);
                                request.timeout = Mathf.Clamp(requestTimeoutSeconds + 60, 60, 240);
                                t0 = Time.realtimeSinceStartup;
                                yield return request.SendWebRequest();
                                if (logDebugMessages)
                                {
                                    var dt = Time.realtimeSinceStartup - t0;
                                    Debug.Log(
                                        $"[RobotVoice] TTS retry seg={i + 1}/{segments.Count} chars={segText.Length} " +
                                        $"voice='{voiceCode}' elapsed={dt:0.00}s code={request.responseCode} err={request.error}");
                                }
                            }
                        }

                        if (request.result != UnityWebRequest.Result.Success)
                        {
                            if (logDebugMessages)
                            {
                                Debug.LogWarning($"[RobotVoice] TTS GET failed: code={request.responseCode} err={request.error}");
                            }
                            // Recovery: when one segment fetch fails, try once with all remaining text as one clip.
                            var remainingText = BuildRemainingSegmentText(segments, i);
                            request.Dispose();
                            if (!string.IsNullOrWhiteSpace(remainingText))
                            {
                                var fallbackUrl = BuildSpeakUrl(url, remainingText, voiceCode, modelOverride, ttsInstruct);
                                if (!string.IsNullOrWhiteSpace(fallbackUrl))
                                {
                                    var fallbackRequest = UnityWebRequestMultimedia.GetAudioClip(fallbackUrl, AudioType.WAV);
                                    fallbackRequest.timeout = Mathf.Clamp(requestTimeoutSeconds + 60, 60, 300);
                                    yield return fallbackRequest.SendWebRequest();
                                    if (fallbackRequest.result == UnityWebRequest.Result.Success)
                                    {
                                        var fallbackClip = DownloadHandlerAudioClip.GetContent(fallbackRequest);
                                        fallbackRequest.Dispose();
                                        if (fallbackClip != null && fallbackClip.samples > 0 && fallbackClip.length > 0.001f)
                                        {
                                            if (logDebugMessages)
                                            {
                                                Debug.Log(
                                                    $"[RobotVoice] TTS segment recovery succeeded from seg={i + 1}/{segments.Count} " +
                                                    $"remainingChars={remainingText.Length}");
                                            }
                                            PlayClipOnSource(fallbackClip);
                                            if (wakeWordPromptSource != null)
                                            {
                                                yield return new WaitWhile(() => wakeWordPromptSource.isPlaying);
                                            }
                                            else if (ttsFallbackSource != null)
                                            {
                                                yield return new WaitWhile(() => ttsFallbackSource.isPlaying);
                                            }
                                            else
                                            {
                                                yield return new WaitForSeconds(Mathf.Max(0.05f, fallbackClip.length));
                                            }
                                            break;
                                        }
                                    }
                                    else
                                    {
                                        if (logDebugMessages)
                                        {
                                            Debug.LogWarning(
                                                $"[RobotVoice] TTS recovery GET failed: code={fallbackRequest.responseCode} err={fallbackRequest.error}");
                                        }
                                        fallbackRequest.Dispose();
                                    }
                                }
                            }
                            yield break;
                        }

                        // Kick off prefetch for next segment before starting playback of this clip.
                        if (allowPrefetchNext && i + 1 < segments.Count)
                        {
                            var nextUrl = BuildSpeakUrl(url, segments[i + 1], voiceCode, modelOverride, ttsInstruct);
                            nextRequest = UnityWebRequestMultimedia.GetAudioClip(nextUrl, AudioType.WAV);
                            nextRequest.timeout = requestTimeoutSeconds;
                            nextOp = nextRequest.SendWebRequest();
                        }

                        var downloadedBytes = 0;
                        try
                        {
                            downloadedBytes = request.downloadHandler != null && request.downloadHandler.data != null
                                ? request.downloadHandler.data.Length
                                : 0;
                        }
                        catch { }

                        var clip = DownloadHandlerAudioClip.GetContent(request);
                        request.Dispose();

                        if (clip == null || clip.samples <= 0 || clip.length <= 0.001f)
                        {
                            if (logDebugMessages)
                            {
                                Debug.LogWarning(
                                    $"[RobotVoice] TTS returned empty/invalid AudioClip seg={i + 1}/{segments.Count} " +
                                    $"bytes={downloadedBytes} voice='{voiceCode}' instructLen={(string.IsNullOrWhiteSpace(ttsInstruct) ? 0 : ttsInstruct.Length)}");
                            }
                            var remainingText = BuildRemainingSegmentText(segments, i);
                            if (!string.IsNullOrWhiteSpace(remainingText))
                            {
                                var fallbackUrl = BuildSpeakUrl(url, remainingText, voiceCode, modelOverride, ttsInstruct);
                                if (!string.IsNullOrWhiteSpace(fallbackUrl))
                                {
                                    var fallbackRequest = UnityWebRequestMultimedia.GetAudioClip(fallbackUrl, AudioType.WAV);
                                    fallbackRequest.timeout = Mathf.Clamp(requestTimeoutSeconds + 60, 60, 300);
                                    yield return fallbackRequest.SendWebRequest();
                                    if (fallbackRequest.result == UnityWebRequest.Result.Success)
                                    {
                                        var fallbackClip = DownloadHandlerAudioClip.GetContent(fallbackRequest);
                                        fallbackRequest.Dispose();
                                        if (fallbackClip != null && fallbackClip.samples > 0 && fallbackClip.length > 0.001f)
                                        {
                                            if (logDebugMessages)
                                            {
                                                Debug.Log(
                                                    $"[RobotVoice] TTS invalid-segment recovery succeeded from seg={i + 1}/{segments.Count} " +
                                                    $"remainingChars={remainingText.Length}");
                                            }
                                            PlayClipOnSource(fallbackClip);
                                            if (wakeWordPromptSource != null)
                                            {
                                                yield return new WaitWhile(() => wakeWordPromptSource.isPlaying);
                                            }
                                            else if (ttsFallbackSource != null)
                                            {
                                                yield return new WaitWhile(() => ttsFallbackSource.isPlaying);
                                            }
                                            else
                                            {
                                                yield return new WaitForSeconds(Mathf.Max(0.05f, fallbackClip.length));
                                            }
                                            break;
                                        }
                                    }
                                    else
                                    {
                                        if (logDebugMessages)
                                        {
                                            Debug.LogWarning(
                                                $"[RobotVoice] TTS invalid-segment recovery GET failed: code={fallbackRequest.responseCode} err={fallbackRequest.error}");
                                        }
                                        fallbackRequest.Dispose();
                                    }
                                }
                            }
                            yield break;
                        }

                        PlayClipOnSource(clip);

                        // Wait for playback to end before continuing to the next segment.
                        if (clip != null)
                        {
                            if (wakeWordPromptSource != null)
                            {
                                yield return new WaitWhile(() => wakeWordPromptSource.isPlaying);
                            }
                            else if (ttsFallbackSource != null)
                            {
                                yield return new WaitWhile(() => ttsFallbackSource.isPlaying);
                            }
                            else
                            {
                                yield return new WaitForSeconds(Mathf.Max(0.05f, clip.length));
                            }
                        }
                    }
                }
                finally
                {
                    try { nextRequest?.Dispose(); } catch { }
                }
            }
            finally
            {
                EndTtsPlaybackSession(shouldMute);
                activeTtsCoroutine = null;
                TryPlayQueuedSpeak();
            }
        }
        private static string BuildSpeakUrl(string baseUrl, string text, string voiceCode, string modelOverride, string ttsInstruct)
        {
            var url = string.IsNullOrWhiteSpace(baseUrl) ? string.Empty : baseUrl.Trim();
            if (string.IsNullOrWhiteSpace(url)) return string.Empty;
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
            if (!string.IsNullOrWhiteSpace(ttsInstruct))
            {
                query.Add("instruct=" + UnityWebRequest.EscapeURL(ttsInstruct));
            }
            return url + separator + string.Join("&", query);
        }

        private static string BuildSpeakStreamUrl(string baseUrl, string text, string voiceCode, string modelOverride, string ttsInstruct)
        {
            var url = string.IsNullOrWhiteSpace(baseUrl) ? string.Empty : baseUrl.Trim();
            if (string.IsNullOrWhiteSpace(url)) return string.Empty;
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
            if (!string.IsNullOrWhiteSpace(ttsInstruct))
            {
                query.Add("instruct=" + UnityWebRequest.EscapeURL(ttsInstruct));
            }
            return url + separator + string.Join("&", query);
        }

        private List<string> SplitTtsTextForStreaming(string text)
        {
            var trimmed = string.IsNullOrWhiteSpace(text) ? string.Empty : text.Trim();
            if (string.IsNullOrWhiteSpace(trimmed)) return new List<string>();

            // If it's already short, don't split.
            // NOTE: this is pseudo-streaming (multiple short requests), not model-level streaming.
            // It improves time-to-first-audio on slow CPU TTS backends.
            if (!enableTtsPseudoStreaming) return new List<string> { trimmed };
            var minChars = Mathf.Max(0, ttsStreamSplitMinChars);
            if (trimmed.Length <= minChars) return new List<string> { trimmed };

            // Sentence-ish splitting (ASCII punctuation + newlines).
            var parts = new List<string>();
            try
            {
                var matches = Regex.Matches(trimmed, @"[^.!?\n]+[.!?\n]?");
                foreach (Match m in matches)
                {
                    var s = m.Value.Trim();
                    if (!string.IsNullOrWhiteSpace(s))
                    {
                        parts.Add(s);
                    }
                }
            }
            catch
            {
                parts.Add(trimmed);
            }

            if (parts.Count <= 1) return new List<string> { trimmed };

            // Group sentences into small chunks to reduce first audio latency.
            var maxChars = Mathf.Clamp(ttsStreamChunkMaxChars, 60, 600);
            var chunks = new List<string>();
            var sb = new StringBuilder();
            for (int i = 0; i < parts.Count; i++)
            {
                var p = parts[i];
                if (sb.Length == 0)
                {
                    sb.Append(p);
                }
                else if (sb.Length + 1 + p.Length <= maxChars)
                {
                    sb.Append(' ').Append(p);
                }
                else
                {
                    chunks.Add(sb.ToString().Trim());
                    sb.Length = 0;
                    sb.Append(p);
                }
            }
            if (sb.Length > 0) chunks.Add(sb.ToString().Trim());
            return chunks.Count > 0 ? chunks : new List<string> { trimmed };
        }

        private List<string> OptimizeQwenFirstChunkForLatency(List<string> segments)
        {
            if (segments == null || segments.Count == 0)
            {
                return segments;
            }

            var first = string.IsNullOrWhiteSpace(segments[0]) ? string.Empty : segments[0].Trim();
            if (string.IsNullOrWhiteSpace(first))
            {
                return segments;
            }

            var maxChars = Mathf.Clamp(qwenFirstChunkMaxChars, 30, 220);
            if (first.Length <= maxChars + 8)
            {
                return segments;
            }

            var splitIndex = FindNaturalSplitIndex(first, maxChars);
            if (splitIndex <= 16 || splitIndex >= first.Length - 8)
            {
                return segments;
            }

            var head = first.Substring(0, splitIndex).Trim();
            var tail = first.Substring(splitIndex).Trim();
            if (string.IsNullOrWhiteSpace(head) || string.IsNullOrWhiteSpace(tail))
            {
                return segments;
            }

            var optimized = new List<string>(segments.Count + 1) { head, tail };
            for (var i = 1; i < segments.Count; i++)
            {
                optimized.Add(segments[i]);
            }

            if (logDebugMessages)
            {
                Debug.Log($"[RobotVoice] Qwen first-chunk optimized: {first.Length} -> {head.Length}+{tail.Length}");
            }
            return optimized;
        }

        private List<string> OptimizeQwenSegmentSizes(List<string> segments)
        {
            if (segments == null || segments.Count == 0)
            {
                return segments;
            }

            var targetMax = Mathf.Clamp(qwenChunkMaxChars, 40, 220);
            var optimized = new List<string>(segments.Count);
            foreach (var raw in segments)
            {
                var part = string.IsNullOrWhiteSpace(raw) ? string.Empty : raw.Trim();
                if (string.IsNullOrWhiteSpace(part))
                {
                    continue;
                }

                if (part.Length <= targetMax + 8)
                {
                    optimized.Add(part);
                    continue;
                }

                var remaining = part;
                while (remaining.Length > targetMax + 8)
                {
                    var split = FindNaturalSplitIndex(remaining, targetMax);
                    if (split <= 16 || split >= remaining.Length - 8)
                    {
                        break;
                    }
                    optimized.Add(remaining.Substring(0, split).Trim());
                    remaining = remaining.Substring(split).Trim();
                    if (string.IsNullOrWhiteSpace(remaining))
                    {
                        break;
                    }
                }
                if (!string.IsNullOrWhiteSpace(remaining))
                {
                    optimized.Add(remaining);
                }
            }
            return optimized.Count > 0 ? optimized : segments;
        }

        private static int FindNaturalSplitIndex(string text, int targetMaxChars)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                return 0;
            }

            var maxChars = Mathf.Clamp(targetMaxChars, 8, text.Length - 1);
            var start = Mathf.Max(8, maxChars - 24);
            var punctuation = ".,!?;: ";

            for (var i = maxChars; i >= start; i--)
            {
                var c = text[i];
                if (char.IsWhiteSpace(c) || punctuation.IndexOf(c) >= 0)
                {
                    return i + 1;
                }
            }

            return maxChars;
        }

        private static string BuildRemainingSegmentText(List<string> segments, int startIndex)
        {
            if (segments == null || segments.Count == 0)
            {
                return string.Empty;
            }

            var begin = Mathf.Clamp(startIndex, 0, segments.Count - 1);
            var sb = new StringBuilder();
            for (var i = begin; i < segments.Count; i++)
            {
                var part = string.IsNullOrWhiteSpace(segments[i]) ? string.Empty : segments[i].Trim();
                if (string.IsNullOrWhiteSpace(part))
                {
                    continue;
                }
                if (sb.Length > 0) sb.Append(' ');
                sb.Append(part);
            }
            return sb.ToString().Trim();
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

            var ttsSource = GetOrCreateTtsOutputSource();
            if (logDebugMessages)
            {
                var sourceLabel = (wakeWordPromptSource != null && ttsSource == wakeWordPromptSource)
                    ? "wakeWordPromptSource"
                    : "dedicated AudioSource";
                Debug.Log($"[RobotVoice] TTS playback via {sourceLabel} on '{ttsSource.gameObject.name}' clipLen={clip.length:0.00}s");
            }
            ttsSource.Stop();
            ttsSource.clip = clip;
            ttsSource.loop = false;
            ttsSource.Play();
            MarkTtsStarted(pendingTtsText);
        }

        private void MarkTtsStarted(string text)
        {
            pendingTtsText = string.Empty;
            lastTtsStartTime = Time.realtimeSinceStartup;
            lastTtsEndTime = -999f;
            lastTtsText = string.IsNullOrWhiteSpace(text) ? string.Empty : text.Trim();
            lastTtsTextNorm = NormalizeForEchoCompare(lastTtsText);
            if (logDebugMessages && !string.IsNullOrWhiteSpace(pendingTtsCorrId) && recentCorrToUserText.TryGetValue(pendingTtsCorrId, out var entry))
            {
                var totalMs = (Time.realtimeSinceStartup - entry.ts) * 1000f;
                Debug.Log($"[RobotVoice] First-audio latency corr_id={pendingTtsCorrId} total={totalMs:0}ms");
            }
            pendingTtsCorrId = string.Empty;
        }

        private bool IsAsrSuppressionWindow()
        {
            if (wakeWordPromptSource != null && wakeWordPromptSource.isPlaying) return true;
            if (ttsFallbackSource != null && ttsFallbackSource.isPlaying) return true;
            var tail = Mathf.Max(0f, suppressAsrAfterTtsSeconds);
            return Time.realtimeSinceStartup - lastTtsEndTime <= tail;
        }

        private bool IsManualTesterSpeakSuppressionWindow()
        {
            return Time.realtimeSinceStartup <= manualTesterSpeakSuppressUntil;
        }

        private bool IsTtsPlayingNow()
        {
            if (wakeWordPromptSource != null && wakeWordPromptSource.isPlaying) return true;
            if (ttsFallbackSource != null && ttsFallbackSource.isPlaying) return true;
            // Tail window after playback ends.
            var tail = Mathf.Max(MinEchoTailSeconds, Mathf.Max(0f, ttsEchoTailSeconds));
            if (Time.realtimeSinceStartup - lastTtsEndTime <= tail) return true;
            return false;
        }

        private bool IsLikelyTtsEcho(string candidateText, RecognitionMetadata metadata)
        {
            if (string.IsNullOrWhiteSpace(candidateText)) return false;
            if (string.IsNullOrWhiteSpace(lastTtsTextNorm)) return false;

            var cand = candidateText.Trim();
            if (cand.Length < Mathf.Max(0, ttsEchoMinChars)) return false;

            var candNorm = NormalizeForEchoCompare(cand);
            if (candNorm.Length == 0) return false;

            // Fast containment check handles most cases (ASR transcribes substrings of TTS).
            if (lastTtsTextNorm.Contains(candNorm) || candNorm.Contains(lastTtsTextNorm))
            {
                // If we're currently in the TTS window, always drop.
                if (IsTtsPlayingNow()) return true;
                // Late ASR buffering: allow hard-drop for a while after TTS started, but only for low-amplitude segments.
                var age = Time.realtimeSinceStartup - lastTtsStartTime;
                if (age <= Mathf.Max(0f, ttsEchoHardMaxAgeSeconds) &&
                    metadata.MaxAmplitude > 0f &&
                    metadata.MaxAmplitude <= Mathf.Clamp01(ttsEchoHardMaxAmplitude))
                {
                    return true;
                }
                // Otherwise don't treat as echo (user might be repeating content later).
                return false;
            }

            // Token overlap (Jaccard) as a fallback.
            // IMPORTANT: For long TTS answers, comparing against the full answer can under-score short ASR chunks.
            // Use the best overlap against sliding windows of the TTS tokens.
            var overlap = BestTokenOverlapAgainstWindows(candNorm, lastTtsTextNorm);
            if (IsTtsPlayingNow())
            {
                if (overlap >= Mathf.Clamp01(ttsEchoTokenOverlapThreshold))
                {
                    return true;
                }
            }
            else
            {
                // Late ASR buffering can surface TTS chunks well after playback ends (segmentation + request latency).
                // For very strong matches, drop anyway as long as:
                // - we're still within a reasonable time since TTS started, and
                // - the captured segment is relatively quiet (echo tends to be quieter than real user speech).
                var age = Time.realtimeSinceStartup - lastTtsStartTime;
                if (age <= Mathf.Max(0f, ttsEchoHardMaxAgeSeconds) &&
                    overlap >= Mathf.Clamp01(ttsEchoHardMatchThreshold) &&
                    metadata.MaxAmplitude > 0f &&
                    metadata.MaxAmplitude <= Mathf.Clamp01(ttsEchoHardMaxAmplitude))
                {
                    return true;
                }
            }

            return false;
        }

        private static string NormalizeForEchoCompare(string text)
        {
            if (string.IsNullOrWhiteSpace(text)) return string.Empty;
            var sb = new StringBuilder(text.Length);
            for (int i = 0; i < text.Length; i++)
            {
                var ch = char.ToLowerInvariant(text[i]);
                if (char.IsLetterOrDigit(ch) || char.IsWhiteSpace(ch))
                {
                    sb.Append(ch);
                }
                else
                {
                    sb.Append(' ');
                }
            }
            return Regex.Replace(sb.ToString(), "\\s+", " ").Trim();
        }

        private static float TokenOverlap(string a, string b)
        {
            if (string.IsNullOrWhiteSpace(a) || string.IsNullOrWhiteSpace(b)) return 0f;
            var sa = new HashSet<string>(a.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries));
            var sb = new HashSet<string>(b.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries));
            if (sa.Count == 0 || sb.Count == 0) return 0f;
            int inter = 0;
            foreach (var t in sa)
            {
                if (sb.Contains(t)) inter++;
            }
            // Use "candidate coverage" instead of Jaccard:
            // For echo detection we care whether MOST of the candidate tokens are present in the TTS window,
            // even if the TTS window contains many extra tokens (long answers).
            //
            // Example: candidate="welcome let's get started at" vs TTS window="welcome let's get started on your..."
            // inter=5, |cand|=6 => 0.83 (good), while Jaccard would be much smaller.
            return sa.Count <= 0 ? 0f : (float)inter / sa.Count;
        }

        private static float BestTokenOverlapAgainstWindows(string candidateNorm, string ttsNorm)
        {
            if (string.IsNullOrWhiteSpace(candidateNorm) || string.IsNullOrWhiteSpace(ttsNorm))
            {
                return 0f;
            }

            var candTokens = candidateNorm.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
            var ttsTokens = ttsNorm.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
            if (candTokens.Length == 0 || ttsTokens.Length == 0)
            {
                return 0f;
            }

            // If TTS is short, just compare full text.
            if (ttsTokens.Length <= 40 || candTokens.Length >= ttsTokens.Length)
            {
                return TokenOverlap(candidateNorm, ttsNorm);
            }

            // Compare candidate against sliding windows of TTS tokens of comparable size.
            // Window is a bit larger than candidate to tolerate minor ASR insertions/deletions.
            var win = Mathf.Clamp(candTokens.Length * 3, 12, 80);
            var step = Mathf.Clamp(candTokens.Length / 2, 1, 8);

            float best = 0f;
            for (int start = 0; start + 1 < ttsTokens.Length; start += step)
            {
                var end = Math.Min(ttsTokens.Length, start + win);
                var windowText = string.Join(" ", ttsTokens, start, end - start);
                var ov = TokenOverlap(candidateNorm, windowText);
                if (ov > best)
                {
                    best = ov;
                    if (best >= 0.9f)
                    {
                        break;
                    }
                }
                if (end >= ttsTokens.Length) break;
            }

            return best;
        }

        private static string TruncateForLog(string text, int max)
        {
            if (string.IsNullOrEmpty(text)) return string.Empty;
            return text.Length <= max ? text : text.Substring(0, max) + "...";
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

            // Normalize punctuation/whitespace for phrase matching.
            var normalized = NormalizeForEchoCompare(trimmed);
            var effectiveRms = metadata.Rms > 0f ? metadata.Rms : metadata.MaxAmplitude;

            if (NoiseSingles.Contains(normalized) && effectiveRms < speechEnergyNoiseGate)
            {
                return true;
            }

            if (NoisePhrases.Contains(normalized) && effectiveRms < Mathf.Max(0f, speechEnergyNoisePhraseGate))
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

