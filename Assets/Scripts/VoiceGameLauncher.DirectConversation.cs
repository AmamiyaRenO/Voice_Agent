using System;
using System.Collections;
using System.Collections.Generic;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RobotVoice
{
    public partial class VoiceGameLauncher : MonoBehaviour
    {
        [Header("Direct Conversation")]
        [SerializeField, Tooltip("Conversation pipeline mode: direct_unified or legacy_mqtt.")]
        private string conversationPipelineMode = "direct_unified";
        [SerializeField, Tooltip("Conversation profile: local or cloud.")]
        private string conversationProfile = "local";
        [SerializeField, Tooltip("Streaming endpoint for the unified direct conversation pipeline.")]
        private string conversationTurnStreamUrl = VoiceAgentDefaults.ConversationTurnStreamUrl;
        [SerializeField, Tooltip("Apply low-latency turn-taking defaults when direct_unified is active.")]
        private bool tuneTurnTakingForDirectConversation = true;

        private sealed class DirectConversationSpeakChunk
        {
            public string CorrId = string.Empty;
            public string Text = string.Empty;
        }

        private static readonly HttpClient DirectConversationHttpClient = BuildDirectConversationHttpClient();
        private readonly Queue<DirectConversationSpeakChunk> pendingDirectConversationSpeakChunks = new Queue<DirectConversationSpeakChunk>();
        private SynchronizationContext directConversationMainThreadContext;
        private CancellationTokenSource activeConversationTurnCts;
        private Task activeConversationTurnTask;
        private Coroutine directConversationSpeakDrainCoroutine;
        private string activeConversationCorrId = string.Empty;
        private string activeConversationRoute = string.Empty;
        private string lastQueuedDirectConversationNormalized = string.Empty;
        private bool directConversationTurnStreamActive = false;
        private bool directConversationRuntimeDefaultsCaptured = false;
        private bool defaultDeferBackendSpeechDispatch;
        private float defaultBackendSpeechQuietWindowSeconds;
        private float defaultBackendSpeechMaxWaitSeconds;
        private bool defaultBackendSpeechDispatchCommandsImmediately;
        private bool defaultEnableUserBargeInDuringTts;
        private bool defaultAllowCommandBargeInDuringTtsWindow;
        private bool defaultMuteMicDuringTtsEvenWithAec;
        private float defaultSuppressAsrAfterTtsSeconds;

        private static HttpClient BuildDirectConversationHttpClient()
        {
            var client = new HttpClient();
            client.Timeout = Timeout.InfiniteTimeSpan;
            return client;
        }

        private static string NormalizeConversationPipelineMode(string value)
        {
            var normalized = string.IsNullOrWhiteSpace(value) ? string.Empty : value.Trim().ToLowerInvariant();
            switch (normalized)
            {
                case "legacy":
                case "mqtt":
                case "legacy_mqtt":
                    return "legacy_mqtt";
                default:
                    return "direct_unified";
            }
        }

        private static string NormalizeConversationProfile(string value)
        {
            var normalized = string.IsNullOrWhiteSpace(value) ? string.Empty : value.Trim().ToLowerInvariant();
            switch (normalized)
            {
                case "cloud":
                case "openai":
                case "online":
                    return "cloud";
                default:
                    return "local";
            }
        }

        private bool UseDirectUnifiedConversationPipeline()
        {
            return string.Equals(
                NormalizeConversationPipelineMode(conversationPipelineMode),
                "direct_unified",
                StringComparison.OrdinalIgnoreCase);
        }

        public string GetConversationPipelineModeForTester()
        {
            return NormalizeConversationPipelineMode(conversationPipelineMode);
        }

        public string GetConversationProfileForTester()
        {
            return NormalizeConversationProfile(conversationProfile);
        }

        public void ApplyConversationRuntimeConfigForTester(string pipelineMode, string profile)
        {
            conversationPipelineMode = NormalizeConversationPipelineMode(pipelineMode);
            conversationProfile = NormalizeConversationProfile(profile);
            ApplyDirectConversationRuntimeDefaults();
            if (logDebugMessages)
            {
                Debug.Log($"[RobotVoice] Conversation runtime updated pipeline={conversationPipelineMode} profile={conversationProfile}");
            }
        }

        private void InitializeDirectConversationRuntime()
        {
            directConversationMainThreadContext = SynchronizationContext.Current;
            ApplyDirectConversationRuntimeDefaults();
        }

        private void CaptureDirectConversationRuntimeDefaults()
        {
            if (directConversationRuntimeDefaultsCaptured)
            {
                return;
            }

            directConversationRuntimeDefaultsCaptured = true;
            defaultDeferBackendSpeechDispatch = deferBackendSpeechDispatch;
            defaultBackendSpeechQuietWindowSeconds = backendSpeechQuietWindowSeconds;
            defaultBackendSpeechMaxWaitSeconds = backendSpeechMaxWaitSeconds;
            defaultBackendSpeechDispatchCommandsImmediately = backendSpeechDispatchCommandsImmediately;
            defaultEnableUserBargeInDuringTts = enableUserBargeInDuringTts;
            defaultAllowCommandBargeInDuringTtsWindow = allowCommandBargeInDuringTtsWindow;
            defaultMuteMicDuringTtsEvenWithAec = muteMicDuringTtsEvenWithAec;
            defaultSuppressAsrAfterTtsSeconds = suppressAsrAfterTtsSeconds;
        }

        private void RestoreDirectConversationRuntimeDefaults()
        {
            if (!directConversationRuntimeDefaultsCaptured)
            {
                return;
            }

            deferBackendSpeechDispatch = defaultDeferBackendSpeechDispatch;
            backendSpeechQuietWindowSeconds = defaultBackendSpeechQuietWindowSeconds;
            backendSpeechMaxWaitSeconds = defaultBackendSpeechMaxWaitSeconds;
            backendSpeechDispatchCommandsImmediately = defaultBackendSpeechDispatchCommandsImmediately;
            enableUserBargeInDuringTts = defaultEnableUserBargeInDuringTts;
            allowCommandBargeInDuringTtsWindow = defaultAllowCommandBargeInDuringTtsWindow;
            muteMicDuringTtsEvenWithAec = defaultMuteMicDuringTtsEvenWithAec;
            suppressAsrAfterTtsSeconds = defaultSuppressAsrAfterTtsSeconds;
        }

        private void ApplyDirectConversationRuntimeDefaults()
        {
            CaptureDirectConversationRuntimeDefaults();
            if (!tuneTurnTakingForDirectConversation)
            {
                RestoreDirectConversationRuntimeDefaults();
                return;
            }
            if (!UseDirectUnifiedConversationPipeline())
            {
                RestoreDirectConversationRuntimeDefaults();
                return;
            }

            deferBackendSpeechDispatch = false;
            backendSpeechQuietWindowSeconds = Mathf.Clamp(0.25f, 0.15f, 2.5f);
            backendSpeechMaxWaitSeconds = Mathf.Clamp(1.1f, 0.5f, 8f);
            backendSpeechDispatchCommandsImmediately = true;
            enableUserBargeInDuringTts = true;
            allowCommandBargeInDuringTtsWindow = true;
            muteMicDuringTtsEvenWithAec = false;
            suppressAsrAfterTtsSeconds = Mathf.Clamp(Mathf.Min(defaultSuppressAsrAfterTtsSeconds, 0.25f), 0f, 3f);
        }

        private void DisposeDirectConversationRuntime()
        {
            CancelActiveDirectConversationTurn(clearQueuedAudio: true, reason: "shutdown", stopPlayback: true);
            if (directConversationSpeakDrainCoroutine != null)
            {
                StopCoroutine(directConversationSpeakDrainCoroutine);
                directConversationSpeakDrainCoroutine = null;
            }
            pendingDirectConversationSpeakChunks.Clear();
            lastQueuedDirectConversationNormalized = string.Empty;
            activeConversationCorrId = string.Empty;
            activeConversationRoute = string.Empty;
        }

        private void PostDirectConversationAction(Action action)
        {
            if (action == null)
            {
                return;
            }

            var context = directConversationMainThreadContext;
            if (context != null && SynchronizationContext.Current != context)
            {
                context.Post(_ => action(), null);
            }
            else
            {
                action();
            }
        }

        private void DispatchVoiceTextDirectConversation(
            string text,
            RecognitionMetadata metadata,
            string corrId,
            bool bargeIn,
            string interruptedTtsText,
            string interruptedTtsCorrId)
        {
            var trimmed = string.IsNullOrWhiteSpace(text) ? string.Empty : text.Trim();
            var effectiveCorrId = string.IsNullOrWhiteSpace(corrId) ? Guid.NewGuid().ToString("N") : corrId.Trim();
            if (string.IsNullOrWhiteSpace(trimmed))
            {
                return;
            }

            CancelActiveDirectConversationTurn(clearQueuedAudio: true, reason: "superseded_by_new_turn", stopPlayback: true);
            activeConversationCorrId = effectiveCorrId;
            activeConversationRoute = string.Empty;
            directConversationTurnStreamActive = true;
            lastQueuedDirectConversationNormalized = string.Empty;
            pendingDirectConversationSpeakChunks.Clear();

            var payload = BuildDirectConversationRequestJson(
                trimmed,
                metadata,
                effectiveCorrId,
                bargeIn,
                interruptedTtsText,
                interruptedTtsCorrId);

            activeConversationTurnCts = new CancellationTokenSource();
            activeConversationTurnTask = Task.Run(
                () => RunDirectConversationTurnAsync(payload, effectiveCorrId, activeConversationTurnCts.Token));
        }

        private string BuildDirectConversationRequestJson(
            string text,
            RecognitionMetadata metadata,
            string corrId,
            bool bargeIn,
            string interruptedTtsText,
            string interruptedTtsCorrId)
        {
            var payload = new StringBuilder(256)
                .Append("{\"text\":\"").Append(EscapeJson(text)).Append('"')
                .Append(",\"corr_id\":\"").Append(EscapeJson(corrId)).Append('"')
                .Append(",\"source\":\"unity_direct\"");

            if (!float.IsNaN(metadata.AvgLogProb))
            {
                payload.Append(",\"avg_logprob\":")
                    .Append(metadata.AvgLogProb.ToString(System.Globalization.CultureInfo.InvariantCulture));
            }
            if (metadata.Rms > 0f)
            {
                payload.Append(",\"rms\":")
                    .Append(metadata.Rms.ToString(System.Globalization.CultureInfo.InvariantCulture));
            }
            if (metadata.MaxAmplitude > 0f)
            {
                payload.Append(",\"max_amplitude\":")
                    .Append(metadata.MaxAmplitude.ToString(System.Globalization.CultureInfo.InvariantCulture));
            }
            if (metadata.HasSpeakerTag)
            {
                payload.Append(",\"speaker_index\":").Append(Mathf.Max(0, metadata.SpeakerIndex));
            }
            if (metadata.SpeakerId > 0UL)
            {
                payload.Append(",\"speaker_id\":")
                    .Append(metadata.SpeakerId.ToString(System.Globalization.CultureInfo.InvariantCulture));
            }
            if (bargeIn)
            {
                payload.Append(",\"barge_in\":true");
                if (!string.IsNullOrWhiteSpace(interruptedTtsText))
                {
                    payload.Append(",\"interrupted_tts_text\":\"")
                        .Append(EscapeJson(interruptedTtsText.Trim()))
                        .Append('"');
                }
                if (!string.IsNullOrWhiteSpace(interruptedTtsCorrId))
                {
                    payload.Append(",\"interrupted_tts_corr_id\":\"")
                        .Append(EscapeJson(interruptedTtsCorrId.Trim()))
                        .Append('"');
                }
            }
            payload.Append('}');
            return payload.ToString();
        }

        private async Task RunDirectConversationTurnAsync(string payload, string corrId, CancellationToken token)
        {
            try
            {
                using (var request = new HttpRequestMessage(HttpMethod.Post, conversationTurnStreamUrl))
                {
                    request.Content = new StringContent(payload, Encoding.UTF8, "application/json");
                    using (var response = await DirectConversationHttpClient.SendAsync(
                               request,
                               HttpCompletionOption.ResponseHeadersRead,
                               token).ConfigureAwait(false))
                    {
                        if (!response.IsSuccessStatusCode)
                        {
                            var body = await response.Content.ReadAsStringAsync().ConfigureAwait(false);
                            PostDirectConversationAction(() =>
                                HandleDirectConversationError(
                                    corrId,
                                    $"HTTP {(int)response.StatusCode}: {body}"));
                            return;
                        }

                        using (var stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false))
                        using (var reader = new System.IO.StreamReader(stream, Encoding.UTF8))
                        {
                            while (!reader.EndOfStream && !token.IsCancellationRequested)
                            {
                                var line = await reader.ReadLineAsync().ConfigureAwait(false);
                                if (string.IsNullOrWhiteSpace(line))
                                {
                                    continue;
                                }
                                HandleDirectConversationEvent(line, corrId);
                            }
                        }
                    }
                }
            }
            catch (OperationCanceledException)
            {
            }
            catch (Exception ex)
            {
                PostDirectConversationAction(() => HandleDirectConversationError(corrId, ex.Message));
            }
            finally
            {
                PostDirectConversationAction(() =>
                {
                    if (string.Equals(activeConversationCorrId, corrId, StringComparison.Ordinal))
                    {
                        directConversationTurnStreamActive = false;
                    }
                });
            }
        }

        private void HandleDirectConversationEvent(string jsonLine, string expectedCorrId)
        {
            JSONNode node;
            try
            {
                node = JSONNode.Parse(jsonLine);
            }
            catch (Exception)
            {
                return;
            }

            var eventCorrId = (node?["corr_id"]?.Value ?? string.Empty).Trim();
            if (string.IsNullOrWhiteSpace(eventCorrId))
            {
                eventCorrId = expectedCorrId;
            }

            var eventType = (node?["type"]?.Value ?? string.Empty).Trim().ToLowerInvariant();
            var route = (node?["route"]?.Value ?? string.Empty).Trim().ToUpperInvariant();
            var gameName = (node?["game_name"]?.Value ?? string.Empty).Trim();
            var text = (node?["text"]?.Value ?? string.Empty).Trim();
            var provider = (node?["provider"]?.Value ?? string.Empty).Trim();
            var message = (node?["message"]?.Value ?? string.Empty).Trim();

            PostDirectConversationAction(() =>
            {
                if (!string.IsNullOrWhiteSpace(activeConversationCorrId) &&
                    !string.Equals(activeConversationCorrId, eventCorrId, StringComparison.Ordinal))
                {
                    return;
                }

                switch (eventType)
                {
                    case "route":
                        activeConversationRoute = route;
                        break;
                    case "chunk":
                        EnqueueDirectConversationSpeakChunk(eventCorrId, text);
                        break;
                    case "final":
                        FinalizeDirectConversationTurn(eventCorrId, route, text, provider);
                        break;
                    case "error":
                        HandleDirectConversationError(eventCorrId, message);
                        break;
                }
            });
        }

        private void ExecuteDirectLaunch(string gameName, string corrId)
        {
            if (publisher == null || string.IsNullOrWhiteSpace(gameName))
            {
                return;
            }

            var rawText = LookupUserTextForCorrId(corrId);
            if (piHub != null)
            {
                _ = piHub.SendFaceHappyAsync();
            }
            lastIntentTime = Time.realtimeSinceStartup;
            _ = publisher.PublishLaunchIntentAsync(gameName.Trim(), rawText);
        }

        private void ExecuteDirectExit(string corrId)
        {
            if (publisher == null)
            {
                return;
            }

            var rawText = LookupUserTextForCorrId(corrId);
            lastIntentTime = Time.realtimeSinceStartup;
            _ = publisher.PublishExitIntentAsync(rawText);
        }

        private string LookupUserTextForCorrId(string corrId)
        {
            if (string.IsNullOrWhiteSpace(corrId))
            {
                return string.Empty;
            }

            return recentCorrToUserText.TryGetValue(corrId, out var entry)
                ? (entry.text ?? string.Empty)
                : string.Empty;
        }

        private void EnqueueDirectConversationSpeakChunk(string corrId, string text)
        {
            var trimmed = string.IsNullOrWhiteSpace(text) ? string.Empty : text.Trim();
            if (string.IsNullOrWhiteSpace(trimmed))
            {
                return;
            }

            var normalized = NormalizeTranscript(trimmed);
            if (string.Equals(normalized, lastQueuedDirectConversationNormalized, StringComparison.Ordinal))
            {
                return;
            }

            pendingDirectConversationSpeakChunks.Enqueue(new DirectConversationSpeakChunk
            {
                CorrId = string.IsNullOrWhiteSpace(corrId) ? activeConversationCorrId : corrId.Trim(),
                Text = trimmed,
            });
            lastQueuedDirectConversationNormalized = normalized;
            if (directConversationSpeakDrainCoroutine == null)
            {
                directConversationSpeakDrainCoroutine = StartCoroutine(DrainDirectConversationSpeakChunks());
            }
        }

        private IEnumerator DrainDirectConversationSpeakChunks()
        {
            while (true)
            {
                if (pendingDirectConversationSpeakChunks.Count > 0 && activeTtsCoroutine == null && queuedSpeakAfterCurrent == null)
                {
                    var chunk = pendingDirectConversationSpeakChunks.Dequeue();
                    if (chunk != null &&
                        !string.IsNullOrWhiteSpace(chunk.Text) &&
                        (string.IsNullOrWhiteSpace(activeConversationCorrId) ||
                         string.Equals(activeConversationCorrId, chunk.CorrId, StringComparison.Ordinal)))
                    {
                        pendingTtsCorrId = string.IsNullOrWhiteSpace(chunk.CorrId) ? activeConversationCorrId : chunk.CorrId;
                        var speakerToUse = string.IsNullOrWhiteSpace(fixedDialogTtsSpeaker) ? null : fixedDialogTtsSpeaker.Trim();
                        if (forcePiperForDialogAnswers && IsLikelyQwenVoiceCode(speakerToUse))
                        {
                            speakerToUse = "en_US";
                        }
                        var instructToUse = string.IsNullOrWhiteSpace(fixedDialogTtsInstruct) ? null : fixedDialogTtsInstruct.Trim();
                        TriggerSpeakForTester(chunk.Text, speakerToUse, null, instructToUse);
                    }
                }

                var shouldContinue =
                    pendingDirectConversationSpeakChunks.Count > 0 ||
                    directConversationTurnStreamActive ||
                    activeTtsCoroutine != null ||
                    queuedSpeakAfterCurrent != null;
                if (!shouldContinue)
                {
                    break;
                }
                yield return null;
            }

            directConversationSpeakDrainCoroutine = null;
        }

        private void FinalizeDirectConversationTurn(string corrId, string route, string finalText, string provider)
        {
            if (!string.IsNullOrWhiteSpace(corrId) &&
                !string.IsNullOrWhiteSpace(activeConversationCorrId) &&
                !string.Equals(activeConversationCorrId, corrId, StringComparison.Ordinal))
            {
                return;
            }

            directConversationTurnStreamActive = false;
            activeConversationRoute = string.IsNullOrWhiteSpace(route) ? activeConversationRoute : route.Trim().ToUpperInvariant();
            var trimmed = string.IsNullOrWhiteSpace(finalText) ? string.Empty : finalText.Trim();
            if (!string.IsNullOrWhiteSpace(trimmed))
            {
                ConversationLog.AddEntry(ConversationRole.Coach, trimmed, "direct_unified", null, string.IsNullOrWhiteSpace(provider) ? "direct" : provider);
            }
        }

        private void HandleDirectConversationError(string corrId, string message)
        {
            if (!string.IsNullOrWhiteSpace(corrId) &&
                !string.IsNullOrWhiteSpace(activeConversationCorrId) &&
                !string.Equals(activeConversationCorrId, corrId, StringComparison.Ordinal))
            {
                return;
            }

            directConversationTurnStreamActive = false;
            if (!string.IsNullOrWhiteSpace(message))
            {
                ConversationLog.AddEntry(ConversationRole.System, message, "direct_unified", null, "direct_error");
                if (logDebugMessages)
                {
                    Debug.LogWarning($"[RobotVoice] Direct conversation error corr_id={corrId}: {message}");
                }
            }
        }

        private void CancelActiveDirectConversationTurn(bool clearQueuedAudio, string reason, bool stopPlayback = false)
        {
            if (activeConversationTurnCts != null)
            {
                try
                {
                    activeConversationTurnCts.Cancel();
                }
                catch (Exception)
                {
                }
                try
                {
                    DirectConversationHttpClient.CancelPendingRequests();
                }
                catch (Exception)
                {
                }
                activeConversationTurnCts.Dispose();
                activeConversationTurnCts = null;
            }

            activeConversationTurnTask = null;
            directConversationTurnStreamActive = false;

            if (stopPlayback)
            {
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
                if (ttsSessionActive || ttsSessionMuteApplied)
                {
                    EndTtsPlaybackSession(ttsSessionMuteApplied);
                }
            }

            if (clearQueuedAudio)
            {
                pendingDirectConversationSpeakChunks.Clear();
                lastQueuedDirectConversationNormalized = string.Empty;
            }

            if (logDebugMessages && !string.IsNullOrWhiteSpace(reason) && !string.IsNullOrWhiteSpace(activeConversationCorrId))
            {
                Debug.Log($"[RobotVoice] Direct conversation cancelled corr_id={activeConversationCorrId} reason={reason}");
            }
        }
    }
}
