using System;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RobotVoice
{
    // Subscribe to dialog_service answers and record them into ConversationLog as Coach.
    public sealed class MqttDialogAnswerSubscriber : MonoBehaviour
    {
        [Header("MQTT")]
        [SerializeField] private string host = VoiceAgentDefaults.LocalHost;
        [SerializeField] private int port = VoiceAgentDefaults.MqttPort;
        [SerializeField] private string topic = VoiceAgentDefaults.DialogAnswerTopic;
        [SerializeField] private bool autoStart = true;

        [Header("Playback (Unity TTS)")]
        [SerializeField, Tooltip("If set, plays dialog answers through Unity (recommended for AEC).")]
        private bool playAnswerInUnity = true;
        [SerializeField] private VoiceGameLauncher launcher;

        [Header("Diagnostics")]
        [SerializeField] private bool verboseLogging = true;

        private CancellationTokenSource loopCts;
        private Task loopTask;
        private SynchronizationContext mainThreadContext;
        private string mqttClientId;

        private void Awake()
        {
            mainThreadContext = SynchronizationContext.Current;
            if (launcher == null)
            {
                launcher = FindObjectOfType<VoiceGameLauncher>();
            }
            try
            {
                var unique = SystemInfo.deviceUniqueIdentifier;
                if (string.IsNullOrEmpty(unique)) unique = Environment.MachineName ?? "host";
                var shortId = unique.Length > 6 ? unique.Substring(0, 6) : unique;
                mqttClientId = $"unity-dialog-{shortId}";
            }
            catch
            {
                mqttClientId = $"unity-dialog-{Guid.NewGuid().ToString("N").Substring(0, 6)}";
            }

        }

        private void OnEnable()
        {
            if (autoStart) StartSubscriber();
        }

        private void OnDisable()
        {
            StopSubscriber();
        }

        public void StartSubscriber()
        {
            if (loopTask != null) return;
            loopCts = new CancellationTokenSource();
            loopTask = Task.Run(() => RunLoopAsync(loopCts.Token));
            if (verboseLogging) Debug.Log("[DialogAnswerSubscriber] start requested");
        }

        public void StopSubscriber()
        {
            try { loopCts?.Cancel(); } catch { }
            loopTask = null;
            loopCts?.Dispose();
            loopCts = null;
        }

        private async Task RunLoopAsync(CancellationToken token)
        {
            var loop = new MqttTcpSubscriberLoop(
                "DialogAnswerSubscriber",
                verboseLogging,
                TimeSpan.FromMilliseconds(600));

            await loop.RunAsync(
                host,
                port,
                topic,
                mqttClientId ?? "unity-dialog",
                OnMqttPayload,
                token);
        }

        private void OnMqttPayload(string messageTopic, string payload)
        {
            if (!string.Equals(messageTopic, topic, StringComparison.OrdinalIgnoreCase))
            {
                return;
            }

            if (verboseLogging)
            {
                Debug.Log($"[DialogAnswerSubscriber] msg on '{messageTopic}': {payload}");
            }

            HandleAnswerJson(payload);
        }

        private void HandleAnswerJson(string json)
        {
            if (string.IsNullOrWhiteSpace(json)) return;
            try
            {
                var node = JSONNode.Parse(json);
                var messageType = node?["type"]?.Value ?? string.Empty;
                if (!string.IsNullOrWhiteSpace(messageType) &&
                    !messageType.Equals("ANSWER", StringComparison.OrdinalIgnoreCase))
                {
                    return;
                }
                var text = node?["text"]?.Value ?? string.Empty;
                if (string.IsNullOrWhiteSpace(text)) return;
                var corrId = node?["corr_id"]?.Value ?? string.Empty;
                var ttsInstruct = node?["tts_instruct"]?.Value ?? string.Empty;
                var ttsSpeaker = node?["tts_speaker"]?.Value ?? string.Empty;

                PostToMainThread(() =>
                {
                    ConversationLog.AddEntry(ConversationRole.Coach, text, "dialog_service");
                    if (playAnswerInUnity && launcher != null)
                    {
                        // Play via Unity so RenderTap has a reference signal for AEC.
                        launcher.PlayDialogAnswerFromService(text, corrId, ttsInstruct, ttsSpeaker);
                    }
                });
            }
            catch { }
        }

        private void PostToMainThread(Action action)
        {
            if (action == null) return;
            var ctx = mainThreadContext;
            if (ctx != null && SynchronizationContext.Current != ctx)
            {
                ctx.Post(_ => action(), null);
            }
            else
            {
                action();
            }
        }
    }
}
