using System;
using System.Collections.Concurrent;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Automation;

namespace LiveCaptionsBridge
{
    /// <summary>
    /// Monitors the Windows Live Captions surface via UI Automation and emits
    /// completed sentences using newline and silence detection. This is intended
    /// to bridge Live Captions output into other applications such as Unity.
    /// </summary>
    internal static class Program
    {
        private static readonly JsonSerializerOptions JsonOptions = new JsonSerializerOptions
        {
            PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
            WriteIndented = false,
        };

        private static string lastText = string.Empty;
        private static string sentenceBuffer = string.Empty;
        private static DateTime lastUpdate = DateTime.Now;
        private static Timer? silenceTimer;
        private static BlockingCollection<string>? publishQueue;
        private static CancellationTokenSource? publisherCts;
        private static Task? publisherTask;
        private static MqttConfig mqttConfig = MqttConfig.Disabled;
        private static bool shutdownInitiated;

        private static void Main(string[] args)
        {
            mqttConfig = MqttConfig.FromEnvironment(args);
            if (mqttConfig.Enabled)
            {
                Console.WriteLine(
                    $"[MQTT] Publishing Live Captions to {mqttConfig.Host}:{mqttConfig.Port} " +
                    $"topic '{mqttConfig.Topic}' as '{mqttConfig.ClientId}'."
                );

                publishQueue = new BlockingCollection<string>();
                publisherCts = new CancellationTokenSource();
                publisherTask = Task.Run(() => PublishLoopAsync(publishQueue, mqttConfig, publisherCts.Token));
            }
            else
            {
                Console.WriteLine("[MQTT] Disabled (set LIVE_CAPTIONS_MQTT_TOPIC or --mqtt-topic to enable publishing).");
            }

            Console.Title = "Live Captions Listener";
            Console.WriteLine("等待 Live Captions 窗口...");

            AutomationElement? captionElement = WaitForLiveCaptions();
            if (captionElement == null)
            {
                Console.WriteLine("未找到 Live Captions 窗口，请先按 Win+Ctrl+L 打开。");
                Shutdown();
                return;
            }

            Console.WriteLine("已找到 Live Captions，开始监听。");

            Automation.AddAutomationEventHandler(
                TextPattern.TextChangedEvent,
                captionElement,
                TreeScope.Element,
                OnTextChanged
            );

            // Timer used to detect silence between phrases.
            silenceTimer = new Timer(CheckSilence, null, 0, 300);

            Console.CancelKeyPress += (_, eventArgs) =>
            {
                eventArgs.Cancel = true;
                Shutdown();
            };

            Console.ReadLine();
            Shutdown();
        }

        private static void Shutdown()
        {
            if (shutdownInitiated)
            {
                return;
            }

            shutdownInitiated = true;
            Automation.RemoveAllEventHandlers();
            silenceTimer?.Dispose();
            silenceTimer = null;

            if (publishQueue != null && !publishQueue.IsAddingCompleted)
            {
                publishQueue.CompleteAdding();
            }

            if (publisherCts != null && !publisherCts.IsCancellationRequested)
            {
                publisherCts.Cancel();
            }

            try
            {
                publisherTask?.Wait(TimeSpan.FromSeconds(2));
            }
            catch (AggregateException)
            {
            }

            publisherTask = null;
            publisherCts?.Dispose();
            publisherCts = null;
            publishQueue?.Dispose();
            publishQueue = null;

            Environment.Exit(0);
        }

        private static AutomationElement? WaitForLiveCaptions()
        {
            for (int i = 0; i < 50; i++)
            {
                AutomationElement root = AutomationElement.RootElement;
                AutomationElementCollection windows = root.FindAll(TreeScope.Children, Condition.TrueCondition);
                foreach (AutomationElement win in windows)
                {
                    string name = win.Current.Name ?? string.Empty;
                    if (name.Contains("Live captions", StringComparison.OrdinalIgnoreCase) ||
                        name.Contains("实时字幕", StringComparison.OrdinalIgnoreCase))
                    {
                        TreeWalker walker = TreeWalker.ContentViewWalker;
                        AutomationElement? node = walker.GetFirstChild(win);
                        while (node != null)
                        {
                            if (node.TryGetCurrentPattern(TextPattern.Pattern, out _))
                            {
                                return node; // Subtitle text control found
                            }

                            node = walker.GetNextSibling(node);
                        }
                    }
                }

                Thread.Sleep(500);
            }

            return null;
        }

