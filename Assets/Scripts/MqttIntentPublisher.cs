#if ROBOTVOICE_USE_MQTT
using System;
using System.Collections.Generic;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using RobotVoice.Mqtt;
using UnityEngine;

namespace RobotVoice
{
    public class MqttIntentPublisher : MonoBehaviour
    {
        [Header("MQTT Broker")]
        [SerializeField] private string host = "127.0.0.1";
        [SerializeField] private int port = 1883;
        [SerializeField] private string username = string.Empty;
        [SerializeField] private string password = string.Empty;
        [SerializeField] private string intentTopic = "robot/intent";
        [SerializeField] private string clientId = "unity-voice";
        [Header("MQTT TLS")]
        [SerializeField] private bool useTls;
        [SerializeField] private string tlsTargetHost = string.Empty;
        [SerializeField] private bool allowUntrustedCertificates;
        [Header("MQTT Options")]
        [SerializeField] private bool autoConnectOnStart = true;
        [SerializeField] private string sourceLabel = "unity_vosk";
        [Header("Flood Protection")]
        [SerializeField, Tooltip("Minimum seconds between publishing identical payloads to the intent topic"), Min(0f)]
        private float duplicatePublishCooldownSeconds = 0.5f;

        private SimpleMqttClient client;
        private SimpleMqttClientOptions options;
        private readonly SemaphoreSlim connectLock = new SemaphoreSlim(1, 1);
        private CancellationTokenSource reconnectCts;
        private bool isDisposing;
        private SynchronizationContext mainThreadContext;
        private readonly object duplicatePublishLock = new object();
        private readonly HashSet<string> pendingPayloads = new HashSet<string>();
        private string lastPublishedPayload = string.Empty;
        private float lastPublishRealtime;

        private void Awake()
        {
            mainThreadContext = SynchronizationContext.Current;
            InitialiseClient();
        }

        private async void Start()
        {
            if (autoConnectOnStart)
            {
                await EnsureConnectedAsync();
            }
        }

        private void InitialiseClient()
        {
            client = new SimpleMqttClient();
            client.Connected += () =>
            {
                PostToMainThread(() => Debug.Log($"[RobotVoice] Connected to MQTT {host}:{port}"));
            };
            client.Disconnected += reason =>
            {
                PostToMainThread(() =>
                {
                    Debug.LogWarning($"[RobotVoice] Disconnected from MQTT: {reason}");
                    if (!isDisposing)
                    {
                        ScheduleReconnect();
                    }
                });
            };
        }

        public async Task EnsureConnectedAsync()
        {
            if (client == null)
            {
                InitialiseClient();
            }

            await connectLock.WaitAsync();
            try
            {
                if (client.IsConnected)
                {
                    return;
                }

                var builder = new SimpleMqttClientOptionsBuilder()
                    .WithTcpServer(host, port)
                    .WithClientId(string.IsNullOrWhiteSpace(clientId) ? Guid.NewGuid().ToString("N") : clientId)
                    .WithKeepAlivePeriod(TimeSpan.FromSeconds(15))
                    .WithCleanSession();

                if (!string.IsNullOrEmpty(username))
                {
                    builder = builder.WithCredentials(username, password ?? string.Empty);
                }

                if (useTls)
                {
                    builder = builder.WithTls(tls =>
                    {
                        if (!string.IsNullOrWhiteSpace(tlsTargetHost))
                        {
                            tls.WithTargetHost(tlsTargetHost.Trim());
                        }

                        if (allowUntrustedCertificates)
                        {
                            tls.AllowUntrustedCertificates();
                        }
                    });
                }

                options = builder.Build();
                await client.ConnectAsync(options, CancellationToken.None);
            }
            catch (Exception ex)
            {
                Debug.LogError($"[RobotVoice] MQTT connect failed: {ex.Message}");
            }
            finally
            {
                connectLock.Release();
            }
        }

        public async Task PublishLaunchIntentAsync(string gameName, string rawText)
        {
            if (string.IsNullOrWhiteSpace(gameName))
            {
                Debug.LogWarning("[RobotVoice] Ignoring empty game name");
                return;
            }

            var payload = BuildLaunchPayload(gameName.Trim(), rawText);
            await PublishAsync(payload);
        }

