using System;
using System.Collections.Generic;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;
using UnityEngine.Serialization;

namespace VoiceAgent.Unity
{
    [DisallowMultipleComponent]
    public sealed class VoiceAgentComponent : MonoBehaviour
    {
        [SerializeField] private VoiceAgentSettings settings = new VoiceAgentSettings();

        [Header("TTS")]
        [SerializeField, TextArea(2, 5)] private string speakText = "Hello from VoiceAgent.";
        [SerializeField] private string voice;
        [SerializeField] private string backend = "piper";
        [SerializeField] private string ttsModel;
        [SerializeField] private float speechSpeed = 1f;
        [SerializeField] private float speechVolume = 1f;
        [SerializeField, TextArea(2, 5)] private string kokoroText = "Hello from Kokoro.";
        [SerializeField] private string kokoroVoice = "af_heart";
        [SerializeField] private string googleCloudVoice;
        [SerializeField] private string googleCloudApiKey;

        [Header("LLM / Runtime")]
        [SerializeField, TextArea(2, 5)] private string llmPrompt = "You are a helpful voice assistant.";
        [SerializeField] private string localModel = "qwen3.5:latest";

        [Header("ASR")]
        [SerializeField] private string asrMode = "manual";
        [SerializeField] private string backendAsrMode = "manual";
        [SerializeField] private bool desiredListeningEnabled = true;
        [SerializeField] private bool desiredConversationDispatchEnabled;
        [SerializeField] private bool currentListeningEnabled;
        [SerializeField] private bool currentConversationDispatchEnabled = true;
        [SerializeField] private bool transcriptStreamConnected;
        [SerializeField, TextArea(2, 4)] private string transcriptStreamError;

        [Header("Reply Mapping")]
        [FormerlySerializedAs("keywordDetectionEnabled")]
        [SerializeField] private bool replyMappingEnabled;
        [FormerlySerializedAs("keywordPhrases")]
        [SerializeField] private List<VoiceAgentReplyRule> replyRules = new List<VoiceAgentReplyRule>
        {
            new VoiceAgentReplyRule { listenFor = "test", replyWith = "I hear that." }
        };
        [FormerlySerializedAs("lastDetectedKeyword")]
        [SerializeField] private string lastMatchedListenFor;
        [FormerlySerializedAs("lastDetectedTranscript")]
        [SerializeField, TextArea(2, 4)] private string lastMatchedTranscript;
        [FormerlySerializedAs("lastKeywordDetectedAt")]
        [SerializeField] private string lastMatchedAt;
        [FormerlySerializedAs("onKeywordDetected")]
        [SerializeField] private VoiceAgentReplyMatchedEvent onReplyMatched = new VoiceAgentReplyMatchedEvent();
        [SerializeField, TextArea(2, 4)] private string lastInterceptedTranscript;
        [SerializeField] private string lastRoutingOutcome;

        [Header("Vision / Game")]
        [SerializeField, TextArea(2, 5)] private string visionPrompt = "Describe the current camera view.";
        [SerializeField] private string visionModel;
        [SerializeField] private string gameName = "demo";

        [Header("Face")]
        [SerializeField] private VoiceAgentFacePreset facePreset = VoiceAgentFacePreset.Neutral;
        [SerializeField] private string facePresetMode = "neutral";
        [SerializeField, TextArea(2, 4)] private string faceCustomValue = "^-^";
        [SerializeField] private float faceSeconds = 3f;

        [Header("LED")]
        [SerializeField] private Color ledColor = Color.cyan;
        [SerializeField, Range(0f, 1f)] private float ledBrightness = 0.8f;
        [SerializeField] private float ledPeriod = 2f;
        [SerializeField] private float ledDuration;

        [Header("Last Result")]
        [SerializeField] private bool lastSuccess;
        [SerializeField] private int lastStatusCode;
        [SerializeField, TextArea(2, 4)] private string lastMessage;
        [SerializeField, TextArea(3, 10)] private string lastRawBody;

        private readonly SemaphoreSlim stateSyncLock = new SemaphoreSlim(1, 1);
        private VoiceAgentClient client;
        private SynchronizationContext unityContext;
        private CancellationTokenSource runtimeSessionCts;
        private bool capturedConversationDispatchState;
        private bool previousConversationDispatchEnabled = true;
        private string lastProcessedStableTranscript = string.Empty;
        private int lastProcessedFinalTranscriptSequence;

