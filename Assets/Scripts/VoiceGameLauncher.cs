using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
#if UNITY_STANDALONE_WIN || UNITY_EDITOR_WIN
using System.Speech.Synthesis;
#endif
using UnityEngine;

namespace RobotVoice
{
    public class VoiceGameLauncher : MonoBehaviour
    {
        [Header("Dependencies")]
        [SerializeField] private MqttIntentPublisher publisher;

        [Header("Configuration")]
        [SerializeField] private TextAsset intentConfigJson;
        [SerializeField] private string wakeWord = "hey robot";
        [SerializeField] private bool requireWakeWord = true;
        [SerializeField] private bool requireLaunchKeyword = false;
        [SerializeField] private string[] launchKeywords = { "open", "play" };
        [SerializeField] private string[] exitKeywords = { "quit", "back to lobby" };
        [SerializeField] private SynonymOverride[] synonymOverrides = Array.Empty<SynonymOverride>();
        [SerializeField] private float intentCooldownSeconds = 1.5f;
        [SerializeField] private bool logDebugMessages = true;
        [Header("Speech")]
        [SerializeField] private string[] launchResponseTemplates =
        {
            "I'm opening {0}.",
            "Launching {0} now.",
            "Starting {0}."
        };

        private float lastIntentTime = -999f;
        private VoiceIntentConfig runtimeConfig;
        private readonly List<KeywordPhrase> keywordPhrases = new List<KeywordPhrase>();
#if UNITY_STANDALONE_WIN || UNITY_EDITOR_WIN
        private SpeechSynthesizer speechSynthesizer;
#endif

        private sealed class KeywordPhrase
        {
            public string Text = string.Empty;
            public string LowerInvariant = string.Empty;
        }

        private void Awake()
        {
            ApplyFullscreenMode();
            runtimeConfig = BuildRuntimeConfig();
            ApplySpeechKeyPhrases();
        }

        private void Start()
        {
            InitializeSpeechSynthesizer();
        }

        private void OnDestroy()
        {
            DisposeSpeechSynthesizer();
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
                    : new[] { "quit", "stop" };
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

            var masked = FilterTranscript(message, out var rawRecognisedText);
            if (string.IsNullOrWhiteSpace(masked))
            {
                return;
            }

            if (logDebugMessages)
            {
                Debug.Log($"[RobotVoice] Recognised: {masked}");
            }

            if (masked == "*")
            {
                return;
            }

            var recognised = RemoveMaskPlaceholders(masked);
            if (string.IsNullOrWhiteSpace(recognised))
            {
                return;
            }

            var rawRecognised = string.IsNullOrWhiteSpace(rawRecognisedText)
                ? recognised
                : rawRecognisedText.Trim();

            recognised = recognised.Trim();
            if (IsOnCooldown())
            {
                if (logDebugMessages)
                {
                    Debug.Log("[RobotVoice] Ignoring speech because of cooldown");
                }
                return;
            }

            var processed = ApplyWakeWord(recognised);
            if (processed == null)
            {
                return;
            }

            if (IsExitIntent(processed))
            {
                PublishExit(rawRecognised);
                return;
            }

            if (TryExtractGameName(processed, out var gameName))
            {
                PublishLaunch(gameName, rawRecognised);
            }
            else if (!requireLaunchKeyword && !string.IsNullOrWhiteSpace(processed))
            {
                PublishLaunch(runtimeConfig.ResolveGameName(processed), rawRecognised);
            }
        }

        private bool IsOnCooldown()
        {
            return Time.realtimeSinceStartup - lastIntentTime < Mathf.Max(0.1f, intentCooldownSeconds);
        }