        public async Task PublishExitIntentAsync(string rawText)
        {
            var payload = BuildExitPayload(rawText);
            await PublishAsync(payload);
        }

        // Raw publish helper for custom topics (e.g., robot/pi/*)
        public async Task PublishRawAsync(string topic, string payload, SimpleMqttQualityOfServiceLevel qos = SimpleMqttQualityOfServiceLevel.AtLeastOnce)
        {
            if (string.IsNullOrWhiteSpace(topic))
            {
                Debug.LogWarning("[RobotVoice] Raw publish ignored: empty topic");
                return;
            }

            try
            {
                await EnsureConnectedAsync();
                if (client == null || !client.IsConnected)
                {
                    Debug.LogWarning("[RobotVoice] MQTT client not connected; raw publish ignored");
                    return;
                }

                var message = new SimpleMqttApplicationMessageBuilder()
                    .WithTopic(topic)
                    .WithPayload(payload)
                    .WithQualityOfServiceLevel(qos)
                    .Build();

                await client.PublishAsync(message, CancellationToken.None);
                Debug.Log($"[RobotVoice] Raw published: {topic} {payload}");
            }
            catch (Exception ex)
            {
                Debug.LogError($"[RobotVoice] Raw publish failed: {ex.Message}");
            }
        }

        private async Task PublishAsync(string payload)
        {
            if (!TryReservePayload(payload))
            {
                return;
            }

            try
            {
                var message = new SimpleMqttApplicationMessageBuilder()
                    .WithTopic(intentTopic)
                    .WithPayload(payload)
                    .WithQualityOfServiceLevel(SimpleMqttQualityOfServiceLevel.AtLeastOnce)
                    .Build();

                var attemptsRemaining = 2;
                while (attemptsRemaining-- > 0)
                {
                    try
                    {
                        await EnsureConnectedAsync();
                        if (client == null || !client.IsConnected)
                        {
                            if (attemptsRemaining > 0)
                            {
                                await Task.Delay(TimeSpan.FromSeconds(1));
                                continue;
                            }

                            Debug.LogWarning("[RobotVoice] MQTT client not connected; intent not sent");
                            return;
                        }

                        await client.PublishAsync(message, CancellationToken.None);
                        MarkPayloadAsPublished(payload);
                        Debug.Log($"[RobotVoice] Intent published: {payload}");
                        return;
                    }
                    catch (Exception ex)
                    {
                        if (attemptsRemaining > 0)
                        {
                            Debug.LogWarning($"[RobotVoice] MQTT publish failed, retrying: {ex.Message}");
                            await Task.Delay(TimeSpan.FromSeconds(1));
                            continue;
                        }

                        Debug.LogError($"[RobotVoice] Failed to publish MQTT intent: {ex.Message}");
                    }
                }
            }
            finally
            {
                ReleasePayloadReservation(payload);
            }
        }

        private bool TryReservePayload(string payload)
        {
            if (string.IsNullOrEmpty(payload))
            {
                return true;
            }

            lock (duplicatePublishLock)
            {
                if (pendingPayloads.Contains(payload))
                {
                    return false;
                }

                if (duplicatePublishCooldownSeconds > 0f && string.Equals(lastPublishedPayload, payload, StringComparison.Ordinal))
                {
                    var elapsed = Time.realtimeSinceStartup - lastPublishRealtime;
                    if (elapsed < Mathf.Max(0.01f, duplicatePublishCooldownSeconds))
                    {
                        return false;
                    }
                }

                pendingPayloads.Add(payload);
                return true;
            }
        }

        private void MarkPayloadAsPublished(string payload)
        {
            if (string.IsNullOrEmpty(payload))
            {
                return;
            }

            lock (duplicatePublishLock)
            {
                lastPublishedPayload = payload;
                lastPublishRealtime = Time.realtimeSinceStartup;
            }
        }

        private void ReleasePayloadReservation(string payload)
        {
            if (string.IsNullOrEmpty(payload))
            {
                return;
            }

            lock (duplicatePublishLock)
            {
                pendingPayloads.Remove(payload);
            }
        }

        // (legacy IsSuppressed/RegisterPublishKey removed; duplicate protection handled by TryReservePayload/MarkPayloadAsPublished)