        public VoiceAgentSettings Settings => settings ?? (settings = new VoiceAgentSettings());
        public VoiceAgentClient Client => client ?? (client = new VoiceAgentClient(Settings));
        public bool LastSuccess => lastSuccess;
        public int LastStatusCode => lastStatusCode;
        public string LastMessage => lastMessage;
        public string LastRawBody => lastRawBody;
        public bool DesiredListeningEnabled => desiredListeningEnabled;
        public bool DesiredConversationDispatchEnabled => desiredConversationDispatchEnabled;
        public bool CurrentListeningEnabled => currentListeningEnabled;
        public bool CurrentConversationDispatchEnabled => currentConversationDispatchEnabled;
        public bool TranscriptStreamConnected => transcriptStreamConnected;
        public string TranscriptStreamError => transcriptStreamError ?? string.Empty;
        public bool ReplyMappingEnabled => replyMappingEnabled;
        public IReadOnlyList<VoiceAgentReplyRule> ReplyRules => replyRules ?? (replyRules = new List<VoiceAgentReplyRule>());
        public string LastMatchedListenFor => lastMatchedListenFor;
        public string LastMatchedTranscript => lastMatchedTranscript;
        public string LastMatchedAt => lastMatchedAt;
        public string LastInterceptedTranscript => lastInterceptedTranscript;
        public string LastRoutingOutcome => lastRoutingOutcome;
        public string GoogleCloudApiKey => googleCloudApiKey ?? string.Empty;
        public VoiceAgentReplyMatchedEvent OnReplyMatched => onReplyMatched ?? (onReplyMatched = new VoiceAgentReplyMatchedEvent());

        private void OnEnable()
        {
            if (!Application.isPlaying)
            {
                return;
            }

            unityContext = SynchronizationContext.Current;
            StartRuntimeSession();
        }

        private void OnDisable()
        {
            StopRuntimeSession();
        }

        private void OnDestroy()
        {
            StopRuntimeSession();
            DisposeClient();
        }

        private void OnValidate()
        {
            if (replyRules == null)
            {
                replyRules = new List<VoiceAgentReplyRule>();
            }

            if (onReplyMatched == null)
            {
                onReplyMatched = new VoiceAgentReplyMatchedEvent();
            }

            transcriptStreamError = transcriptStreamError ?? string.Empty;
            lastInterceptedTranscript = lastInterceptedTranscript ?? string.Empty;
            lastRoutingOutcome = lastRoutingOutcome ?? string.Empty;
            facePresetMode = string.IsNullOrWhiteSpace(facePresetMode) ? ResolveFacePresetMode() : facePresetMode.Trim();
        }

        public void RecreateClient()
        {
            DisposeClient();
            client = new VoiceAgentClient(Settings);
        }

        public void DisposeClient()
        {
            client?.Dispose();
            client = null;
        }

        public void ClearLastResult()
        {
            lastSuccess = false;
            lastStatusCode = 0;
            lastMessage = string.Empty;
            lastRawBody = string.Empty;
        }

        public VoiceAgentSpeechRequest CreateSpeechRequest()
        {
            var selectedVoice = voice;
            if (IsBackend(backend, "kokoro"))
            {
                selectedVoice = kokoroVoice;
            }
            else if (IsBackend(backend, "google-cloud"))
            {
                selectedVoice = googleCloudVoice;
            }

            return new VoiceAgentSpeechRequest
            {
                text = speakText,
                voice = selectedVoice,
                backend = backend,
                model = ttsModel,
                googleCloudApiKey = IsBackend(backend, "google-cloud") ? googleCloudApiKey : string.Empty,
                speed = speechSpeed,
                volume = speechVolume,
            };
        }

