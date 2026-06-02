using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;
using UnityEngine;
using UnityEngine.Networking;

namespace RobotVoice
{
    public partial class VoiceGameLauncher : MonoBehaviour
    {
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

            var effectiveVoiceCode = string.IsNullOrWhiteSpace(voiceCode)
                ? testerPanelVoiceCodeOverride
                : voiceCode.Trim();
            var effectiveModelPath = string.IsNullOrWhiteSpace(modelPath)
                ? testerPanelModelPathOverride
                : modelPath.Trim();
            if (IsLikelyKokoroVoiceCode(effectiveVoiceCode))
            {
                effectiveModelPath = string.Empty;
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

            var selectedSpeakUrl = ResolveSpeakUrlForVoice(effectiveVoiceCode);
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
                            VoiceCode = effectiveVoiceCode,
                            ModelPath = effectiveModelPath,
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
                StartTtsCoroutine(trimmed, effectiveVoiceCode, effectiveModelPath, ttsInstruct, selectedSpeakUrl);
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
                                   && !IsLikelyKokoroVoiceCode(voiceCode);
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

        private static bool IsLikelyKokoroVoiceCode(string voiceCode)
        {
            var code = string.IsNullOrWhiteSpace(voiceCode) ? string.Empty : voiceCode.Trim();
            if (string.IsNullOrEmpty(code))
            {
                return false;
            }

            return Regex.IsMatch(code, "^[abefhijpzm][fm]_[a-z0-9_]+$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        }

        private static bool IsLikelyQwenVoiceCode(string voiceCode)
        {
            var code = string.IsNullOrWhiteSpace(voiceCode) ? string.Empty : voiceCode.Trim();
            if (string.IsNullOrEmpty(code))
            {
                return false;
            }

            return code.IndexOf("qwen", StringComparison.OrdinalIgnoreCase) >= 0 ||
                   code.IndexOf("kokoro", StringComparison.OrdinalIgnoreCase) >= 0 ||
                   code.IndexOf(":", StringComparison.Ordinal) >= 0;
        }

        private string ResolveSpeakUrlForVoice(string voiceCode)
        {
            if (IsLikelyKokoroVoiceCode(voiceCode) && !string.IsNullOrWhiteSpace(kokoroSpeakUrl))
            {
                return kokoroSpeakUrl.Trim();
            }
            return string.IsNullOrWhiteSpace(piperSpeakUrl) ? string.Empty : piperSpeakUrl.Trim();
        }

        private bool IsOnCooldown()
        {
            return Time.realtimeSinceStartup - lastIntentTime < Mathf.Max(0.1f, intentCooldownSeconds);
        }

        private bool IsCandidateEligibleForBargeIn(string candidateText, RecognitionMetadata metadata)
        {
            if (!enableUserBargeInDuringTts || !IsAsrSuppressionWindow())
            {
                return false;
            }

            if (string.IsNullOrWhiteSpace(candidateText))
            {
                return false;
            }

            if (dropTtsEchoWhileSpeaking && IsLikelyTtsEcho(candidateText, metadata))
            {
                return false;
            }

            var normalized = NormalizeForEchoCompare(candidateText);
            if (string.IsNullOrWhiteSpace(normalized))
            {
                return false;
            }

            var words = normalized.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
            var minWords = Mathf.Max(1, bargeInMinWords);
            if (words.Length >= minWords)
            {
                return true;
            }

            var effectiveEnergy = metadata.Rms > 0f ? metadata.Rms : metadata.MaxAmplitude;
            var minChars = Mathf.Max(2, bargeInMinChars);
            if (normalized.Length >= minChars && effectiveEnergy >= Mathf.Clamp01(bargeInMinEnergy))
            {
                return true;
            }

            return false;
        }

        private bool TryInterruptTtsForBargeIn(string candidateText, RecognitionMetadata metadata)
        {
            if (!IsCandidateEligibleForBargeIn(candidateText, metadata))
            {
                return false;
            }

            var interruptedText = lastTtsText;
            var interruptedCorrId = pendingTtsCorrId;

            if (activeTtsCoroutine != null)
            {
                StopCoroutine(activeTtsCoroutine);
                activeTtsCoroutine = null;
            }

            queuedSpeakAfterCurrent = null;
            if (wakeWordPromptSource != null)
            {
                wakeWordPromptSource.Stop();
            }
            if (ttsFallbackSource != null)
            {
                ttsFallbackSource.Stop();
            }

            CancelActiveDirectConversationTurn(clearQueuedAudio: true, reason: "barge_in");
            EndTtsPlaybackSession(ttsSessionMuteApplied);
            pendingBargeInForNextUserTurn = true;
            pendingBargeInInterruptedText = string.IsNullOrWhiteSpace(interruptedText) ? string.Empty : interruptedText.Trim();
            pendingBargeInInterruptedCorrId = string.IsNullOrWhiteSpace(interruptedCorrId) ? string.Empty : interruptedCorrId.Trim();

            if (logDebugMessages)
            {
                Debug.Log($"[RobotVoice] User barge-in interrupted TTS. user=\"{TruncateForLog(candidateText, 80)}\"");
            }
            return true;
        }

        private void QueueOrPublishVoiceText(string text, RecognitionMetadata metadata, string sourceTag)
        {
            var trimmed = string.IsNullOrWhiteSpace(text) ? string.Empty : text.Trim();
            if (string.IsNullOrWhiteSpace(trimmed))
            {
                return;
            }

            var immediate = !deferBackendSpeechDispatch;
            if (!immediate && backendSpeechDispatchCommandsImmediately && MatchesCommand(trimmed))
            {
                immediate = true;
            }

            if (immediate)
            {
                FlushPendingSpeechDispatchIfAny();
                PublishVoiceTextNow(trimmed, metadata, sourceTag);
                return;
            }

            var speakerKey = BuildSpeakerDispatchKey(metadata);
            if (pendingSpeechSegments.Count > 0 &&
                !string.Equals(pendingSpeechSpeakerKey, speakerKey, StringComparison.Ordinal))
            {
                FlushPendingSpeechDispatchIfAny();
            }

            if (!AppendPendingSpeechSegment(trimmed))
            {
                pendingSpeechLastTs = Time.realtimeSinceStartup;
                return;
            }

            if (pendingSpeechSegments.Count == 1)
            {
                pendingSpeechFirstTs = Time.realtimeSinceStartup;
                pendingSpeechMetadata = metadata;
                pendingSpeechSource = string.IsNullOrWhiteSpace(sourceTag) ? "asr" : sourceTag.Trim();
                pendingSpeechSpeakerKey = speakerKey;
            }
            else
            {
                pendingSpeechMetadata = MergeRecognitionMetadata(pendingSpeechMetadata, metadata);
            }
            pendingSpeechLastTs = Time.realtimeSinceStartup;

            if (pendingSpeechDispatchCoroutine == null)
            {
                pendingSpeechDispatchCoroutine = StartCoroutine(DispatchPendingSpeechWhenQuiet());
            }
        }

        private static string BuildSpeakerDispatchKey(RecognitionMetadata metadata)
        {
            if (!metadata.HasSpeakerTag)
            {
                return "default";
            }

            return $"spk:{Mathf.Max(0, metadata.SpeakerIndex)}:{metadata.SpeakerId.ToString(CultureInfo.InvariantCulture)}";
        }

        private bool AppendPendingSpeechSegment(string segment)
        {
            var normalized = NormalizeTranscript(segment);
            if (string.IsNullOrWhiteSpace(normalized))
            {
                return false;
            }

            if (string.Equals(normalized, pendingSpeechLastNormalized, StringComparison.Ordinal))
            {
                return false;
            }

            if (pendingSpeechSegments.Count > 0)
            {
                var lastIndex = pendingSpeechSegments.Count - 1;
                var lastSegment = pendingSpeechSegments[lastIndex];
                var lastNormalized = NormalizeTranscript(lastSegment);
                if (!string.IsNullOrWhiteSpace(lastNormalized))
                {
                    if (lastNormalized.Contains(normalized))
                    {
                        return false;
                    }

                    if (normalized.Contains(lastNormalized))
                    {
                        pendingSpeechSegments[lastIndex] = segment;
                        pendingSpeechLastNormalized = normalized;
                        return true;
                    }
                }
            }

            pendingSpeechSegments.Add(segment);
            pendingSpeechLastNormalized = normalized;
            return true;
        }

        private float ResolveSpeechDispatchQuietWindowSeconds()
        {
            var baseQuiet = Mathf.Clamp(backendSpeechQuietWindowSeconds, 0.15f, 2.5f);
            if (pendingSpeechSegments.Count <= 0)
            {
                return baseQuiet;
            }

            var merged = string.Join(" ", pendingSpeechSegments).Trim();
            if (string.IsNullOrWhiteSpace(merged))
            {
                return baseQuiet;
            }

            var tokens = merged.Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
            var tokenCount = tokens.Length;
            var endpointReason = (pendingSpeechMetadata.EndpointReason ?? string.Empty).Trim().ToLowerInvariant();
            if (tokenCount <= 2)
            {
                return Mathf.Clamp(baseQuiet * 1.3f, 0.8f, 2.5f);
            }

            var trimmed = merged.TrimEnd();
            var endsWithTerminal = trimmed.EndsWith(".") || trimmed.EndsWith("!") || trimmed.EndsWith("?")
                || trimmed.EndsWith("\u3002") || trimmed.EndsWith("\uFF01") || trimmed.EndsWith("\uFF1F");
            var endsWithContinuationPunct = trimmed.EndsWith(",") || trimmed.EndsWith("\uFF0C")
                || trimmed.EndsWith(";") || trimmed.EndsWith("\uFF1B")
                || trimmed.EndsWith(":") || trimmed.EndsWith("\uFF1A")
                || trimmed.EndsWith("-") || trimmed.EndsWith("\u2014");
            if (pendingSpeechSegments.Count == 1 &&
                (endpointReason.StartsWith("max_length", StringComparison.Ordinal) || endsWithContinuationPunct))
            {
                // ASR likely cut mid-thought; wait longer to merge the continuation.
                return Mathf.Clamp(Mathf.Max(baseQuiet, 5.6f), 0.15f, 8f);
            }
            if (endsWithTerminal && tokenCount >= 8)
            {
                return Mathf.Clamp(baseQuiet * 0.6f, 0.45f, 1.2f);
            }

            var normalized = NormalizeTranscript(merged);
            if (!string.IsNullOrWhiteSpace(normalized))
            {
                var continuationSuffixes = new[]
                {
                    " and",
                    " or",
                    " but",
                    " so",
                    " because",
                    " then",
                    " that",
                    " which",
                    " to",
                    " for",
                    " with",
                    " if",
                    " when",
                    " while",
                };
                for (var i = 0; i < continuationSuffixes.Length; i++)
                {
                    if (normalized.EndsWith(continuationSuffixes[i], StringComparison.Ordinal))
                    {
                        if (pendingSpeechSegments.Count == 1)
                        {
                            return Mathf.Clamp(Mathf.Max(baseQuiet, 4.8f), 0.9f, 8f);
                        }
                        return Mathf.Clamp(baseQuiet * 1.2f, 0.9f, 2.5f);
                    }
                }
            }

            return baseQuiet;
        }

        private float ResolveSpeechDispatchMaxWaitSeconds(float quietWindow)
        {
            var maxWait = Mathf.Clamp(backendSpeechMaxWaitSeconds, 0.5f, 8f);
            var endpointReason = (pendingSpeechMetadata.EndpointReason ?? string.Empty).Trim().ToLowerInvariant();
            if (pendingSpeechSegments.Count >= 2)
            {
                var merged = string.Join(" ", pendingSpeechSegments).Trim();
                var hasTerminal = merged.EndsWith(".") || merged.EndsWith("!") || merged.EndsWith("?")
                    || merged.EndsWith("\u3002") || merged.EndsWith("\uFF01") || merged.EndsWith("\uFF1F");
                if (hasTerminal)
                {
                    return Mathf.Clamp(Mathf.Min(maxWait, quietWindow + 0.9f), quietWindow, 8f);
                }
            }
            if (pendingSpeechSegments.Count == 1 &&
                endpointReason.StartsWith("max_length", StringComparison.Ordinal))
            {
                return Mathf.Clamp(Mathf.Max(maxWait, quietWindow + 1.4f), quietWindow, 8f);
            }
            return Mathf.Max(quietWindow, maxWait);
        }

        private IEnumerator DispatchPendingSpeechWhenQuiet()
        {
            while (true)
            {
                if (pendingSpeechSegments.Count == 0)
                {
                    pendingSpeechDispatchCoroutine = null;
                    yield break;
                }

                var quietWindow = ResolveSpeechDispatchQuietWindowSeconds();
                var maxWait = ResolveSpeechDispatchMaxWaitSeconds(quietWindow);
                var now = Time.realtimeSinceStartup;
                var speechStillActive = unitySpeechInput != null && unitySpeechInput.IsSpeechSegmentActiveForDispatch;
                if (speechStillActive)
                {
                    yield return null;
                    continue;
                }
                var quietElapsed = now - pendingSpeechLastTs;
                var totalElapsed = now - pendingSpeechFirstTs;
                if (quietElapsed >= quietWindow || totalElapsed >= maxWait)
                {
                    FlushPendingSpeechDispatchIfAny();
                    pendingSpeechDispatchCoroutine = null;
                    yield break;
                }

                yield return null;
            }
        }

        private void FlushPendingSpeechDispatchIfAny()
        {
            if (pendingSpeechSegments.Count == 0)
            {
                return;
            }

            var merged = string.Join(" ", pendingSpeechSegments).Trim();
            var metadata = pendingSpeechMetadata;
            var source = pendingSpeechSource;
            pendingSpeechSegments.Clear();
            pendingSpeechSource = "asr";
            pendingSpeechSpeakerKey = string.Empty;
            pendingSpeechLastNormalized = string.Empty;
            pendingSpeechFirstTs = -1f;
            pendingSpeechLastTs = -1f;
            pendingSpeechMetadata = new RecognitionMetadata
            {
                AvgLogProb = float.NaN,
                MaxAmplitude = 0f,
                Rms = 0f,
                Text = string.Empty,
                EndpointReason = string.Empty,
                HasSpeakerTag = false,
                SpeakerIndex = 0,
                SpeakerId = 0UL,
            };

            if (string.IsNullOrWhiteSpace(merged))
            {
                return;
            }

            PublishVoiceTextNow(merged, metadata, source);
        }

        private void CancelPendingSpeechDispatch(bool clearOnly)
        {
            if (!clearOnly)
            {
                FlushPendingSpeechDispatchIfAny();
                return;
            }

            if (pendingSpeechDispatchCoroutine != null)
            {
                StopCoroutine(pendingSpeechDispatchCoroutine);
                pendingSpeechDispatchCoroutine = null;
            }

            pendingSpeechSegments.Clear();
            pendingSpeechSource = "asr";
            pendingSpeechSpeakerKey = string.Empty;
            pendingSpeechLastNormalized = string.Empty;
            pendingSpeechFirstTs = -1f;
            pendingSpeechLastTs = -1f;
            pendingSpeechMetadata = new RecognitionMetadata
            {
                AvgLogProb = float.NaN,
                MaxAmplitude = 0f,
                Rms = 0f,
                Text = string.Empty,
                EndpointReason = string.Empty,
                HasSpeakerTag = false,
                SpeakerIndex = 0,
                SpeakerId = 0UL,
            };
        }

        private static RecognitionMetadata MergeRecognitionMetadata(RecognitionMetadata baseline, RecognitionMetadata candidate)
        {
            var merged = baseline;
            if (!float.IsNaN(candidate.AvgLogProb))
            {
                if (float.IsNaN(merged.AvgLogProb))
                {
                    merged.AvgLogProb = candidate.AvgLogProb;
                }
                else
                {
                    merged.AvgLogProb = Mathf.Max(merged.AvgLogProb, candidate.AvgLogProb);
                }
            }
            merged.MaxAmplitude = Mathf.Max(merged.MaxAmplitude, candidate.MaxAmplitude);
            merged.Rms = Mathf.Max(merged.Rms, candidate.Rms);
            if (candidate.HasSpeakerTag)
            {
                merged.HasSpeakerTag = true;
                merged.SpeakerIndex = candidate.SpeakerIndex;
                merged.SpeakerId = candidate.SpeakerId;
            }
            if (!string.IsNullOrWhiteSpace(candidate.Text))
            {
                merged.Text = candidate.Text;
            }
            if (!string.IsNullOrWhiteSpace(candidate.EndpointReason))
            {
                merged.EndpointReason = candidate.EndpointReason;
            }
            return merged;
        }

        private void PublishVoiceTextNow(string text, RecognitionMetadata metadata, string sourceTag)
        {
            var normalized = NormalizeTranscript(text);
            if (string.IsNullOrWhiteSpace(normalized))
            {
                return;
            }

            if (IsDuplicateTranscript(normalized))
            {
                return;
            }

            ConversationLog.AddEntry(
                ConversationRole.User,
                text,
                BuildUserSpeakerLabel(metadata),
                BuildUserMetadataText(metadata),
                string.IsNullOrWhiteSpace(sourceTag) ? "asr" : sourceTag.Trim());
            PublishVoiceText(text, metadata);
            RegisterTranscriptUsage(normalized);
        }

        private void PublishVoiceText(string text, RecognitionMetadata metadata)
        {
            if (string.IsNullOrWhiteSpace(text)) return;

            var trimmed = text.Trim();
            var corrId = Guid.NewGuid().ToString("N");
            TrackCorrId(corrId, trimmed);

            var bargeIn = pendingBargeInForNextUserTurn;
            var interruptedText = pendingBargeInInterruptedText;
            var interruptedCorrId = pendingBargeInInterruptedCorrId;
            pendingBargeInForNextUserTurn = false;
            pendingBargeInInterruptedText = string.Empty;
            pendingBargeInInterruptedCorrId = string.Empty;

            if (UseDirectUnifiedConversationPipeline())
            {
                DispatchVoiceTextDirectConversation(
                    trimmed,
                    metadata,
                    corrId,
                    bargeIn,
                    interruptedText,
                    interruptedCorrId);
                return;
            }

            if (publisher == null) return;

            var payload = new StringBuilder(256)
                .Append("{\"text\":\"").Append(EscapeJson(trimmed)).Append('"')
                .Append(",\"source\":\"unity_voice\"")
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
            if (metadata.HasSpeakerTag)
            {
                payload.Append(",\"speaker_index\":").Append(Mathf.Max(0, metadata.SpeakerIndex));
            }
            if (metadata.SpeakerId > 0UL)
            {
                payload.Append(",\"speaker_id\":")
                    .Append(metadata.SpeakerId.ToString(CultureInfo.InvariantCulture));
            }
            if (bargeIn)
            {
                payload.Append(",\"barge_in\":true");
                if (!string.IsNullOrWhiteSpace(interruptedText))
                {
                    var interrupted = interruptedText.Length > 240
                        ? interruptedText.Substring(0, 240)
                        : interruptedText;
                    payload.Append(",\"interrupted_tts_text\":\"")
                        .Append(EscapeJson(interrupted))
                        .Append('"');
                }
                if (!string.IsNullOrWhiteSpace(interruptedCorrId))
                {
                    payload.Append(",\"interrupted_tts_corr_id\":\"")
                        .Append(EscapeJson(interruptedCorrId))
                        .Append('"');
                }
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

            var now = Time.realtimeSinceStartup;
            recentCorrToUserText[corrId] = (userText, now);
            recentCorrOrder.Enqueue(corrId);
            latestPublishedUserCorrId = corrId;
            latestPublishedUserCorrTs = now;
            while (recentCorrOrder.Count > Mathf.Max(4, corrHistorySize))
            {
                var old = recentCorrOrder.Dequeue();
                if (recentCorrToUserText.ContainsKey(old))
                {
                    recentCorrToUserText.Remove(old);
                }
            }
        }

        public bool ShouldAcceptDialogAnswer(string corrId)
        {
            if (string.IsNullOrWhiteSpace(corrId))
            {
                return true;
            }

            if (!recentCorrToUserText.TryGetValue(corrId, out var entry))
            {
                return true;
            }

            if (latestPublishedUserCorrTs <= 0f)
            {
                return true;
            }

            var isLatestCorr = string.Equals(latestPublishedUserCorrId, corrId, StringComparison.Ordinal);
            if (isLatestCorr)
            {
                return true;
            }

            // If a newer user utterance has already been published, this answer is stale.
            // Skipping stale replies avoids duplicate/contradicting coach answers for a split long utterance.
            return latestPublishedUserCorrTs <= entry.ts + 0.15f;
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
            var instructToUse = string.IsNullOrWhiteSpace(fixedDialogTtsInstruct) ? null : fixedDialogTtsInstruct.Trim();
            pendingTtsCorrId = string.IsNullOrWhiteSpace(corrId) ? string.Empty : corrId.Trim();
            TriggerSpeakForTester(trimmed, voiceCode: speakerToUse, modelPath: null, ttsInstruct: instructToUse);
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
            testerPanelVoiceCodeOverride = voice;
            testerPanelModelPathOverride = IsLikelyKokoroVoiceCode(voice) ? string.Empty : model;
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
            ConversationLog.AddEntry(
                ConversationRole.System,
                $"TTS options updated (voice={voice}, model={testerPanelModelPathOverride})",
                "tester_panel");
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
            if (unitySpeechInput != null)
            {
                var aecActive = unitySpeechInput.HasActiveAec();
                if (aecActive)
                {
                    shouldMute = muteMicDuringTtsEvenWithAec;
                }
            }

            if (shouldMute && unitySpeechInput != null)
            {
                unitySpeechInput.SetPlaybackMute(true);
            }

            ttsSessionActive = true;
            ttsSessionMuteApplied = shouldMute;
            return shouldMute;
        }

        private void EndTtsPlaybackSession(bool shouldMute)
        {
            var muteApplied = ttsSessionMuteApplied || shouldMute;
            if (muteApplied && unitySpeechInput != null)
            {
                unitySpeechInput.SetPlaybackMute(false);
            }

            if (ttsSessionActive && publisher != null)
            {
                _ = publisher.PublishRawAsync("robot/tts/state", "{\"speaking\":false}");
            }

            ttsSessionActive = false;
            ttsSessionMuteApplied = false;
            lastTtsEndTime = Time.realtimeSinceStartup;
            pendingTtsCorrId = string.Empty;
        }

        private static float EstimateOutputBufferTailSeconds(int sampleRate)
        {
            try
            {
                AudioSettings.GetDSPBufferSize(out var dspBufferLength, out var dspNumBuffers);
                if (dspBufferLength > 0 && dspNumBuffers > 0)
                {
                    var sr = Mathf.Max(8000, sampleRate);
                    var dspSeconds = (dspBufferLength * Mathf.Max(1, dspNumBuffers)) / (float)sr;
                    return Mathf.Clamp(dspSeconds * 2.2f, 0.08f, 0.6f);
                }
            }
            catch
            {
                // Ignore and use conservative fallback below.
            }

            return 0.12f;
        }

        private float ResolveStreamDrainTailSeconds(int sampleRate)
        {
            var configured = Mathf.Clamp(ttsStreamDrainTailSeconds, 0f, 1.2f);
            var estimated = EstimateOutputBufferTailSeconds(sampleRate);
            return Mathf.Clamp(Mathf.Max(configured, estimated), 0.05f, 1.2f);
        }

        private IEnumerator WaitForCurrentClipToFinish(AudioClip fallbackClip = null)
        {
            if (wakeWordPromptSource != null)
            {
                yield return new WaitWhile(() => wakeWordPromptSource.isPlaying);
            }
            else if (ttsFallbackSource != null)
            {
                yield return new WaitWhile(() => ttsFallbackSource.isPlaying);
            }
            else if (fallbackClip != null)
            {
                yield return new WaitForSeconds(Mathf.Max(0.05f, fallbackClip.length));
            }

            var tailPadding = Mathf.Clamp(ttsClipEndPaddingSeconds, 0f, 0.25f);
            if (tailPadding > 0f)
            {
                yield return new WaitForSecondsRealtime(tailPadding);
            }
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

                    var drainTailSeconds = ResolveStreamDrainTailSeconds(sampleRate);
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
                var allowPrefetchNext = ttsStreamPrefetchNext;
                var requestTimeoutSeconds = 60;

                UnityWebRequest nextRequest = null;
                UnityWebRequestAsyncOperation nextOp = null;

                try
                {
                    for (int i = 0; i < segments.Count; i++)
                    {
                        var segText = segments[i];
                        if (string.IsNullOrWhiteSpace(segText)) continue;

                        // Use GET WAV for Piper and Kokoro wrappers; extra params are ignored when unsupported.
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
                                            yield return WaitForCurrentClipToFinish(fallbackClip);
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
                                            yield return WaitForCurrentClipToFinish(fallbackClip);
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
                            yield return WaitForCurrentClipToFinish(clip);
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

    }
}
