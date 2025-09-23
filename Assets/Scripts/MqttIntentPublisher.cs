#if ROBOTVOICE_USE_MQTT
using System;
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

        private SimpleMqttClient client;
        private SimpleMqttClientOptions options;
        private readonly SemaphoreSlim connectLock = new SemaphoreSlim(1, 1);
        private CancellationTokenSource reconnectCts;
        private bool isDisposing;

        private void Awake()
        {
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
                Debug.Log($"[RobotVoice] Connected to MQTT {host}:{port}");
            };
            client.Disconnected += reason =>
            {
                Debug.LogWarning($"[RobotVoice] Disconnected from MQTT: {reason}");
                if (!isDisposing)
                {
                    ScheduleReconnect();
                }
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

            await PublishAsync(BuildLaunchPayload(gameName.Trim(), rawText));
        }

        public async Task PublishExitIntentAsync(string rawText)
        {
            await PublishAsync(BuildExitPayload(rawText));
        }

        private async Task PublishAsync(string payload)
        {
            await EnsureConnectedAsync();
            if (client == null || !client.IsConnected)
            {
                Debug.LogWarning("[RobotVoice] MQTT client not connected; intent not sent");
                return;
            }

            var message = new SimpleMqttApplicationMessageBuilder()
                .WithTopic(intentTopic)
                .WithPayload(payload)
                .WithQualityOfServiceLevel(SimpleMqttQualityOfServiceLevel.AtLeastOnce)
                .Build();

            try
            {
                await client.PublishAsync(message, CancellationToken.None);
                Debug.Log($"[RobotVoice] Intent published: {payload}");
            }
            catch (Exception ex)
            {
                Debug.LogError($"[RobotVoice] Failed to publish MQTT intent: {ex.Message}");
            }
        }

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
            if (!autoConnectOnStart || !isActiveAndEnabled)
            {
                return;
            }

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