        private static void OnTextChanged(object sender, AutomationEventArgs e)
        {
            try
            {
                var automationElement = (AutomationElement)sender;
                var textPattern = (TextPattern)automationElement.GetCurrentPattern(TextPattern.Pattern);
                string text = textPattern.DocumentRange.GetText(-1);

                if (text == lastText)
                {
                    return;
                }

                lastUpdate = DateTime.Now;

                string newPart = GetNewPart(lastText, text);
                if (!string.IsNullOrWhiteSpace(newPart))
                {
                    sentenceBuffer += " " + newPart.Trim();
                }

                lastText = text;

                if (text.Contains('\n'))
                {
                    FinalizeSentence();
                }
            }
            catch (InvalidCastException)
            {
                // UI Automation can occasionally surface stale elements; ignore and keep listening.
            }
            catch (ElementNotAvailableException)
            {
                // Live Captions window was closed or re-created. The main loop will pick it up again
                // after the process is restarted.
            }
        }

        private static string GetNewPart(string oldText, string newText)
        {
            if (string.IsNullOrWhiteSpace(oldText))
            {
                return newText;
            }

            return newText.EndsWith(oldText, StringComparison.Ordinal)
                ? string.Empty
                : newText.Replace(oldText, string.Empty, StringComparison.Ordinal).Trim();
        }

        private static void CheckSilence(object? state)
        {
            if ((DateTime.Now - lastUpdate).TotalMilliseconds > 1200 && sentenceBuffer.Length > 0)
            {
                FinalizeSentence();
            }
        }

        private static void FinalizeSentence()
        {
            string sentence = sentenceBuffer.Trim();
            if (sentence.Length > 0)
            {
                Console.WriteLine($"[Sentence] {sentence}");
                EnqueueForPublish(sentence);
            }

            sentenceBuffer = string.Empty;
        }

        private static void EnqueueForPublish(string sentence)
        {
            if (!mqttConfig.Enabled || publishQueue == null)
            {
                return;
            }

            try
            {
                publishQueue.Add(sentence);
            }
            catch (InvalidOperationException)
            {
                // Queue has been marked as complete during shutdown.
            }
        }

