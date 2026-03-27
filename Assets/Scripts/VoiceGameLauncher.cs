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
    public partial class VoiceGameLauncher : MonoBehaviour
    {
        [Header("Dependencies")]
        [SerializeField] private MqttIntentPublisher publisher;
        [SerializeField] private VoskSpeechToText speechToText;
		[SerializeField] private PiMessageHub piHub;
        [Header("Unity Speech Fallback")]
        [SerializeField, Tooltip("If false, Unity-side microphone/ASR fallback stays disabled and the standalone desktop runtime should handle speech input.")]
        private bool enableUnitySpeechInputFallback = false;

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
        [Header("Speech Turn Taking")]
        [SerializeField, Tooltip("If enabled, adjacent ASR chunks are merged and dispatched after a short quiet window to avoid mid-sentence interruption.")]
        private bool deferBackendSpeechDispatch = true;
        [SerializeField, Tooltip("Quiet window before dispatching buffered user speech (seconds)."), Range(0.15f, 2.5f)]
        private float backendSpeechQuietWindowSeconds = 1.05f;
        [SerializeField, Tooltip("Hard max wait for buffered speech dispatch (seconds)."), Range(0.5f, 8f)]
        private float backendSpeechMaxWaitSeconds = 4.2f;
        [SerializeField, Tooltip("If true, command-like speech bypasses buffering and dispatches immediately.")]
        private bool backendSpeechDispatchCommandsImmediately = false;
        [Header("Wake Word Interaction")]
        [SerializeField] private string wakeWordPrompt = "Listening";
        [SerializeField] private AudioSource wakeWordPromptSource;
        [SerializeField] private AudioClip wakeWordPromptClip;
        [SerializeField] private GameObject wakeListeningIndicatorRoot;
        [SerializeField] private Image wakeListeningProgressImage;
        [SerializeField] private Text wakeListeningCountdownText;
        [Header("TTS (Piper)")]
        [SerializeField] private string piperSpeakUrl = VoiceAgentDefaults.PiperSpeakUrl;
        [SerializeField, Tooltip("Optional Kokoro /speak endpoint for Kokoro voices. Leave empty to use piperSpeakUrl.")]
        private string kokoroSpeakUrl = VoiceAgentDefaults.KokoroSpeakUrl;
        [SerializeField, Tooltip("Enable true streaming TTS via /speak_stream (PCM chunked transfer). Falls back to /speak when unavailable.")]
        private bool enableTrueStreamingTts = true;
        [SerializeField, Tooltip("HTTP endpoint for true streaming TTS (raw PCM chunks).")]
        private string piperSpeakStreamUrl = VoiceAgentDefaults.PiperSpeakStreamUrl;
        [SerializeField, Tooltip("Sample rate expected from the /speak_stream endpoint.")]
        private int ttsStreamSampleRate = 22050;
        [SerializeField, Tooltip("How much audio to buffer before starting playback (seconds)."), Range(0.02f, 1.0f)]
        private float ttsStreamStartBufferSeconds = 0.15f;
        [SerializeField, Tooltip("Extra realtime seconds to wait after stream buffer drains before stopping playback (prevents clipped final phonemes)."), Range(0f, 1.2f)]
        private float ttsStreamDrainTailSeconds = 0.28f;
        [SerializeField, Tooltip("Extra realtime seconds to wait after clip playback ends before moving on (reduces clipped ending words)."), Range(0f, 0.25f)]
        private float ttsClipEndPaddingSeconds = 0.06f;
        [SerializeField, Tooltip("In-memory PCM ring buffer size in seconds for true streaming playback."), Range(4, 60)]
        private int ttsStreamRingBufferSeconds = 20;
        [SerializeField, Tooltip("Force a fixed speaker for dialog answer playback (ignores dialog_service tts_speaker).")]
        private string fixedDialogTtsSpeaker = "en_US";
        [SerializeField, Tooltip("Force a fixed style for dialog answer playback (ignores dialog_service tts_instruct). Leave empty to disable instruct.")]
        private string fixedDialogTtsInstruct = string.Empty;
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
        [Header("Backend Voice Pipeline")]
        [SerializeField, Tooltip("Auto-enable MQTT publishing on the MqttIntentPublisher so speech can reach intent_service/dialog_service.")]
        private bool autoEnableMqttPublishing = true;
        [SerializeField, Tooltip("Topic for raw recognised speech text. intent_service subscribes to this topic.")]
        private string voiceTextTopic = VoiceAgentDefaults.VoiceTextTopic;
        [SerializeField, Tooltip("If true, always publish transcript to robot/voice/text and let intent_service handle intent classification (recommended).")]
        private bool preferBackendIntentService = true;
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
        [SerializeField, Tooltip("If true, real user speech can interrupt current TTS playback (barge-in).")]
        private bool enableUserBargeInDuringTts = true;
        [SerializeField, Tooltip("Minimum words required to trigger barge-in during TTS."), Range(1, 6)]
        private int bargeInMinWords = 2;
        [SerializeField, Tooltip("Minimum normalized chars required to trigger barge-in during TTS."), Range(2, 20)]
        private int bargeInMinChars = 6;
        [SerializeField, Tooltip("Minimum energy to trigger barge-in for short phrases (0-1)."), Range(0f, 1f)]
        private float bargeInMinEnergy = 0.03f;
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
        private bool ttsSessionActive = false;
        private bool ttsSessionMuteApplied = false;
        private bool pendingBargeInForNextUserTurn = false;
        private string pendingBargeInInterruptedText = string.Empty;
        private string pendingBargeInInterruptedCorrId = string.Empty;
        private bool ttsWasPlayingLastFrame = false;
        private bool unitySpeechInputFallbackRuntimeEnabled;
        private string testerPanelVoiceCodeOverride = string.Empty;
        private string testerPanelModelPathOverride = string.Empty;
        private Coroutine activeTtsCoroutine;
        private Coroutine pendingSpeechDispatchCoroutine;
        private readonly List<string> pendingSpeechSegments = new List<string>();
        private RecognitionMetadata pendingSpeechMetadata;
        private string pendingSpeechSource = "asr";
        private string pendingSpeechSpeakerKey = string.Empty;
        private string pendingSpeechLastNormalized = string.Empty;
        private float pendingSpeechFirstTs = -1f;
        private float pendingSpeechLastTs = -1f;
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
        private string latestPublishedUserCorrId = string.Empty;
        private float latestPublishedUserCorrTs = -999f;
        private readonly HashSet<string> playedAnswerCorrIds = new HashSet<string>();
        private readonly Queue<string> playedAnswerOrder = new Queue<string>();
        [SerializeField, Tooltip("How many answer corr_ids to remember for de-duping playback.")]
        private int playedAnswerHistorySize = 64;

        private static readonly HashSet<string> NoiseSingles = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "you",
            "hi",
            "hey",
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
            public string EndpointReason;
            public bool HasSpeakerTag;
            public int SpeakerIndex;
            public ulong SpeakerId;
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
            ApplyUnitySpeechInputFallbackRuntime();
            runtimeConfig = BuildRuntimeConfig();
            ApplySpeechKeyPhrases();
            ApplyTurnTakingRuntimeDefaults();
            InitializeDirectConversationRuntime();

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

        private void ApplyUnitySpeechInputFallbackRuntime()
        {
            unitySpeechInputFallbackRuntimeEnabled = enableUnitySpeechInputFallback;
            if (speechToText == null)
            {
                return;
            }

            speechToText.AutoStart = unitySpeechInputFallbackRuntimeEnabled;
            if (!unitySpeechInputFallbackRuntimeEnabled)
            {
                speechToText.SetListeningEnabled(false);
            }
        }

        private bool IsUnitySpeechInputFallbackEnabled()
        {
            return unitySpeechInputFallbackRuntimeEnabled;
        }

        private void SetUnitySpeechInputFallbackEnabled(bool enabled)
        {
            unitySpeechInputFallbackRuntimeEnabled = enabled;
            if (speechToText == null)
            {
                speechToText = GetComponent<VoskSpeechToText>();
            }
            if (speechToText == null)
            {
                return;
            }

            speechToText.AutoStart = enabled;
            if (!enabled)
            {
                speechToText.SetListeningEnabled(false);
            }
        }

        private void ApplyTurnTakingRuntimeDefaults()
        {
            backendSpeechQuietWindowSeconds = Mathf.Clamp(
                Mathf.Max(backendSpeechQuietWindowSeconds, 0.95f),
                0.15f,
                2.5f);
            backendSpeechMaxWaitSeconds = Mathf.Clamp(
                Mathf.Max(backendSpeechMaxWaitSeconds, backendSpeechQuietWindowSeconds + 1.6f),
                0.5f,
                8f);
            // Conservative default: avoid accidental mid-sentence dispatch when text contains command-like words.
            backendSpeechDispatchCommandsImmediately = false;
            enableUserBargeInDuringTts = true;
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
            CancelPendingSpeechDispatch(clearOnly: true);
            queuedSpeakAfterCurrent = null;
            if (activeTtsCoroutine != null)
            {
                StopCoroutine(activeTtsCoroutine);
                activeTtsCoroutine = null;
            }
            if (wakeWordPromptSource != null) wakeWordPromptSource.Stop();
            if (ttsFallbackSource != null) ttsFallbackSource.Stop();
            DisposeDirectConversationRuntime();
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
    }
}

