#if ROBOTVOICE_USE_MQTT
using System;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using MQTTnet;
using MQTTnet.Client;
using MQTTnet.Client.Options;
using MQTTnet.Protocol;
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
        [SerializeField] private bool autoConnectOnStart = true;
        [SerializeField] private string sourceLabel = "unity_vosk";

        private IMqttClient client;
        private IMqttClientOptions options;
        private readonly SemaphoreSlim connectLock = new SemaphoreSlim(1, 1);
        private MqttFactory factory;

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
            factory = new MqttFactory();
            client = factory.CreateMqttClient();
            client.ConnectedAsync += e =>
            {
                Debug.Log($"[RobotVoice] Connected to MQTT {host}:{port}");
                return Task.CompletedTask;
            };
            client.DisconnectedAsync += async e =>
            {
                Debug.LogWarning($"[RobotVoice] Disconnected from MQTT: {e.Reason}");
                if (autoConnectOnStart)
                {
                    await Task.Delay(TimeSpan.FromSeconds(2));
                    await EnsureConnectedAsync();
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

                var builder = new MqttClientOptionsBuilder()
                    .WithTcpServer(host, port)
                    .WithClientId(string.IsNullOrWhiteSpace(clientId) ? Guid.NewGuid().ToString("N") : clientId)
                    .WithKeepAlivePeriod(TimeSpan.FromSeconds(15))
                    .WithCleanSession();

                if (!string.IsNullOrEmpty(username))
                {
                    builder = builder.WithCredentials(username, password ?? string.Empty);
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

            var message = new MqttApplicationMessageBuilder()
                .WithTopic(intentTopic)
                .WithPayload(payload)
                .WithQualityOfServiceLevel(MqttQualityOfServiceLevel.AtLeastOnce)
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

        private async void OnDestroy()
        {
            if (client != null && client.IsConnected)
            {
                try
                {
                    await client.DisconnectAsync();
                }
                catch (Exception ex)
                {
                    Debug.LogWarning($"[RobotVoice] MQTT disconnect failed: {ex.Message}");
                }
            }

            client?.Dispose();
            connectLock?.Dispose();
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