        public Task<VoiceAgentConnectionHealth> CheckConnectionAsync() => RunConnectionAsync(current => current.CheckConnectionHealthAsync());
        public Task<VoiceAgentApiResult> GetLogsAsync() => RunAsync(current => current.GetLogsAsync());
        public Task<VoiceAgentApiResult> GetTtsOptionsAsync() => RunAsync(current => current.GetTtsOptionsAsync(googleCloudApiKey));
        public Task<VoiceAgentApiResult> GetKokoroOptionsAsync() => RunAsync(current => current.GetKokoroOptionsAsync());
        public Task<VoiceAgentApiResult> SpeakAsync() => RunAsync(current => current.SpeakAsync(CreateSpeechRequest()));
        public Task<VoiceAgentApiResult> SetVoiceAsync() => RunAsync(current => current.SetVoiceAsync(voice));
        public Task<VoiceAgentApiResult> SetTtsModelAsync() => RunAsync(current => current.SetTtsModelAsync(ttsModel));
        public Task<VoiceAgentApiResult> SetTtsBackendAsync() => RunAsync(current => current.SetTtsBackendAsync(backend));
        public Task<VoiceAgentApiResult> SetKokoroVoiceAsync() => RunAsync(current => current.SetKokoroVoiceAsync(kokoroVoice));
        public Task<VoiceAgentApiResult> SetGoogleCloudVoiceAsync() => RunAsync(current => current.SetGoogleCloudVoiceAsync(googleCloudVoice));
        public Task<VoiceAgentApiResult> KokoroSpeakAsync() => RunAsync(current => current.KokoroSpeakAsync(kokoroText, kokoroVoice));

        private static bool IsBackend(string value, string expected)
        {
            return string.Equals((value ?? string.Empty).Trim(), expected, StringComparison.OrdinalIgnoreCase);
        }
        public Task<VoiceAgentApiResult> GetLlmPromptAsync() => RunAsync(current => current.GetLlmPromptAsync());
        public Task<VoiceAgentApiResult> SetLlmPromptAsync() => RunAsync(current => current.SetLlmPromptAsync(llmPrompt));
        public Task<VoiceAgentApiResult> ResetLlmPromptAsync() => RunAsync(current => current.ResetLlmPromptAsync());
        public Task<VoiceAgentApiResult> GetRuntimeConfigAsync() => RunAsync(current => current.GetRuntimeConfigAsync());
        public Task<VoiceAgentApiResult> SetLocalModelAsync() => RunAsync(current => current.SetLocalModelAsync(localModel));
        public Task<VoiceAgentApiResult> GetAsrStatusAsync() => RunAsync(current => current.GetAsrStatusAsync());
        public Task<VoiceAgentApiResult> SetAsrModeAsync() => RunAsync(current => current.SetAsrModeAsync(asrMode));
        public Task<VoiceAgentApiResult> SetBackendAsrModeAsync() => RunAsync(current => current.SetBackendAsrModeAsync(backendAsrMode));
        public Task<VoiceAgentApiResult> StartListeningAsync() => RunAsync(current => current.StartListeningAsync());
        public Task<VoiceAgentApiResult> PauseListeningAsync() => RunAsync(current => current.PauseListeningAsync());
        public Task<VoiceAgentApiResult> DescribeCurrentCameraAsync() => RunAsync(current => current.DescribeCurrentCameraAsync(visionPrompt, visionModel));
        public Task<VoiceAgentApiResult> LaunchGameAsync() => RunAsync(current => current.LaunchGameAsync(gameName));
        public Task<VoiceAgentApiResult> ExitGameAsync() => RunAsync(current => current.ExitGameAsync());
        public Task<VoiceAgentApiResult> FacePresetAsync() => RunAsync(current => current.FacePresetAsync(ResolveFacePresetMode(), faceSeconds));
        public Task<VoiceAgentApiResult> FaceCustomAsync() => RunAsync(current => current.FaceCustomAsync(faceCustomValue, faceSeconds));
        public Task<VoiceAgentApiResult> LedBreatheAsync() => RunAsync(current => current.LedBreatheAsync(ledColor, ledBrightness, ledPeriod, ledDuration));
        public Task<VoiceAgentApiResult> LedSolidAsync() => RunAsync(current => current.LedSolidAsync(ledColor, ledBrightness, ledDuration));
        public Task<VoiceAgentApiResult> LedRandomAsync() => RunAsync(current => current.LedRandomAsync(ledDuration));
        public Task<VoiceAgentApiResult> LedOffAsync() => RunAsync(current => current.LedOffAsync());
        public Task<VoiceAgentApiResult> FlowerOpenAsync() => RunAsync(current => current.FlowerOpenAsync());
        public Task<VoiceAgentApiResult> FlowerCloseAsync() => RunAsync(current => current.FlowerCloseAsync());
        public Task<VoiceAgentApiResult> FlowerStopAsync() => RunAsync(current => current.FlowerStopAsync());
        public Task<VoiceAgentApiResult> FlowerOpenSlowAsync() => RunAsync(current => current.FlowerOpenSlowAsync());
        public Task<VoiceAgentApiResult> FlowerCloseSlowAsync() => RunAsync(current => current.FlowerCloseSlowAsync());