        private static async Task PublishLoopAsync(
            BlockingCollection<string> queue,
            MqttConfig config,
            CancellationToken cancellationToken)
        {
            foreach (string sentence in queue.GetConsumingEnumerable(cancellationToken))
            {
                try
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    string payload = BuildPayload(config.SourceLabel, sentence);
                    await SimpleMqttPublisher.PublishOnceAsync(config, payload, cancellationToken).ConfigureAwait(false);
                }
                catch (OperationCanceledException)
                {
                    break;
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"[MQTT] 发布失败: {ex.Message}");
                    await Task.Delay(1000, CancellationToken.None).ConfigureAwait(false);
                }
            }
        }

        private static string BuildPayload(string sourceLabel, string sentence)
        {
            var payload = new LiveCaptionPayload
            {
                Type = "LIVE_CAPTION",
                Text = sentence,
                Source = sourceLabel,
                Timestamp = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture),
            };

            return JsonSerializer.Serialize(payload, JsonOptions);
        }

        private sealed class LiveCaptionPayload
        {
            public string Type { get; set; } = string.Empty;

            public string Text { get; set; } = string.Empty;

            public string Source { get; set; } = string.Empty;

            public string Timestamp { get; set; } = string.Empty;
        }

        private sealed class MqttConfig
        {
            private MqttConfig()
            {
                Enabled = false;
                Host = "127.0.0.1";
                Port = 1883;
                Topic = string.Empty;
                ClientId = "live-captions-listener";
                Username = string.Empty;
                Password = string.Empty;
                SourceLabel = "live_captions";
            }

            private MqttConfig(
                string host,
                int port,
                string topic,
                string clientId,
                string username,
                string password,
                string sourceLabel)
            {
                Host = host;
                Port = port;
                Topic = topic;
                ClientId = clientId;
                Username = username;
                Password = password;
                SourceLabel = sourceLabel;
                Enabled = !string.IsNullOrWhiteSpace(topic);
            }

            public static MqttConfig Disabled { get; } = new MqttConfig();

            public bool Enabled { get; }

            public string Host { get; }

            public int Port { get; }

            public string Topic { get; }

            public string ClientId { get; }

            public string Username { get; }

            public string Password { get; }

            public string SourceLabel { get; }

            public static MqttConfig FromEnvironment(string[] args)
            {
                string host = GetValue("LIVE_CAPTIONS_MQTT_HOST", args, "mqtt-host") ?? "127.0.0.1";
                string topic = GetValue("LIVE_CAPTIONS_MQTT_TOPIC", args, "mqtt-topic") ?? "robot/live_captions";
                string clientId = GetValue("LIVE_CAPTIONS_MQTT_CLIENT_ID", args, "mqtt-client-id")
                    ?? $"live-captions-{Environment.MachineName}";
                string username = GetValue("LIVE_CAPTIONS_MQTT_USERNAME", args, "mqtt-username") ?? string.Empty;
                string password = GetValue("LIVE_CAPTIONS_MQTT_PASSWORD", args, "mqtt-password") ?? string.Empty;
                string sourceLabel = GetValue("LIVE_CAPTIONS_SOURCE_LABEL", args, "source-label") ?? "live_captions";

                int port = 1883;
                string? portValue = GetValue("LIVE_CAPTIONS_MQTT_PORT", args, "mqtt-port");
                if (!string.IsNullOrWhiteSpace(portValue) && !int.TryParse(portValue, out port))
                {
                    port = 1883;
                }

                if (string.IsNullOrWhiteSpace(topic))
                {
                    return Disabled;
                }

                return new MqttConfig(
                    host.Trim(),
                    port,
                    topic.Trim(),
                    string.IsNullOrWhiteSpace(clientId) ? $"live-captions-{Guid.NewGuid():N}" : clientId.Trim(),
                    username?.Trim() ?? string.Empty,
                    password ?? string.Empty,
                    string.IsNullOrWhiteSpace(sourceLabel) ? "live_captions" : sourceLabel.Trim()
                );
            }

            private static string? GetValue(string environmentKey, string[] args, string argumentKey)
            {
                string? value = Environment.GetEnvironmentVariable(environmentKey);
                if (!string.IsNullOrWhiteSpace(value))
                {
                    return value;
                }

                string prefix = "--" + argumentKey + "=";
                foreach (string arg in args)
                {
                    if (arg.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                    {
                        return arg.Substring(prefix.Length);
                    }
                }

                return null;
            }
        }

        private static class SimpleMqttPublisher
        {
            public static async Task PublishOnceAsync(MqttConfig config, string payload, CancellationToken cancellationToken)
            {
                using var client = new TcpClient();
                await client.ConnectAsync(config.Host, config.Port, cancellationToken).ConfigureAwait(false);

                using NetworkStream stream = client.GetStream();

                byte[] connectPacket = BuildConnectPacket(config);
                await stream.WriteAsync(connectPacket.AsMemory(0, connectPacket.Length), cancellationToken).ConfigureAwait(false);
                await stream.FlushAsync(cancellationToken).ConfigureAwait(false);

                await ReadConnAckAsync(stream, cancellationToken).ConfigureAwait(false);

                byte[] publishPacket = BuildPublishPacket(config.Topic, payload);
                await stream.WriteAsync(publishPacket.AsMemory(0, publishPacket.Length), cancellationToken).ConfigureAwait(false);
                await stream.FlushAsync(cancellationToken).ConfigureAwait(false);

                byte[] disconnectPacket = new byte[] { 0xE0, 0x00 };
                await stream.WriteAsync(disconnectPacket.AsMemory(0, disconnectPacket.Length), cancellationToken).ConfigureAwait(false);
                await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
            }

            private static byte[] BuildConnectPacket(MqttConfig config)
            {
                byte[] clientIdBytes = Encoding.UTF8.GetBytes(config.ClientId);
                byte[] usernameBytes = string.IsNullOrEmpty(config.Username) ? Array.Empty<byte>() : Encoding.UTF8.GetBytes(config.Username);
                byte[] passwordBytes = string.IsNullOrEmpty(config.Password) ? Array.Empty<byte>() : Encoding.UTF8.GetBytes(config.Password);

                int payloadLength = 2 + clientIdBytes.Length;
                if (usernameBytes.Length > 0)
                {
                    payloadLength += 2 + usernameBytes.Length;
                }

                if (passwordBytes.Length > 0)
                {
                    payloadLength += 2 + passwordBytes.Length;
                }

                const int variableHeaderLength = 10; // Protocol name (6) + level (1) + flags (1) + keep alive (2)
                int remainingLength = variableHeaderLength + payloadLength;

                byte[] fixedHeader = BuildFixedHeader(0x10, remainingLength);
                byte[] packet = new byte[fixedHeader.Length + remainingLength];

                int offset = 0;
                Buffer.BlockCopy(fixedHeader, 0, packet, offset, fixedHeader.Length);
                offset += fixedHeader.Length;

                // Protocol name "MQTT"
                packet[offset++] = 0x00;
                packet[offset++] = 0x04;
                packet[offset++] = (byte)'M';
                packet[offset++] = (byte)'Q';
                packet[offset++] = (byte)'T';
                packet[offset++] = (byte)'T';

                // Protocol level 4 (MQTT 3.1.1)
                packet[offset++] = 0x04;

                byte connectFlags = 0x02; // Clean session
                if (usernameBytes.Length > 0)
                {
                    connectFlags |= 0x80;
                }

                if (passwordBytes.Length > 0)
                {
                    connectFlags |= 0x40;
                }

                packet[offset++] = connectFlags;

                // Keep alive = 60 seconds
                packet[offset++] = 0x00;
                packet[offset++] = 0x3C;

                offset = WriteMqttString(packet, offset, clientIdBytes);

                if (usernameBytes.Length > 0)
                {
                    offset = WriteMqttString(packet, offset, usernameBytes);
                }

                if (passwordBytes.Length > 0)
                {
                    offset = WriteMqttString(packet, offset, passwordBytes);
                }

                return packet;
            }

            private static byte[] BuildPublishPacket(string topic, string payload)
            {
                byte[] topicBytes = Encoding.UTF8.GetBytes(topic);
                byte[] payloadBytes = Encoding.UTF8.GetBytes(payload);

                int remainingLength = 2 + topicBytes.Length + payloadBytes.Length;
                byte[] fixedHeader = BuildFixedHeader(0x30, remainingLength);
                byte[] packet = new byte[fixedHeader.Length + remainingLength];

                int offset = 0;
                Buffer.BlockCopy(fixedHeader, 0, packet, offset, fixedHeader.Length);
                offset += fixedHeader.Length;

                offset = WriteMqttString(packet, offset, topicBytes);
                Buffer.BlockCopy(payloadBytes, 0, packet, offset, payloadBytes.Length);

                return packet;
            }

            private static byte[] BuildFixedHeader(byte controlPacketType, int remainingLength)
            {
                Span<byte> encodedLength = stackalloc byte[4];
                int index = 0;
                int value = remainingLength;

                do
                {
                    byte encodedByte = (byte)(value % 128);
                    value /= 128;
                    if (value > 0)
                    {
                        encodedByte |= 0x80;
                    }

                    encodedLength[index++] = encodedByte;
                }
                while (value > 0 && index < encodedLength.Length);

                byte[] header = new byte[1 + index];
                header[0] = controlPacketType;
                for (int i = 0; i < index; i++)
                {
                    header[1 + i] = encodedLength[i];
                }

                return header;
            }

            private static int WriteMqttString(byte[] buffer, int offset, byte[] data)
            {
                buffer[offset++] = (byte)((data.Length >> 8) & 0xFF);
                buffer[offset++] = (byte)(data.Length & 0xFF);
                Buffer.BlockCopy(data, 0, buffer, offset, data.Length);
                return offset + data.Length;
            }

            private static async Task ReadConnAckAsync(NetworkStream stream, CancellationToken cancellationToken)
            {
                byte[] buffer = new byte[1];
                int read = await stream.ReadAsync(buffer.AsMemory(0, 1), cancellationToken).ConfigureAwait(false);
                if (read != 1 || (buffer[0] & 0xF0) != 0x20)
                {
                    throw new IOException("MQTT broker did not return a CONNACK packet.");
                }

                int remainingLength = await ReadRemainingLengthAsync(stream, cancellationToken).ConfigureAwait(false);
                if (remainingLength < 2)
                {
                    throw new IOException("MQTT CONNACK packet malformed.");
                }

                byte[] payload = new byte[remainingLength];
                int offset = 0;
                while (offset < payload.Length)
                {
                    int chunk = await stream.ReadAsync(payload.AsMemory(offset, payload.Length - offset), cancellationToken).ConfigureAwait(false);
                    if (chunk == 0)
                    {
                        throw new IOException("MQTT connection closed while reading CONNACK.");
                    }

                    offset += chunk;
                }

                byte returnCode = payload[1];
                if (returnCode != 0x00)
                {
                    throw new IOException($"MQTT broker rejected connection (code {returnCode}).");
                }
            }

            private static async Task<int> ReadRemainingLengthAsync(NetworkStream stream, CancellationToken cancellationToken)
            {
                int multiplier = 1;
                int value = 0;
                byte encodedByte;
                int loops = 0;

                do
                {
                    loops++;
                    if (loops > 4)
                    {
                        throw new IOException("MQTT remaining length is malformed.");
                    }

                    byte[] buffer = new byte[1];
                    int read = await stream.ReadAsync(buffer.AsMemory(0, 1), cancellationToken).ConfigureAwait(false);
                    if (read != 1)
                    {
                        throw new IOException("MQTT connection closed while reading remaining length.");
                    }

                    encodedByte = buffer[0];
                    value += (encodedByte & 0x7F) * multiplier;
                    multiplier *= 128;
                }
                while ((encodedByte & 0x80) != 0);

                return value;
            }
        }
    }
}