        private string ApplyWakeWord(string recognised)
        {
            var configuredWakeWord = runtimeConfig.WakeWord?.Trim();
            if (string.IsNullOrEmpty(configuredWakeWord))
            {
                return recognised;
            }

            if (recognised.StartsWith(configuredWakeWord, StringComparison.OrdinalIgnoreCase))
            {
                return recognised.Substring(configuredWakeWord.Length).TrimStart();
            }

            if (requireWakeWord)
            {
                if (logDebugMessages)
                {
                    Debug.Log($"[RobotVoice] Wake word '{configuredWakeWord}' missing in '{recognised}'");
                }
                return null;
            }

            return recognised;
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

            lastIntentTime = Time.realtimeSinceStartup;
            _ = publisher.PublishLaunchIntentAsync(gameName, rawText);
            SpeakLaunchResponse(gameName);
        }

        private void PublishExit(string rawText)
        {
            lastIntentTime = Time.realtimeSinceStartup;
            _ = publisher.PublishExitIntentAsync(rawText);
        }

#if UNITY_STANDALONE_WIN || UNITY_EDITOR_WIN
        private void InitializeSpeechSynthesizer()
        {
            if (speechSynthesizer != null)
            {
                return;
            }

            if (Application.platform != RuntimePlatform.WindowsPlayer &&
                Application.platform != RuntimePlatform.WindowsEditor)
            {
                return;
            }

            try
            {
                speechSynthesizer = new SpeechSynthesizer();
                speechSynthesizer.SetOutputToDefaultAudioDevice();
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[RobotVoice] Failed to initialise Windows speech synthesizer: {ex.Message}");
                speechSynthesizer = null;
            }
        }

        private void DisposeSpeechSynthesizer()
        {
            if (speechSynthesizer == null)
            {
                return;
            }

            try
            {
                speechSynthesizer.SpeakAsyncCancelAll();
                speechSynthesizer.Dispose();
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[RobotVoice] Failed to dispose Windows speech synthesizer: {ex.Message}");
            }
            finally
            {
                speechSynthesizer = null;
            }
        }

        private void SpeakLaunchResponse(string gameName)
        {
            if (speechSynthesizer == null)
            {
                return;
            }

            var message = BuildLaunchResponse(gameName);
            if (string.IsNullOrWhiteSpace(message))
            {
                return;
            }

            try
            {
                speechSynthesizer.SpeakAsyncCancelAll();
                speechSynthesizer.SpeakAsync(message);
            }
            catch (Exception ex)
            {
                Debug.LogWarning($"[RobotVoice] Failed to speak launch response: {ex.Message}");
            }
        }
#else
        private void InitializeSpeechSynthesizer()
        {
        }

        private void DisposeSpeechSynthesizer()
        {
        }

        private void SpeakLaunchResponse(string gameName)
        {
        }
#endif

        private string BuildLaunchResponse(string gameName)
        {
            if (string.IsNullOrWhiteSpace(gameName))
            {
                return string.Empty;
            }

            var trimmedName = gameName.Trim();
            if (trimmedName.Length == 0)
            {
                return string.Empty;
            }

            string selectedTemplate = null;
            if (launchResponseTemplates != null && launchResponseTemplates.Length > 0)
            {
                var candidates = new List<string>();
                for (int i = 0; i < launchResponseTemplates.Length; i++)
                {
                    var template = launchResponseTemplates[i];
                    if (string.IsNullOrWhiteSpace(template))
                    {
                        continue;
                    }

                    var trimmedTemplate = template.Trim();
                    if (trimmedTemplate.Length == 0)
                    {
                        continue;
                    }

                    candidates.Add(trimmedTemplate);
                }

                if (candidates.Count > 0)
                {
                    var index = UnityEngine.Random.Range(0, candidates.Count);
                    selectedTemplate = candidates[index];
                }
            }

            if (string.IsNullOrWhiteSpace(selectedTemplate))
            {
                return $"I'm opening {trimmedName}.";
            }

            try
            {
                return string.Format(CultureInfo.InvariantCulture, selectedTemplate, trimmedName);
            }
            catch (FormatException ex)
            {
                if (logDebugMessages)
                {
                    Debug.LogWarning($"[RobotVoice] Launch response template '{selectedTemplate}' is invalid: {ex.Message}");
                }

                return $"I'm opening {trimmedName}.";
            }
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
    }
}