        private string BuildLaunchPayload(string gameName, string rawText)
        {
            var sb = new StringBuilder();
            sb.Append("{\"type\":\"LAUNCH_GAME\"");
            sb.Append(",\"game_name\":\"").Append(EscapeJson(gameName)).Append("\"");
            sb.Append(",\"source\":\"").Append(EscapeJson(sourceLabel)).Append("\"");
            if (!string.IsNullOrWhiteSpace(rawText))
            {
                sb.Append(",\"raw_text\":\"").Append(EscapeJson(rawText.Trim())).Append("\"");
            }
            sb.Append("}");
            return sb.ToString();
        }

        private string BuildExitPayload(string rawText)
        {
            var sb = new StringBuilder();
            sb.Append("{\"type\":\"BACK_HOME\"");
            sb.Append(",\"source\":\"").Append(EscapeJson(sourceLabel)).Append("\"");
            if (!string.IsNullOrWhiteSpace(rawText))
            {
                sb.Append(",\"raw_text\":\"").Append(EscapeJson(rawText.Trim())).Append("\"");
            }
            sb.Append("}");
            return sb.ToString();
        }

        private static string EscapeJson(string value)
        {
            if (string.IsNullOrEmpty(value))
            {
                return string.Empty;
            }

            var sb = new StringBuilder();
            foreach (var ch in value)
            {
                switch (ch)
                {
                    case '\\':
                        sb.Append("\\\\");
                        break;
                    case '"':
                        sb.Append("\\\"");
                        break;
                    case '\n':
                        sb.Append("\\n");
                        break;
                    case '\r':
                        sb.Append("\\r");
                        break;
                    case '\t':
                        sb.Append("\\t");
                        break;
                    default:
                        sb.Append(ch);
                        break;
                }
            }

            return sb.ToString();
        }

        private void ScheduleReconnect()
        {
            // This method may be called from non-main threads (MQTT callbacks).
            // Guard against touching Unity API off the main thread by marshalling first.
            if (SynchronizationContext.Current != mainThreadContext)
            {
                PostToMainThread(ScheduleReconnect);
                return;
            }

            if (!autoConnectOnStart || !isActiveAndEnabled) return;

            reconnectCts?.Cancel();
            reconnectCts = new CancellationTokenSource();
            var token = reconnectCts.Token;

            Task.Run(async () =>
            {
                try
                {
                    await Task.Delay(TimeSpan.FromSeconds(2), token);
                    if (!token.IsCancellationRequested)
                    {
                        await EnsureConnectedAsync();
                    }
                }
                catch (OperationCanceledException)
                {
                }
            });
        }

        private void PostToMainThread(Action action)
        {
            if (action == null) return;
            var ctx = mainThreadContext;
            if (ctx != null)
            {
                ctx.Post(_ => action(), null);
            }
            else
            {
                action();
            }
        }

        private async void OnDestroy()
        {
            isDisposing = true;
            reconnectCts?.Cancel();
            if (client != null && client.IsConnected)
            {
                try
                {
                    await client.DisconnectAsync(CancellationToken.None);
                }
                catch (Exception ex)
                {
                    Debug.LogWarning($"[RobotVoice] MQTT disconnect failed: {ex.Message}");
                }
            }

            client?.Dispose();
            connectLock?.Dispose();
            reconnectCts?.Dispose();
        }
    }
}
#else
using System;
using System.Threading.Tasks;
using UnityEngine;

namespace RobotVoice
{
    public class MqttIntentPublisher : MonoBehaviour
    {
        [Header("MQTT disabled (define ROBOTVOICE_USE_MQTT to enable)")]
        [SerializeField] private string sourceLabel = "unity_vosk";

        public Task EnsureConnectedAsync()
        {
            return Task.CompletedTask;
        }

        public Task PublishLaunchIntentAsync(string gameName, string rawText)
        {
            if (!string.IsNullOrWhiteSpace(gameName))
            {
                Debug.Log($"[RobotVoice] (No MQTT) LAUNCH_GAME game='{gameName}' source='{sourceLabel}' raw='{rawText}'");
            }
            return Task.CompletedTask;
        }

        public Task PublishExitIntentAsync(string rawText)
        {
            Debug.Log($"[RobotVoice] (No MQTT) BACK_HOME source='{sourceLabel}' raw='{rawText}'");
            return Task.CompletedTask;
        }
    }
}
#endif
