using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using System.Globalization;
#if UNITY_STANDALONE_WIN || UNITY_EDITOR_WIN
using System.Speech.Synthesis;
#endif
using UnityEngine;
using UnityEngine.Networking;

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
        [Header("Coach Agent")]
        [SerializeField] private string coachRespondUrl = "http://127.0.0.1:8000/respond";
        [SerializeField] private float coachResponseTimeoutSeconds = 10f;

        private float lastIntentTime = -999f;
        private VoiceIntentConfig runtimeConfig;
        private readonly List<KeywordPhrase> keywordPhrases = new List<KeywordPhrase>();
#if UNITY_STANDALONE_WIN || UNITY_EDITOR_WIN
        private SpeechSynthesizer speechSynthesizer;
        private Coroutine coachSpeechCoroutine;
#endif

        [Serializable]
        private struct CoachRespondPayload
        {
            public string text;
        }

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
#if UNITY_STANDALONE_WIN || UNITY_EDITOR_WIN
            if (coachSpeechCoroutine != null)
            {
                StopCoroutine(coachSpeechCoroutine);
                coachSpeechCoroutine = null;
            }
#endif
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
            if (IsOnCooldown())
            {
                if (logDebugMessages)
                {
                    Debug.Log("[RobotVoice] Ignoring speech because of cooldown");
                }
                return;
            }

            var wakeWordSource = hasKeywordMatch ? recognised : rawRecognised;
            if (string.IsNullOrWhiteSpace(wakeWordSource))
            {
                wakeWordSource = recognised;
            }

            var processed = ApplyWakeWord(wakeWordSource);
            if (processed == null)
            {
                return;
            }

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
                    PublishLaunch(runtimeConfig.ResolveGameName(processed), rawRecognised);
                    return;
                }
            }

            var textForCoach = string.IsNullOrWhiteSpace(processed) ? rawRecognised : processed;
            if (!string.IsNullOrWhiteSpace(textForCoach))
            {
                RequestCoachSpeech(textForCoach, string.Empty);
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
            RequestCoachSpeech(rawText, gameName);
        }

        private void PublishExit(string rawText)
        {
            lastIntentTime = Time.realtimeSinceStartup;
            _ = publisher.PublishExitIntentAsync(rawText);
            RequestCoachSpeech(rawText, string.Empty);
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

            var originalCulture = CultureInfo.CurrentCulture;
            var originalUiCulture = CultureInfo.CurrentUICulture;
            var originalDefaultCulture = CultureInfo.DefaultThreadCurrentCulture;
            var originalDefaultUiCulture = CultureInfo.DefaultThreadCurrentUICulture;

            try
            {
                try
                {
                    CultureInfo.CurrentCulture = CultureInfo.InvariantCulture;
                    CultureInfo.CurrentUICulture = CultureInfo.InvariantCulture;
                    CultureInfo.DefaultThreadCurrentCulture = CultureInfo.InvariantCulture;
                    CultureInfo.DefaultThreadCurrentUICulture = CultureInfo.InvariantCulture;

                    speechSynthesizer = new SpeechSynthesizer();
                    speechSynthesizer.SetOutputToDefaultAudioDevice();
                }
                finally
                {
                    if (originalDefaultCulture != null)
                    {
                        CultureInfo.DefaultThreadCurrentCulture = originalDefaultCulture;
                    }
                    else
                    {
                        CultureInfo.DefaultThreadCurrentCulture = null;
                    }

                    if (originalDefaultUiCulture != null)
                    {
                        CultureInfo.DefaultThreadCurrentUICulture = originalDefaultUiCulture;
                    }
                    else
                    {
                        CultureInfo.DefaultThreadCurrentUICulture = null;
                    }

                    CultureInfo.CurrentCulture = originalCulture;
                    CultureInfo.CurrentUICulture = originalUiCulture;
                }
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

        private void SpeakCoachResponse(string message)
        {
            if (speechSynthesizer == null)
            {
                return;
            }

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
                            if (speechSynthesizer != null)
                            {
                                SpeakCoachResponse(finalReply);
                            }
                            else
                            {
                                Debug.Log($"[RobotVoice] Coach: {finalReply}");
                            }
                        }
                        else if (logDebugMessages)
                        {
                            Debug.LogWarning("[RobotVoice] Coach reply was empty");
                        }
                    }
                    else if (logDebugMessages)
                    {
                        var error = string.IsNullOrWhiteSpace(request.error)
                            ? $"HTTP {(int)request.responseCode}"
                            : request.error;
                        Debug.LogWarning($"[RobotVoice] Failed to fetch coach reply: {error}");
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
#else
        private void InitializeSpeechSynthesizer()
        {
        }

        private void DisposeSpeechSynthesizer()
        {
        }

#endif

#if !UNITY_STANDALONE_WIN && !UNITY_EDITOR_WIN
        private void RequestCoachSpeech(string recognisedText, string fallbackGameName)
        {
        }
#endif

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
