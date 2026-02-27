using System;
using System.Threading;
using System.Threading.Tasks;
using UnityEngine;

namespace RobotVoice
{
    // Subscribe to caption sentences and forward them into VoiceGameLauncher.
    public sealed class MqttCaptionSubscriber : MonoBehaviour
    {
        [Header("MQTT")]
        [SerializeField] private string host = VoiceAgentDefaults.LocalHost;
        [SerializeField] private int port = VoiceAgentDefaults.MqttPort;
        // IMPORTANT: Do NOT subscribe to robot/voice/text here, because VoiceGameLauncher publishes to that topic.
        // Subscribing to the same topic and forwarding back into VoiceGameLauncher creates an infinite loop.
        [SerializeField] private string topic = VoiceAgentDefaults.CaptionTopic;
        [SerializeField] private bool autoStart = true;
        [SerializeField, Tooltip("ClientId suffix to distinguish multiple Unity instances")]
        private string clientIdSuffix = "unity-caption";

        [Header("Routing")]
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

            // Build a stable client id on the main thread to avoid Unity API access off-thread
            try
            {
                var unique = SystemInfo.deviceUniqueIdentifier;
                if (string.IsNullOrEmpty(unique)) unique = Environment.MachineName ?? "host";
                var shortId = unique.Length > 6 ? unique.Substring(0, 6) : unique;
                mqttClientId = $"{clientIdSuffix}-{shortId}";
            }
            catch
            {
                mqttClientId = $"{clientIdSuffix}-{Guid.NewGuid().ToString("N").Substring(0, 6)}";
            }

        }

        private void OnEnable()
        {
            if (autoStart)
            {
                StartSubscriber();
            }
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
            if (verboseLogging) Debug.Log("[CaptionSubscriber] start requested");
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
                "CaptionSubscriber",
                verboseLogging,
                TimeSpan.FromMilliseconds(500));

            await loop.RunAsync(
                host,
                port,
                topic,
                mqttClientId ?? $"{clientIdSuffix}-client",
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
                Debug.Log($"[CaptionSubscriber] msg on '{messageTopic}': {payload}");
            }

            HandleIncomingJson(payload);
        }

        private void HandleIncomingJson(string json)
        {
            if (string.IsNullOrWhiteSpace(json)) return;
            try
            {
                var node = JSONNode.Parse(json);
                // Prevent feedback loop: ignore Unity-originated publishes.
                var source = node?["source"]?.Value ?? string.Empty;
                if (!string.IsNullOrWhiteSpace(source) &&
                    source.StartsWith("unity", StringComparison.OrdinalIgnoreCase))
                {
                    return;
                }
                var sourceLabel = node?["sourceLabel"]?.Value ?? string.Empty;
                if (!string.IsNullOrWhiteSpace(sourceLabel) &&
                    sourceLabel.StartsWith("unity", StringComparison.OrdinalIgnoreCase))
                {
                    return;
                }

                var text = node?["text"]?.Value ?? string.Empty;
                if (string.IsNullOrWhiteSpace(text)) return;

                if (launcher == null) return;

                PostToMainThread(() =>
                {
                    try
                    {
                        if (verboseLogging) Debug.Log($"[CaptionSubscriber] forwarding transcript: {text}");
                        launcher.HandleSpeechResult(text);
                    }
                    catch { }
                });
            }
            catch
            {
                // ignore parse errors
            }
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