        public async Task<VoiceAgentAsrStatus> RefreshAsrStatusAsync()
        {
            await stateSyncLock.WaitAsync();
            try
            {
                var result = await Client.GetAsrStatusTypedAsync();
                ApplyResult(result.ApiResult, false);
                ApplyAsrStatus(result.Payload);
                return result.Payload;
            }
            finally
            {
                stateSyncLock.Release();
            }
        }

        public Task<VoiceAgentAsrStatus> SetListeningEnabledAsync(bool listeningEnabled)
        {
            return SetListeningEnabledInternalAsync(listeningEnabled, true);
        }

        public Task<VoiceAgentAsrStatus> ApplyListeningStateAsync()
        {
            return SetListeningEnabledInternalAsync(desiredListeningEnabled, true);
        }

        public Task<VoiceAgentAsrStatus> SetConversationDispatchEnabledAsync(bool enabled)
        {
            return SetConversationDispatchEnabledInternalAsync(enabled, true);
        }

        public Task<VoiceAgentAsrStatus> ApplyConversationDispatchStateAsync()
        {
            return SetConversationDispatchEnabledInternalAsync(desiredConversationDispatchEnabled, true);
        }

        public async Task ApplyReplyMappingStateAsync()
        {
            await stateSyncLock.WaitAsync();
            try
            {
                lastRoutingOutcome = replyMappingEnabled ? "streaming" : string.Empty;
                ApplyResult(
                    VoiceAgentApiResult.Ok(
                        replyMappingEnabled
                            ? "reply mapping is using runtime transcript events"
                            : "reply mapping disabled"),
                    false);
            }
            finally
            {
                stateSyncLock.Release();
            }
        }

        private void StartRuntimeSession()
        {
            StopRuntimeSession();
            transcriptStreamConnected = false;
            transcriptStreamError = string.Empty;
            runtimeSessionCts = new CancellationTokenSource();
            _ = RunRuntimeSessionAsync(runtimeSessionCts.Token);
        }

        private void StopRuntimeSession()
        {
            var cts = runtimeSessionCts;
            runtimeSessionCts = null;
            if (cts != null)
            {
                cts.Cancel();
                cts.Dispose();
            }

            transcriptStreamConnected = false;
            transcriptStreamError = string.Empty;
            lastProcessedStableTranscript = string.Empty;
            lastProcessedFinalTranscriptSequence = 0;

            if (!Application.isPlaying || !capturedConversationDispatchState)
            {
                return;
            }

            var previousDispatch = previousConversationDispatchEnabled;
            capturedConversationDispatchState = false;
            try
            {
                using (var restoreClient = new VoiceAgentClient(Settings))
                {
                    var restoreResult = restoreClient
                        .SetConversationDispatchEnabledAsync(previousDispatch)
                        .GetAwaiter()
                        .GetResult();
                    if (VoiceAgentClient.TryParseJson(restoreResult != null ? restoreResult.RawBody : null, out VoiceAgentAsrStatus restoreStatus))
                    {
                        ApplyAsrStatus(restoreStatus);
                    }
                    else
                    {
                        currentConversationDispatchEnabled = previousDispatch;
                    }
                }
            }
            catch
            {
                currentConversationDispatchEnabled = previousDispatch;
            }
        }

