using System;
using System.Text;
using System.Threading;
using System.Windows.Automation;
using System.Globalization;
using System.Net.Sockets;
using System.IO;
using System.Text.Json;
using System.Threading.Tasks;

class Program
{
    [STAThread]
    static void Main()
    {
        // MQTT 配置初始化（通过环境变量）
        mqttConfig = MqttConfig.FromEnvironment();
        if (mqttConfig.Enabled)
        {
            Console.WriteLine($"[MQTT] 将发布到 {mqttConfig.Host}:{mqttConfig.Port} topic '{mqttConfig.Topic}' as '{mqttConfig.ClientId}'.");
        }
        else
        {
            Console.WriteLine("[MQTT] 未启用（设置 LIVE_CAPTIONS_MQTT_TOPIC 可开启发布）。");
        }

        Console.OutputEncoding = Encoding.UTF8;
        Console.WriteLine("🔍 正在查找 Live Captions 窗口…");

        // 1️⃣ 查找 Live Captions 主窗口
        var root = AutomationElement.RootElement;
        var live = root.FindFirst(TreeScope.Subtree,
            new OrCondition(
                new PropertyCondition(AutomationElement.NameProperty, "Live Captions", PropertyConditionFlags.IgnoreCase),
                new PropertyCondition(AutomationElement.NameProperty, "实时字幕"),
                new PropertyCondition(AutomationElement.NameProperty, "实时辅助字幕")
            ));

        if (live == null)
        {
            Console.WriteLine("❌ 未找到 Live Captions，请先按 Win + Ctrl + L 开启。");
            return;
        }

        Console.WriteLine("✅ 找到字幕窗口！");

        // 2️⃣ 查找 CaptionsTextBlock 元素
        var captionsBlock = FindCaptionsBlock(live);
        if (captionsBlock == null)
        {
            Console.WriteLine("⚠️ 未找到 CaptionsTextBlock 控件。请确认系统版本支持此 AutomationId。");
            return;
        }

        Console.WriteLine("✅ 找到字幕文本控件 CaptionsTextBlock！");
        Console.WriteLine("📡 正在监听字幕变化…");

        string lastText = "";
        string stableSentence = "";
        DateTime lastChange = DateTime.Now;
        bool sentenceSent = false;

        // 🧩 LiveRegionChanged 事件监听
        AutomationEventHandler liveRegionChanged = (sender, e) =>
        {
            try
            {
                var src = sender as AutomationElement ?? captionsBlock;
                if (src == null) return;

                string text = src.Current.Name?.Trim() ?? "";
                if (!string.IsNullOrEmpty(text) && text != lastText)
                {
                    lastChange = DateTime.Now;
                    stableSentence = text;
                    lastText = text;
                    sentenceSent = false;
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"⚠️ 事件读取失败: {ex.Message}");
            }
        };

        Automation.AddAutomationEventHandler(
            AutomationElementIdentifiers.LiveRegionChangedEvent,
            captionsBlock,
            TreeScope.Element,
            liveRegionChanged
        );

        // 3️⃣ 主循环 - 检测稳定文本并输出完整句
        while (true)
        {
            try
            {
                string current = captionsBlock.Current.Name?.Trim() ?? "";

                if (!string.IsNullOrEmpty(current))
                {
                    // 如果字幕变化
                    if (current != lastText)
                    {
                        lastChange = DateTime.Now;
                        stableSentence = current;
                        lastText = current;
                        sentenceSent = false;
                    }
                    else
                    {
                        // 若 1 秒无变化且还没发，说明一句结束
                        if (!sentenceSent && (DateTime.Now - lastChange).TotalMilliseconds > 1000)
                        {
                            if (IsSentenceEnd(stableSentence))
                            {
                                SendSentence(stableSentence);
                                sentenceSent = true;
                            }
                        }
                    }
                }
                else
                {
                    // Live Captions 清空 → 上一句结束
                    if (!sentenceSent && !string.IsNullOrEmpty(stableSentence))
                    {
                        SendSentence(stableSentence);
                        sentenceSent = true;
                    }

                    lastText = "";
                    stableSentence = "";
                }
            }
            catch (ElementNotAvailableException)
            {
                Console.WriteLine("⚠️ 控件失效，尝试重新查找…");
                captionsBlock = FindCaptionsBlock(live);
            }
            catch (Exception ex)
            {
                Console.WriteLine($"⚠️ 错误: {ex.Message}");
            }

            Thread.Sleep(150); // 每秒约7次检查
        }
    }

