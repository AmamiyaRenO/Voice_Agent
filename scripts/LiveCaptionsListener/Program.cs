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
    // 简单去重：避免短时间内重复发布相同文本
    static string lastPublished = "";
    static DateTime lastPublishedAt = DateTime.MinValue;

    // TTS 抑制：订阅 robot/tts/state 后在 speaking=true 期间停止发布
    static volatile bool ttsSpeaking = false;
    static DateTime ttsLastUpdateUtc = DateTime.MinValue;
    static int ttsSuppressTailMs = 1200; // TTS 结束后再抑制一小段时间
    static string ttsLastTextLower = ""; // 最近一次 TTS 文本（小写）
    static int ttsEchoWindowMs = 3000; // 在此窗口内做文本级回声过滤

    // 语音口令门控：仅在听到 "start listening" 后才转发，"stop listening" 关闭
    static volatile bool listeningEnabled = false; // 默认关闭，等待口令开启

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

        // TTS 抑制窗口配置
        int tail;
        if (int.TryParse(Environment.GetEnvironmentVariable("LIVE_CAPTIONS_TTS_SUPPRESS_MS"), out tail) && tail >= 0)
        {
            ttsSuppressTailMs = tail;
        }
        int echoMs;
        if (int.TryParse(Environment.GetEnvironmentVariable("LIVE_CAPTIONS_TTS_ECHO_MS"), out echoMs) && echoMs >= 0)
        {
            ttsEchoWindowMs = echoMs;
        }

        // 启动 TTS 状态订阅（可选）
        if (!string.IsNullOrWhiteSpace(mqttConfig.TtsStateTopic))
        {
            Console.WriteLine($"[MQTT] 订阅 TTS 状态: {mqttConfig.TtsStateTopic}");
            _ = System.Threading.Tasks.Task.Run(() => MqttTtsSubscriber.RunAsync(mqttConfig, OnTtsStateMessageInternal));
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

                if (IsTtsActive()) return;
                string raw = ReadCaptionText(src);
                string text = CleanToLastLine(raw);
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
                if (IsTtsActive())
                {
                    Thread.Sleep(50);
                    continue;
                }

                string current = CleanToLastLine(ReadCaptionText(captionsBlock));

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
        sentence = CleanToLastLine(sentence);
        if (string.IsNullOrEmpty(sentence)) return;

        if (IsTtsActive()) return; // 播报时严格抑制

        // 若高度疑似为 TTS 回声，也不发送
        if (IsLikelyTtsEcho(sentence))
        {
            return;
        }

        // 语音口令门控：先处理指令，不外发
        if (TryHandleListeningCommand(sentence))
        {
            return;
        }

        // 未开启监听时，忽略普通句子
        if (!listeningEnabled)
        {
            return;
        }

        // 3 秒内相同文本不再发送
        if (sentence == lastPublished && (DateTime.UtcNow - lastPublishedAt).TotalSeconds < 3)
            return;
        lastPublished = sentence;
        lastPublishedAt = DateTime.UtcNow;

        Console.WriteLine($"🗣 完整句: {sentence}");

        // ⚙️ 发布到 MQTT（JSON 负载）
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

    // ✅ 读取字幕文本：优先使用 TextPattern；退化到 Name
    static string ReadCaptionText(AutomationElement element)
    {
        try
        {
            object patternObj;
            if (element.TryGetCurrentPattern(TextPattern.Pattern, out patternObj))
            {
                var tp = (TextPattern)patternObj;
                string all = tp.DocumentRange.GetText(-1) ?? string.Empty;
                return all.Trim();
            }
        }
        catch { }
        string name = element.Current.Name ?? string.Empty;
        return name.Trim();
    }

    // ✅ 仅取最后一行，去掉历史行
    static string CleanToLastLine(string text)
    {
        if (string.IsNullOrWhiteSpace(text)) return string.Empty;
        var parts = text.Replace("\r\n", "\n").Replace('\r', '\n').Split('\n');
        for (int i = parts.Length - 1; i >= 0; i--)
        {
            string line = parts[i].Trim();
            if (!string.IsNullOrEmpty(line)) return line;
        }
        return text.Trim();
    }

    // ✅ TTS 状态回调与抑制判定
    static void OnTtsStateMessageInternal(bool speaking, string text)
    {
        ttsSpeaking = speaking;
        ttsLastUpdateUtc = DateTime.UtcNow;
        if (speaking)
        {
            // 清空缓冲，避免 TTS 期间累积的旧片段被发出
            try { /* 局部变量在 Main 中，这里无法直接清空 */ } catch { }
            ttsLastTextLower = (text ?? string.Empty).Trim().ToLowerInvariant();
        }
        else
        {
            // 结束后仍保留文本用于尾随抑制匹配
            if (!string.IsNullOrEmpty(text))
                ttsLastTextLower = text.Trim().ToLowerInvariant();
        }
    }

    static bool IsTtsActive()
    {
        if (ttsSpeaking) return true;
        if (ttsLastUpdateUtc == DateTime.MinValue) return false;
        return (DateTime.UtcNow - ttsLastUpdateUtc).TotalMilliseconds < ttsSuppressTailMs;
    }

    static bool IsLikelyTtsEcho(string sentence)
    {
        if (string.IsNullOrWhiteSpace(sentence) || string.IsNullOrWhiteSpace(ttsLastTextLower)) return false;
        if ((DateTime.UtcNow - ttsLastUpdateUtc).TotalMilliseconds > ttsEchoWindowMs) return false;
        string s = sentence.Trim().ToLowerInvariant();
        if (s.Length < 6 || ttsLastTextLower.Length < 6) return false;
        if (ttsLastTextLower.Contains(s)) return true;
        if (s.Contains(ttsLastTextLower)) return true;
        // 简单重叠判断：若交集子串长度>=句子长度的60%
        int common = LongestCommonSubsequenceLength(s, ttsLastTextLower);
        if (common * 10 >= s.Length * 6) return true;
        return false;
    }

    static int LongestCommonSubsequenceLength(string a, string b)
    {
        int n = a.Length, m = b.Length;
        int[,] dp = new int[n + 1, m + 1];
        for (int i = 1; i <= n; i++)
        {
            for (int j = 1; j <= m; j++)
            {
                if (a[i - 1] == b[j - 1]) dp[i, j] = dp[i - 1, j - 1] + 1;
                else dp[i, j] = dp[i - 1, j] > dp[i, j - 1] ? dp[i - 1, j] : dp[i, j - 1];
            }
        }
        return dp[n, m];
    }

    // ✅ 处理 "start listening" / "stop listening" 指令
    static bool TryHandleListeningCommand(string sentence)
    {
        string norm = (sentence ?? string.Empty).Trim().ToLowerInvariant();
        if (norm.Length == 0) return false;

        // 去掉收尾标点
        while (norm.Length > 0 && ".!?。？！".IndexOf(norm[^1]) >= 0)
        {
            norm = norm.Substring(0, norm.Length - 1).TrimEnd();
        }

        bool matched = false;
        if (norm.Contains("start listening"))
        {
            listeningEnabled = true;
            matched = true;
            Console.WriteLine("[Gate] Listening enabled");
        }
        else if (norm.Contains("stop listening"))
        {
            listeningEnabled = false;
            matched = true;
            Console.WriteLine("[Gate] Listening disabled");
        }

        return matched;
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
            TtsStateTopic = "robot/tts/state";
        }

        private MqttConfig(string host, int port, string topic, string clientId, string source, string ttsStateTopic)
        {
            Host = host;
            Port = port;
            Topic = topic;
            ClientId = clientId;
            SourceLabel = source;
            TtsStateTopic = ttsStateTopic;
            Enabled = !string.IsNullOrWhiteSpace(topic);
        }

        public bool Enabled { get; }
        public string Host { get; }
        public int Port { get; }
        public string Topic { get; }
        public string ClientId { get; }
        public string SourceLabel { get; }
        public string TtsStateTopic { get; }

        public static MqttConfig Disabled { get; } = new MqttConfig();

        public static MqttConfig FromEnvironment()
        {
            string host = Environment.GetEnvironmentVariable("LIVE_CAPTIONS_MQTT_HOST") ?? "127.0.0.1";
            string topic = Environment.GetEnvironmentVariable("LIVE_CAPTIONS_MQTT_TOPIC") ?? "robot/voice/text";
            string clientId = Environment.GetEnvironmentVariable("LIVE_CAPTIONS_MQTT_CLIENT_ID") ?? $"live-captions-{Environment.MachineName}";
            string source = Environment.GetEnvironmentVariable("LIVE_CAPTIONS_SOURCE_LABEL") ?? "live_captions";
            string ttsStateTopic = Environment.GetEnvironmentVariable("LIVE_CAPTIONS_TTS_STATE_TOPIC") ?? "robot/tts/state";
            return new MqttConfig(host, 1883, topic, clientId, source, ttsStateTopic);
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
            Console.WriteLine($"[MQTT] 已发布: {config.Topic} ({payload.Length}B)");
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
            bool retain = (Environment.GetEnvironmentVariable("LIVE_CAPTIONS_MQTT_RETAIN") ?? "0").Trim() is string r &&
                          (r.Equals("1", StringComparison.OrdinalIgnoreCase) || r.Equals("true", StringComparison.OrdinalIgnoreCase));
            byte header = (byte)(0x30 | (retain ? 0x01 : 0x00));
            byte[] fixedHeader = BuildFixedHeader(header, remain);
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

    // --- Minimal MQTT subscriber for TTS state ---
    static class MqttTtsSubscriber
    {
        public static async System.Threading.Tasks.Task RunAsync(MqttConfig config, System.Action<bool, string> onTtsState)
        {
            while (true)
            {
                try
                {
                    using var client = new System.Net.Sockets.TcpClient();
                    await client.ConnectAsync(config.Host, config.Port);
                    using var stream = client.GetStream();

                    // CONNECT
                    byte[] connect = BuildConnectPacketLocal(config);
                    await stream.WriteAsync(connect, 0, connect.Length);
                    await stream.FlushAsync();
                    await ReadConnAckAsyncLocal(stream);

                    // SUBSCRIBE to TTS state topic, packet id = 1, QoS 0
                    byte[] subscribePacket = BuildSubscribePacket(config.TtsStateTopic, packetId: 1);
                    await stream.WriteAsync(subscribePacket, 0, subscribePacket.Length);
                    await stream.FlushAsync();

                    // Simple read loop
                    var buffer = new byte[8192];
                    while (true)
                    {
                        int header = stream.ReadByte();
                        if (header < 0) break;
                        byte first = (byte)header;
                        int remaining = ReadRemainingLength(stream);
                        if (remaining <= 0 || remaining > buffer.Length) {
                            // skip payload if too large
                            SkipBytes(stream, remaining);
                            continue;
                        }
                        int read = ReadExactAsync(stream, buffer, remaining);
                        if (read != remaining) break;

                        byte packetType = (byte)(first >> 4);
                        if (packetType == 3) // PUBLISH
                        {
                            int idx = 0;
                            if (remaining < 2) continue;
                            int topicLen = (buffer[idx] << 8) | buffer[idx + 1]; idx += 2;
                            if (topicLen < 0 || idx + topicLen > remaining) continue;
                            string topic = System.Text.Encoding.UTF8.GetString(buffer, idx, topicLen); idx += topicLen;

                            // QoS 0 assumed (no packet id)
                            int payloadLen = remaining - idx;
                            if (payloadLen <= 0) continue;
                            string json = System.Text.Encoding.UTF8.GetString(buffer, idx, payloadLen);
                            try
                            {
                                using var doc = System.Text.Json.JsonDocument.Parse(json);
                                if (doc.RootElement.TryGetProperty("speaking", out var speakingProp))
                                {
                                    bool speaking = speakingProp.ValueKind == System.Text.Json.JsonValueKind.True ||
                                                    (speakingProp.ValueKind == System.Text.Json.JsonValueKind.Number && speakingProp.GetInt32() != 0);
                                    string ttsText = null;
                                    if (doc.RootElement.TryGetProperty("text", out var textProp) && textProp.ValueKind == System.Text.Json.JsonValueKind.String)
                                    {
                                        ttsText = textProp.GetString();
                                    }
                                    onTtsState?.Invoke(speaking, ttsText);
                                }
                            }
                            catch { }
                        }
                        // ignore other packet types
                    }
                }
                catch
                {
                    // backoff and retry
                    System.Threading.Thread.Sleep(500);
                }
            }
        }

        static int ReadRemainingLength(System.IO.Stream s)
        {
            int multiplier = 1;
            int value = 0;
            while (true)
            {
                int digit = s.ReadByte();
                if (digit < 0) return -1;
                value += (digit & 127) * multiplier;
                if ((digit & 128) == 0) break;
                multiplier *= 128;
                if (multiplier > 128*128*128) return -1;
            }
            return value;
        }

        static int ReadExactAsync(System.IO.Stream s, byte[] buffer, int len)
        {
            int total = 0;
            while (total < len)
            {
                int n = s.Read(buffer, total, len - total);
                if (n <= 0) break;
                total += n;
            }
            return total;
        }

        static void SkipBytes(System.IO.Stream s, int len)
        {
            var tmp = new byte[1024];
            int remaining = len;
            while (remaining > 0)
            {
                int toRead = System.Math.Min(remaining, tmp.Length);
                int n = s.Read(tmp, 0, toRead);
                if (n <= 0) break;
                remaining -= n;
            }
        }

        static byte[] BuildSubscribePacket(string topic, ushort packetId)
        {
            byte[] topicBytes = System.Text.Encoding.UTF8.GetBytes(topic);
            int remain = 2 + 2 + topicBytes.Length + 1; // packetId(2) + topic len(2)+topic + QoS(1)
            var header = BuildFixedHeaderLocal(0x82, remain);
            var packet = new byte[header.Length + remain];
            System.Buffer.BlockCopy(header, 0, packet, 0, header.Length);
            int offset = header.Length;
            packet[offset++] = (byte)((packetId >> 8) & 0xFF);
            packet[offset++] = (byte)(packetId & 0xFF);
            packet[offset++] = (byte)((topicBytes.Length >> 8) & 0xFF);
            packet[offset++] = (byte)(topicBytes.Length & 0xFF);
            System.Buffer.BlockCopy(topicBytes, 0, packet, offset, topicBytes.Length);
            offset += topicBytes.Length;
            packet[offset++] = 0x00; // QoS 0
            return packet;
        }

        static byte[] BuildFixedHeaderLocal(byte type, int len)
        {
            using var ms = new System.IO.MemoryStream();
            ms.WriteByte(type);
            int value = len;
            do
            {
                byte encoded = (byte)(value % 128);
                value /= 128;
                if (value > 0) encoded |= 0x80;
                ms.WriteByte(encoded);
            } while (value > 0);
            return ms.ToArray();
        }

        static byte[] BuildConnectPacketLocal(MqttConfig c)
        {
            byte[] clientId = System.Text.Encoding.UTF8.GetBytes(c.ClientId);
            const int header = 10;
            int remain = header + 2 + clientId.Length;
            byte[] fixedHeader = BuildFixedHeaderLocal(0x10, remain);
            byte[] packet = new byte[fixedHeader.Length + remain];
            System.Buffer.BlockCopy(fixedHeader, 0, packet, 0, fixedHeader.Length);
            int offset = fixedHeader.Length;

            packet[offset++] = 0x00; packet[offset++] = 0x04;
            packet[offset++] = (byte)'M'; packet[offset++] = (byte)'Q';
            packet[offset++] = (byte)'T'; packet[offset++] = (byte)'T';
            packet[offset++] = 0x04;      // Protocol Level 4 (3.1.1)
            packet[offset++] = 0x02;      // Clean session
            packet[offset++] = 0x00; packet[offset++] = 0x3C; // Keepalive 60s

            packet[offset++] = (byte)((clientId.Length >> 8) & 0xFF);
            packet[offset++] = (byte)(clientId.Length & 0xFF);
            System.Buffer.BlockCopy(clientId, 0, packet, offset, clientId.Length);

            return packet;
        }

        static async System.Threading.Tasks.Task ReadConnAckAsyncLocal(System.IO.Stream s)
        {
            byte[] b = new byte[4];
            int read = 0;
            while (read < 4)
            {
                int n = await s.ReadAsync(b, read, 4 - read);
                if (n <= 0) throw new System.IO.IOException("MQTT connack read failed");
                read += n;
            }
            if (b[0] != 0x20 || b[3] != 0x00)
                throw new System.IO.IOException("MQTT broker rejected connection");
        }
    }
}