        private async Task RunRuntimeSessionAsync(CancellationToken cancellationToken)
        {
            try
            {
                var statusResult = await Client.GetAsrStatusTypedAsync(cancellationToken);
                PostToMainThread(() =>
                {
                    ApplyResult(statusResult.ApiResult, false);
                    ApplyAsrStatus(statusResult.Payload);
                    var initialStable = statusResult.Payload != null ? statusResult.Payload.StablePartial : string.Empty;
                    lastProcessedStableTranscript = NormalizeKeywordText(initialStable);
                    lastProcessedFinalTranscriptSequence = statusResult.Payload != null ? statusResult.Payload.FinalTranscriptSequence : 0;
                });
                if (cancellationToken.IsCancellationRequested)
                {
                    return;
                }

                if (statusResult.Payload != null)
                {
                    previousConversationDispatchEnabled = statusResult.Payload.ConversationDispatchEnabled;
                    capturedConversationDispatchState = true;
                }
                else
                {
                    previousConversationDispatchEnabled = true;
                    capturedConversationDispatchState = false;
                }

                var dispatchResult = await Client.SetConversationDispatchEnabledAsync(desiredConversationDispatchEnabled, cancellationToken);
                var dispatchStatus = await ParseRuntimeAsrStatusAsync(dispatchResult);
                if (cancellationToken.IsCancellationRequested)
                {
                    return;
                }
                PostToMainThread(() =>
                {
                    ApplyResult(dispatchResult, false);
                    ApplyAsrStatus(dispatchStatus);
                });
                if (cancellationToken.IsCancellationRequested)
                {
                    return;
                }

                if (dispatchStatus != null && dispatchStatus.Listening != desiredListeningEnabled)
                {
                    var listeningResult = await Client.SetListeningEnabledAsync(desiredListeningEnabled, cancellationToken);
                    var listeningStatus = await ParseRuntimeAsrStatusAsync(listeningResult);
                    if (cancellationToken.IsCancellationRequested)
                    {
                        return;
                    }

                    PostToMainThread(() =>
                    {
                        ApplyResult(listeningResult, false);
                        ApplyAsrStatus(listeningStatus);
                    });
                }

                await RunTranscriptStreamLoopAsync(cancellationToken);
            }
            catch (OperationCanceledException)
            {
            }
            catch (Exception ex)
            {
                PostToMainThread(() =>
                {
                    transcriptStreamConnected = false;
                    transcriptStreamError = ex.Message;
                    ApplyResult(VoiceAgentApiResult.Fail(ex.Message), false);
                });
            }
        }

