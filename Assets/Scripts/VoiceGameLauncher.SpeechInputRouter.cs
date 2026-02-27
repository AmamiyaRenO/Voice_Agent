using System;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using UnityEngine;

namespace RobotVoice
{
    public partial class VoiceGameLauncher : MonoBehaviour
    {
        public void HandleSpeechResult(string message)
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

            if (preferBackendIntentService)
            {
                var textForBackend = string.IsNullOrWhiteSpace(processed) ? rawRecognised : processed;
                if (!string.IsNullOrWhiteSpace(textForBackend))
                {
                    ConversationLog.AddEntry(ConversationRole.User, textForBackend);
                    PublishVoiceText(textForBackend, metadata);
                }
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

        public bool SetAgentListeningForTester(bool shouldListen)
        {
            if (speechToText == null)
            {
                speechToText = GetComponent<VoskSpeechToText>();
            }

            if (speechToText == null)
            {
                return false;
            }

            speechToText.SetListeningEnabled(shouldListen);
            return true;
        }

        public bool IsAgentListeningForTester()
        {
            if (speechToText == null)
            {
                speechToText = GetComponent<VoskSpeechToText>();
            }

            return speechToText != null && speechToText.IsListening;
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