    // ✅ 句尾标点检测函数
    static bool IsSentenceEnd(string text)
    {
        if (string.IsNullOrEmpty(text)) return false;
        char last = text[^1];
        return ".!?。？！".Contains(last);
    }

    // ✅ 输出或发MQTT的统一函数
    static void SendSentence(string sentence)
    {
        sentence = sentence.Trim();
        if (string.IsNullOrEmpty(sentence)) return;

        Console.WriteLine($"🗣 完整句: {sentence}");

        // ⚙️ 发布到 MQTT（JSON 负载，与 LiveCaptionsBridge 保持一致）
        if (mqttConfig.Enabled)
        {
            try
            {
                var payload = JsonSerializer.Serialize(new
                {
                    type = "LIVE_CAPTION",
                    text = sentence,
                    source = mqttConfig.SourceLabel,
                    timestamp = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)
                });

                // 单次连接发布，避免常驻连接依赖
                _ = Task.Run(async () =>
                {
                    try
                    {
                        await SimpleMqttPublisher.PublishOnceAsync(mqttConfig, payload, CancellationToken.None).ConfigureAwait(false);
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"[MQTT] 发布失败: {ex.Message}");
                    }
                });
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[MQTT] 构建负载失败: {ex.Message}");
            }
        }
    }

    // ✅ 查找 CaptionsTextBlock
    static AutomationElement FindCaptionsBlock(AutomationElement live)
    {
        try
        {
            var element = live.FindFirst(
                TreeScope.Descendants,
                new PropertyCondition(AutomationElement.AutomationIdProperty, "CaptionsTextBlock")
            );
            return element;
        }
        catch (Exception ex)
        {
            Console.WriteLine($"⚠️ 查找 CaptionsTextBlock 出错: {ex.Message}");
            return null;
        }
    }

    // --- MQTT CONFIG & SIMPLE PUBLISHER ---
    static MqttConfig mqttConfig = MqttConfig.Disabled;

    sealed class MqttConfig
    {
        private MqttConfig()
        {
            Enabled = false;
            Host = "127.0.0.1";
            Port = 1883;
            Topic = "";
            ClientId = $"live-captions-{Environment.MachineName}";
            SourceLabel = "live_captions";
        }

        private MqttConfig(string host, int port, string topic, string clientId, string source)
        {
            Host = host;
            Port = port;
            Topic = topic;
            ClientId = clientId;
            SourceLabel = source;
            Enabled = !string.IsNullOrWhiteSpace(topic);
        }

        public bool Enabled { get; }
        public string Host { get; }
        public int Port { get; }
        public string Topic { get; }
        public string ClientId { get; }
        public string SourceLabel { get; }

        public static MqttConfig Disabled { get; } = new MqttConfig();

        public static MqttConfig FromEnvironment()
        {
            string host = Environment.GetEnvironmentVariable("LIVE_CAPTIONS_MQTT_HOST") ?? "127.0.0.1";
            string topic = Environment.GetEnvironmentVariable("LIVE_CAPTIONS_MQTT_TOPIC") ?? "robot/live_captions";
            string clientId = Environment.GetEnvironmentVariable("LIVE_CAPTIONS_MQTT_CLIENT_ID") ?? $"live-captions-{Environment.MachineName}";
            string source = Environment.GetEnvironmentVariable("LIVE_CAPTIONS_SOURCE_LABEL") ?? "live_captions";
            return new MqttConfig(host, 1883, topic, clientId, source);
        }
    }

    static class SimpleMqttPublisher
    {
        public static async Task PublishOnceAsync(MqttConfig config, string payload, CancellationToken token)
        {
            using var client = new TcpClient();
            await client.ConnectAsync(config.Host, config.Port, token);
            using NetworkStream stream = client.GetStream();

            byte[] connect = BuildConnectPacket(config);
            await stream.WriteAsync(connect, 0, connect.Length, token);
            await stream.FlushAsync(token);
            await ReadConnAckAsync(stream, token);

            byte[] publish = BuildPublishPacket(config.Topic, payload);
            await stream.WriteAsync(publish, 0, publish.Length, token);
            await stream.FlushAsync(token);

            byte[] disconnect = new byte[] { 0xE0, 0x00 };
            await stream.WriteAsync(disconnect, 0, disconnect.Length, token);
        }

        static byte[] BuildConnectPacket(MqttConfig c)
        {
            byte[] clientId = Encoding.UTF8.GetBytes(c.ClientId);
            const int header = 10;
            int remain = header + 2 + clientId.Length;
            byte[] fixedHeader = BuildFixedHeader(0x10, remain);
            byte[] packet = new byte[fixedHeader.Length + remain];
            Buffer.BlockCopy(fixedHeader, 0, packet, 0, fixedHeader.Length);
            int offset = fixedHeader.Length;

            packet[offset++] = 0x00; packet[offset++] = 0x04;
            packet[offset++] = (byte)'M'; packet[offset++] = (byte)'Q';
            packet[offset++] = (byte)'T'; packet[offset++] = (byte)'T';
            packet[offset++] = 0x04;
            packet[offset++] = 0x02;
            packet[offset++] = 0x00; packet[offset++] = 0x3C;

            packet[offset++] = (byte)((clientId.Length >> 8) & 0xFF);
            packet[offset++] = (byte)(clientId.Length & 0xFF);
            Buffer.BlockCopy(clientId, 0, packet, offset, clientId.Length);

            return packet;
        }

        static byte[] BuildPublishPacket(string topic, string payload)
        {
            byte[] topicBytes = Encoding.UTF8.GetBytes(topic);
            byte[] payloadBytes = Encoding.UTF8.GetBytes(payload);
            int remain = 2 + topicBytes.Length + payloadBytes.Length;
            byte[] fixedHeader = BuildFixedHeader(0x30, remain);
            byte[] packet = new byte[fixedHeader.Length + remain];
            Buffer.BlockCopy(fixedHeader, 0, packet, 0, fixedHeader.Length);
            int offset = fixedHeader.Length;
            packet[offset++] = (byte)((topicBytes.Length >> 8) & 0xFF);
            packet[offset++] = (byte)(topicBytes.Length & 0xFF);
            Buffer.BlockCopy(topicBytes, 0, packet, offset, topicBytes.Length);
            offset += topicBytes.Length;
            Buffer.BlockCopy(payloadBytes, 0, packet, offset, payloadBytes.Length);
            return packet;
        }

        static byte[] BuildFixedHeader(byte type, int len)
        {
            using var ms = new MemoryStream();
            ms.WriteByte(type);
            do
            {
                byte encoded = (byte)(len % 128);
                len /= 128;
                if (len > 0) encoded |= 0x80;
                ms.WriteByte(encoded);
            } while (len > 0);
            return ms.ToArray();
        }

        static async Task ReadConnAckAsync(NetworkStream s, CancellationToken token)
        {
            byte[] b = new byte[4];
            await s.ReadAsync(b, 0, 4, token);
            if (b[0] != 0x20 || b[3] != 0x00)
                throw new IOException("MQTT broker rejected connection");
        }
    }
}