        private async Task RunTranscriptStreamLoopAsync(CancellationToken cancellationToken)
        {
            while (!cancellationToken.IsCancellationRequested)
            {
                try
                {
                    await Client.StreamAsrEventsAsync(
                        onConnected: () => PostToMainThread(() =>
                        {
                            transcriptStreamConnected = true;
                            transcriptStreamError = string.Empty;
                        }),
                        onEvent: status => PostToMainThread(() => HandleAsrEvent(status)),
                        onError: error => PostToMainThread(() =>
                        {
                            transcriptStreamConnected = false;
                            transcriptStreamError = error ?? string.Empty;
                        }),
                        cancellationToken: cancellationToken);

                    if (cancellationToken.IsCancellationRequested)
                    {
                        break;
                    }

                    PostToMainThread(() =>
                    {
                        transcriptStreamConnected = false;
                        if (string.IsNullOrWhiteSpace(transcriptStreamError))
                        {
                            transcriptStreamError = "transcript stream disconnected";
                        }
                    });
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    PostToMainThread(() =>
                    {
                        transcriptStreamConnected = false;
                        transcriptStreamError = ex.Message;
                    });
                }

                try
                {
                    await Task.Delay(1000, cancellationToken);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }
        }

        private async Task<VoiceAgentAsrStatus> SetListeningEnabledInternalAsync(bool listeningEnabled, bool logToConsole)
        {
            await stateSyncLock.WaitAsync();
            try
            {
                desiredListeningEnabled = listeningEnabled;
                var result = await Client.SetListeningEnabledAsync(listeningEnabled);
                ApplyResult(result, logToConsole);
                var status = await ParseRuntimeAsrStatusAsync(result);
                ApplyAsrStatus(status);
                if (!listeningEnabled)
                {
                    lastProcessedStableTranscript = string.Empty;
                    lastInterceptedTranscript = string.Empty;
                    lastRoutingOutcome = string.Empty;
                }

                return status;
            }
            finally
            {
                stateSyncLock.Release();
            }
        }

        private async Task<VoiceAgentAsrStatus> SetConversationDispatchEnabledInternalAsync(bool enabled, bool logToConsole)
        {
            await stateSyncLock.WaitAsync();
            try
            {
                desiredConversationDispatchEnabled = enabled;
                var result = await Client.SetConversationDispatchEnabledAsync(enabled);
                ApplyResult(result, logToConsole);
                var status = await ParseRuntimeAsrStatusAsync(result);
                ApplyAsrStatus(status);
                return status;
            }
            finally
            {
                stateSyncLock.Release();
            }
        }

        private async Task<VoiceAgentAsrStatus> ParseRuntimeAsrStatusAsync(VoiceAgentApiResult result)
        {
            if (VoiceAgentClient.TryParseJson(result != null ? result.RawBody : null, out VoiceAgentAsrStatus payload))
            {
                return payload;
            }

            if (result != null && result.Success)
            {
                var refresh = await Client.GetAsrStatusTypedAsync();
                ApplyResult(refresh.ApiResult, false);
                return refresh.Payload;
            }

            return null;
        }

        private void HandleAsrEvent(VoiceAgentAsrStatus status)
        {
            ApplyAsrStatus(status);
            if (status == null)
            {
                return;
            }

            var transcript = status.StablePartial;
            var normalizedTranscript = NormalizeKeywordText(transcript);
            if (string.Equals(status.EventType, "snapshot", StringComparison.OrdinalIgnoreCase))
            {
                lastProcessedStableTranscript = normalizedTranscript;
                lastProcessedFinalTranscriptSequence = status.FinalTranscriptSequence;
                lastInterceptedTranscript = !string.IsNullOrWhiteSpace(status.FinalTranscript)
                    ? status.FinalTranscript
                    : (transcript ?? string.Empty);
                lastRoutingOutcome = "snapshot";
                return;
            }

            if (status.FinalTranscriptSequence > lastProcessedFinalTranscriptSequence && !string.IsNullOrWhiteSpace(status.FinalTranscript))
            {
                lastProcessedFinalTranscriptSequence = status.FinalTranscriptSequence;
                lastInterceptedTranscript = status.FinalTranscript;
                TryHandleReplyTranscript(status.FinalTranscript);
                return;
            }

            if (string.IsNullOrWhiteSpace(normalizedTranscript))
            {
                lastProcessedStableTranscript = string.Empty;
                lastInterceptedTranscript = !string.IsNullOrWhiteSpace(status.FinalTranscript)
                    ? status.FinalTranscript
                    : (transcript ?? string.Empty);
                lastRoutingOutcome = string.Empty;
                return;
            }

            if (string.Equals(lastProcessedStableTranscript, normalizedTranscript, StringComparison.Ordinal))
            {
                return;
            }

            lastProcessedStableTranscript = normalizedTranscript;
            lastInterceptedTranscript = transcript ?? string.Empty;
            TryHandleReplyTranscript(transcript);
        }

        private void TryHandleReplyTranscript(string transcript)
        {
            if (!replyMappingEnabled || !desiredListeningEnabled || !HasConfiguredReplyRules())
            {
                lastRoutingOutcome = "ignored";
                return;
            }

            if (currentConversationDispatchEnabled)
            {
                lastRoutingOutcome = "auto-conversation-enabled";
                return;
            }

            if (!TryMatchReplyRule(transcript, out var matchedListenFor, out var replyWith))
            {
                lastRoutingOutcome = "ignored";
                return;
            }

            RecordReplyMatch(matchedListenFor, transcript);
            lastRoutingOutcome = "matched";
            _ = SpeakMappedReplyAsync(replyWith);
        }

        private void ApplyAsrStatus(VoiceAgentAsrStatus status)
        {
            if (status == null)
            {
                return;
            }

            currentListeningEnabled = status.Listening;
            currentConversationDispatchEnabled = status.ConversationDispatchEnabled;
        }

        private bool TryMatchReplyRule(string transcript, out string matchedListenFor, out string replyWith)
        {
            matchedListenFor = string.Empty;
            replyWith = string.Empty;
            if (string.IsNullOrWhiteSpace(transcript) || replyRules == null)
            {
                return false;
            }

            var normalizedTranscript = NormalizeKeywordText(transcript);
            if (string.IsNullOrWhiteSpace(normalizedTranscript))
            {
                return false;
            }

            for (var index = 0; index < replyRules.Count; index++)
            {
                var rule = replyRules[index];
                if (rule == null)
                {
                    continue;
                }

                var normalizedListenFor = NormalizeKeywordText(rule.listenFor);
                var normalizedReply = rule.replyWith != null ? rule.replyWith.Trim() : string.Empty;
                if (string.IsNullOrWhiteSpace(normalizedListenFor) || string.IsNullOrWhiteSpace(normalizedReply))
                {
                    continue;
                }

                if (!string.Equals(normalizedTranscript, normalizedListenFor, StringComparison.Ordinal))
                {
                    continue;
                }

                matchedListenFor = rule.listenFor != null ? rule.listenFor.Trim() : string.Empty;
                replyWith = normalizedReply;
                return true;
            }

            return false;
        }

        private void RecordReplyMatch(string matchedListenFor, string transcript)
        {
            lastMatchedListenFor = matchedListenFor ?? string.Empty;
            lastMatchedTranscript = transcript ?? string.Empty;
            lastMatchedAt = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss");
            OnReplyMatched.Invoke(lastMatchedListenFor);
        }

        private async Task SpeakMappedReplyAsync(string replyText)
        {
            var request = CreateSpeechRequest();
            request.text = replyText ?? string.Empty;
            var result = await Client.SpeakAsync(request);
            PostToMainThread(() => ApplyResult(result, false));
        }

        private bool HasConfiguredReplyRules()
        {
            if (replyRules == null || replyRules.Count == 0)
            {
                return false;
            }

            for (var index = 0; index < replyRules.Count; index++)
            {
                var rule = replyRules[index];
                if (rule == null)
                {
                    continue;
                }

                if (!string.IsNullOrWhiteSpace(rule.listenFor) && !string.IsNullOrWhiteSpace(rule.replyWith))
                {
                    return true;
                }
            }

            return false;
        }

        private string ResolveFacePresetMode()
        {
            if (!string.IsNullOrWhiteSpace(facePresetMode))
            {
                return facePresetMode.Trim();
            }

            switch (facePreset)
            {
                case VoiceAgentFacePreset.Happy: return "happy";
                case VoiceAgentFacePreset.Sad: return "sad";
                case VoiceAgentFacePreset.VerySad: return "verySad";
                case VoiceAgentFacePreset.Excited: return "excited";
                default: return "neutral";
            }
        }

        private void PostToMainThread(Action action)
        {
            if (action == null)
            {
                return;
            }

            var context = unityContext;
            if (context != null && SynchronizationContext.Current != context)
            {
                context.Post(_ => action(), null);
                return;
            }

            action();
        }

        private static string NormalizeKeywordText(string text)
        {
            if (string.IsNullOrWhiteSpace(text))
            {
                return string.Empty;
            }

            var builder = new StringBuilder(text.Length);
            var lastWasSpace = true;
            for (var index = 0; index < text.Length; index++)
            {
                var character = char.ToLowerInvariant(text[index]);
                if (char.IsLetterOrDigit(character))
                {
                    builder.Append(character);
                    lastWasSpace = false;
                    continue;
                }

                if (!lastWasSpace)
                {
                    builder.Append(' ');
                    lastWasSpace = true;
                }
            }

            return builder.ToString().Trim();
        }

        private async Task<VoiceAgentApiResult> RunAsync(Func<VoiceAgentClient, Task<VoiceAgentApiResult>> action)
        {
            var result = await action(Client);
            ApplyResult(result);
            return result;
        }

        private async Task<VoiceAgentConnectionHealth> RunConnectionAsync(Func<VoiceAgentClient, Task<VoiceAgentConnectionHealth>> action)
        {
            var result = await action(Client);
            lastSuccess = result != null && result.IsReachable;
            lastStatusCode = result != null && result.IsReachable ? 200 : 0;
            lastMessage = result != null ? result.Summary ?? string.Empty : string.Empty;
            lastRawBody = string.Empty;
            Debug.Log(lastMessage, this);
            return result;
        }

        private void ApplyResult(VoiceAgentApiResult result)
        {
            ApplyResult(result, true);
        }

        private void ApplyResult(VoiceAgentApiResult result, bool logToConsole)
        {
            lastSuccess = result != null && result.Success;
            lastStatusCode = result != null ? result.StatusCode : 0;
            lastMessage = result != null ? result.Message ?? string.Empty : string.Empty;
            lastRawBody = result != null ? result.RawBody ?? string.Empty : string.Empty;

            if (!logToConsole)
            {
                return;
            }

            if (result == null)
            {
                Debug.LogWarning("VoiceAgent returned no result.", this);
                return;
            }

            if (result.Success)
            {
                Debug.Log($"{result.StatusCode}: {result.Message}", this);
            }
            else
            {
                Debug.LogWarning($"{result.StatusCode}: {result.Message}", this);
            }
        }
    }
}
