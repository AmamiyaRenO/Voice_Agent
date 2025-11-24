using System;
using System.Text;
using System.Threading;
using System.Windows.Automation;
using System.Globalization;
using System.Net.Sockets;
using System.IO;
using System.Text.Json;
using System.Runtime.InteropServices;
using System.Diagnostics;
using System.Threading.Tasks;

class Program
{
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    private const int SW_HIDE = 0;

    static string lastPublished = "";
    static DateTime lastPublishedAt = DateTime.MinValue;
    static volatile bool ttsSpeaking = false;
    static DateTime ttsLastUpdateUtc = DateTime.MinValue;
    static int ttsSuppressTailMs = 1200;
    static string ttsLastTextLower = "";
    static int ttsEchoWindowMs = 3000;
    static volatile bool listeningEnabled = true;
    static MqttConfig mqttConfig = MqttConfig.Disabled;

    [STAThread]
    static void Main()
    {
        Console.OutputEncoding = Encoding.UTF8;

        mqttConfig = MqttConfig.FromEnvironment();
        if (mqttConfig.Enabled)
            Console.WriteLine($"[MQTT] -> {mqttConfig.Host}:{mqttConfig.Port} topic '{mqttConfig.Topic}' as '{mqttConfig.ClientId}'");
        else
            Console.WriteLine("[MQTT] disabled (set LIVE_CAPTIONS_MQTT_TOPIC to enable).");

        if (int.TryParse(Environment.GetEnvironmentVariable("LIVE_CAPTIONS_TTS_SUPPRESS_MS"), out var tail) && tail >= 0)
            ttsSuppressTailMs = tail;
        if (int.TryParse(Environment.GetEnvironmentVariable("LIVE_CAPTIONS_TTS_ECHO_MS"), out var echoMs) && echoMs >= 0)
            ttsEchoWindowMs = echoMs;

        if (!string.IsNullOrWhiteSpace(mqttConfig.TtsStateTopic))
        {
            Console.WriteLine($"[MQTT] subscribing TTS state: {mqttConfig.TtsStateTopic}");
            _ = Task.Run(() => MqttTtsSubscriber.RunAsync(mqttConfig, OnTtsStateMessageInternal));
        }

        Console.WriteLine("== Bootstrapping Live Captions ==");
        var (liveWindow, captionsBlock) = BootstrapLiveCaptions();

        if (liveWindow == null)
        {
            Console.WriteLine("❌ Live Captions window not found. Press any key to exit.");
            Console.ReadKey();
            return;
        }

        if (captionsBlock == null)
            Console.WriteLine("⚠️ CaptionsTextBlock not found even after enabling mic. Will retry in loop.");
        else
            Console.WriteLine("✅ CaptionsTextBlock ready.");

        string lastText = "";
        string stableSentence = "";
        DateTime lastChange = DateTime.Now;
        bool sentenceSent = false;

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
                Console.WriteLine($"⚠️ LiveRegionChanged read failed: {ex.Message}");
            }
        };

        if (captionsBlock != null)
        {
            Automation.AddAutomationEventHandler(
                AutomationElementIdentifiers.LiveRegionChangedEvent,
                captionsBlock,
                TreeScope.Element,
                liveRegionChanged
            );
        }

        // 参考 PowerShell 脚本：在所有顶层窗口中寻找包含 CaptionsTextBlock 的树并隐藏之
        TryHideLiveCaptionsWindow();

        Console.WriteLine("📡 Listening...");
        for (;;)
        {
            try
            {
                if (captionsBlock == null || !captionsBlock.Current.IsEnabled)
                {
                    captionsBlock = TryFindCaptionsBlockWithRetry(liveWindow, retries: 75, delayMs: 200); // ≈15s
                    if (captionsBlock != null)
                    {
                        Console.WriteLine("✅ CaptionsTextBlock re-acquired.");
                        Automation.AddAutomationEventHandler(
                            AutomationElementIdentifiers.LiveRegionChangedEvent,
                            captionsBlock, TreeScope.Element, liveRegionChanged);
                        lastText = ""; stableSentence = ""; sentenceSent = false;
                    }
                    Thread.Sleep(200);
                    continue;
                }

                if (IsTtsActive())
                {
                    Thread.Sleep(50);
                    continue;
                }

                string current = CleanToLastLine(ReadCaptionText(captionsBlock));

                if (!string.IsNullOrEmpty(current))
                {
                    if (current != lastText)
                    {
                        lastChange = DateTime.Now;
                        stableSentence = current;
                        lastText = current;
                        sentenceSent = false;
                    }
                    else
                    {
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
                captionsBlock = null;
            }
            catch (Exception ex)
            {
                Console.WriteLine($"⚠️ Loop error: {ex.Message}");
            }

            Thread.Sleep(120);
        }
    }

    // ===== Bootstrap =====
    private static (AutomationElement? live, AutomationElement? captions) BootstrapLiveCaptions()
    {
        try
        {
            EnsureLiveCaptionsProcess();

            var live = WaitForLiveCaptionsWindow(timeoutMs: 8000);
            if (live == null)
            {
                Console.WriteLine("❌ Live Captions top window not found.");
                return (null, null);
            }
            Console.WriteLine("✅ Live Captions window located.");

            // 聚焦窗口（菜单更稳定）
            try { live.SetFocus(); } catch {}

            if (!EnsureMicrophoneIncluded(live, overallTimeoutMs: 10000))
                Console.WriteLine("⚠️ Failed to enable 'Include microphone audio' (will continue anyway).");
            else
                Console.WriteLine("✅ 'Include microphone audio' enabled.");

            // 给字幕区域挂载更充裕的时间（对齐旧版）
            var captions = TryFindCaptionsBlockWithRetry(live, retries: 75, delayMs: 200); // ≈15s
            return (live, captions);
        }
        catch (Exception ex)
        {
            Console.WriteLine($"❌ Bootstrap error: {ex.Message}");
            return (null, null);
        }
    }

    private static void EnsureLiveCaptionsProcess()
    {
        try
        {
            var procs = Process.GetProcessesByName("LiveCaptions");
            if (procs == null || procs.Length == 0)
            {
                var exe = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "System32", "LiveCaptions.exe");
                Console.WriteLine($"Launching: {exe}");
                Process.Start(new ProcessStartInfo
                {
                    FileName = exe,
                    UseShellExecute = true,
                    WindowStyle = ProcessWindowStyle.Normal
                });
                Thread.Sleep(1500); // 由 800ms 拉长到 1500ms
            }
        }
        catch (Exception ex)
        {
            Console.WriteLine($"⚠️ Start LiveCaptions.exe failed: {ex.Message}");
        }
    }

    private static AutomationElement? WaitForLiveCaptionsWindow(int timeoutMs)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            var root = AutomationElement.RootElement;
            var live = root.FindFirst(TreeScope.Subtree,
                new OrCondition(
                    new PropertyCondition(AutomationElement.ClassNameProperty, "LiveCaptionsDesktopWindow"),
                    new OrCondition(
                        new PropertyCondition(AutomationElement.NameProperty, "Live Captions", PropertyConditionFlags.IgnoreCase),
                        new OrCondition(
                            new PropertyCondition(AutomationElement.NameProperty, "实时字幕"),
                            new PropertyCondition(AutomationElement.NameProperty, "实时辅助字幕")
                        )
                    )
                ));
            if (live != null) return live;
            Thread.Sleep(200);
        }
        return null;
    }

    private static bool EnsureMicrophoneIncluded(AutomationElement live, int overallTimeoutMs)
    {
        // 设置按钮
        var settingsBtn = FindByAutomationIdWithRootFallback(live, "SettingsButton", retries: 20, delayMs: 150);
        if (settingsBtn == null) return false;
        TryInvoke(settingsBtn);
        Thread.Sleep(200);

        // 首选项按钮（在 Root 弹层兜底）
        var prefBtn = FindByAutomationIdWithRootFallback(live, "PreferencesButton", retries: 20, delayMs: 150);
        if (prefBtn == null) return false;

        if (!TryInvoke(prefBtn))
        {
            if (!TryExpand(prefBtn))
                TryInvoke(prefBtn);
        }

        // 等子菜单出现（在 Root 兜底）
        if (!WaitUntil(() => FindByAutomationIdWithRootFallback(live, "MicrophoneMenuFlyoutItem", 1, 0) != null,
                       timeoutMs: 4000, pollMs: 120))
        {
            TryInvoke(prefBtn);
            if (!WaitUntil(() => FindByAutomationIdWithRootFallback(live, "MicrophoneMenuFlyoutItem", 1, 0) != null,
                           timeoutMs: 3000, pollMs: 120))
                return false;
        }

        // 麦克风菜单项
        var micItem = FindByAutomationIdWithRootFallback(live, "MicrophoneMenuFlyoutItem", retries: 15, delayMs: 120);
        if (micItem == null) return false;

        TryScrollIntoView(micItem);

        if (!TryEnsureToggleOn(micItem))
            TryInvoke(micItem);

        var toggled = GetToggleState(micItem);
        if (toggled.HasValue && toggled.Value != ToggleState.On)
        {
            TryInvoke(micItem);
            Thread.Sleep(150);
        }

        return true;
    }

    private static AutomationElement? TryFindCaptionsBlockWithRetry(AutomationElement live, int retries, int delayMs)
    {
        for (int i = 0; i < retries; i++)
        {
            var el = FindCaptionsBlock(live);
            if (el != null) return el;
            Thread.Sleep(delayMs);
        }
        return null;
    }

    private static AutomationElement? FindCaptionsBlock(AutomationElement live)
    {
        try
        {
            return live.FindFirst(
                TreeScope.Descendants,
                new PropertyCondition(AutomationElement.AutomationIdProperty, "CaptionsTextBlock")
            );
        }
        catch { return null; }
    }

    private static void TryHideLiveCaptionsWindow()
    {
        try
        {
            var root = AutomationElement.RootElement;
            if (root == null) return;
            var tops = root.FindAll(TreeScope.Children, Condition.TrueCondition);
            for (int i = 0; i < tops.Count; i++)
            {
                var top = tops[i];
                AutomationElement caption = null;
                try
                {
                    caption = top.FindFirst(
                        TreeScope.Subtree,
                        new PropertyCondition(AutomationElement.AutomationIdProperty, "CaptionsTextBlock")
                    );
                }
                catch {}

                if (caption != null)
                {
                    var hwnd = (IntPtr)top.Current.NativeWindowHandle;
                    if (hwnd != IntPtr.Zero)
                    {
                        ShowWindow(hwnd, SW_HIDE);
                        Console.WriteLine("🙈 Live Captions window hidden.");
                    }
                    return;
                }
            }
            Console.WriteLine("ℹ️ Live Captions window not found via CaptionsTextBlock.");
        }
        catch (Exception ex)
        {
            Console.WriteLine($"⚠️ Hide window failed: {ex.Message}");
        }
    }

    // ===== UIA helpers =====
    private static AutomationElement? FindByAutomationIdWithRootFallback(AutomationElement root, string id, int retries, int delayMs)
    {
        var el = FindByAutomationId(root, id, retries, delayMs);
        if (el != null) return el;
        return FindByAutomationId(AutomationElement.RootElement, id, retries, delayMs);
    }

    private static AutomationElement? FindByAutomationId(AutomationElement root, string id, int retries, int delayMs)
    {
        for (int i = 0; i < retries; i++)
        {
            try
            {
                var el = root.FindFirst(
                    TreeScope.Subtree,
                    new PropertyCondition(AutomationElement.AutomationIdProperty, id)
                );
                if (el != null) return el;
            }
            catch {}
            Thread.Sleep(delayMs);
        }
        return null;
    }

    private static void TryScrollIntoView(AutomationElement el)
    {
        try
        {
            if (el.TryGetCurrentPattern(ScrollItemPattern.Pattern, out var p))
                ((ScrollItemPattern)p).ScrollIntoView();
        }
        catch {}
    }

    private static bool TryInvoke(AutomationElement el)
    {
        try
        {
            if (el.TryGetCurrentPattern(InvokePattern.Pattern, out var p))
            {
                ((InvokePattern)p).Invoke();
                return true;
            }
        }
        catch {}
        return false;
    }

    private static bool TryExpand(AutomationElement el)
    {
        try
        {
            if (el.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out var p))
            {
                var ec = (ExpandCollapsePattern)p;
                if (ec.Current.ExpandCollapseState != ExpandCollapseState.Expanded)
                    ec.Expand();
                return true;
            }
        }
        catch {}
        return false;
    }

    private static bool TryEnsureToggleOn(AutomationElement el)
    {
        try
        {
            if (el.TryGetCurrentPattern(TogglePattern.Pattern, out var p))
            {
                var tp = (TogglePattern)p;
                if (tp.Current.ToggleState != ToggleState.On)
                    tp.Toggle();
                return true;
            }
        }
        catch {}
        return false;
    }

    private static ToggleState? GetToggleState(AutomationElement el)
    {
        try
        {
            if (el.TryGetCurrentPattern(TogglePattern.Pattern, out var p))
                return ((TogglePattern)p).Current.ToggleState;
        }
        catch {}
        return null;
    }

    private static bool WaitUntil(Func<bool> predicate, int timeoutMs, int pollMs)
    {
        var sw = Stopwatch.StartNew();
        while (sw.ElapsedMilliseconds < timeoutMs)
        {
            if (predicate()) return true;
            Thread.Sleep(pollMs);
        }
        return false;
    }

    // ===== Text & gating =====
    static bool IsSentenceEnd(string text)
    {
        if (string.IsNullOrEmpty(text)) return false;
        char last = text[^1];
        return ".!?。？！".Contains(last);
    }

    static void SendSentence(string sentence)
    {
        sentence = CleanToLastLine(sentence);
        if (string.IsNullOrEmpty(sentence)) return;
        if (IsTtsActive()) return;
        if (IsLikelyTtsEcho(sentence)) return;
        if (TryHandleListeningCommand(sentence)) return;
        if (!listeningEnabled) return;

        if (sentence == lastPublished && (DateTime.UtcNow - lastPublishedAt).TotalSeconds < 3) return;
        lastPublished = sentence;
        lastPublishedAt = DateTime.UtcNow;

        Console.WriteLine($"🗣 {sentence}");

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

                _ = Task.Run(async () =>
                {
                    try { await SimpleMqttPublisher.PublishOnceAsync(mqttConfig, payload, CancellationToken.None).ConfigureAwait(false); }
                    catch (Exception ex) { Console.WriteLine($"[MQTT] publish failed: {ex.Message}"); }
                });
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[MQTT] build payload failed: {ex.Message}");
            }
        }
    }

    static string ReadCaptionText(AutomationElement element)
    {
        try
        {
            if (element.TryGetCurrentPattern(TextPattern.Pattern, out var patternObj))
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

    static void OnTtsStateMessageInternal(bool speaking, string text)
    {
        ttsSpeaking = speaking;
        ttsLastUpdateUtc = DateTime.UtcNow;
        if (speaking)
            ttsLastTextLower = (text ?? string.Empty).Trim().ToLowerInvariant();
        else if (!string.IsNullOrEmpty(text))
            ttsLastTextLower = text.Trim().ToLowerInvariant();
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
        int common = LongestCommonSubsequenceLength(s, ttsLastTextLower);
        return common * 10 >= s.Length * 6;
    }

    static int LongestCommonSubsequenceLength(string a, string b)
    {
        int n = a.Length, m = b.Length;
        int[,] dp = new int[n + 1, m + 1];
        for (int i = 1; i <= n; i++)
        for (int j = 1; j <= m; j++)
            dp[i, j] = (a[i - 1] == b[j - 1]) ? dp[i - 1, j - 1] + 1 : Math.Max(dp[i - 1, j], dp[i, j - 1]);
        return dp[n, m];
    }

    static bool TryHandleListeningCommand(string sentence)
    {
        string norm = (sentence ?? string.Empty).Trim().ToLowerInvariant();
        if (norm.Length == 0) return false;
        while (norm.Length > 0 && ".!?。？！".IndexOf(norm[^1]) >= 0)
            norm = norm[..^1].TrimEnd();

        bool matched = false;
        if (norm.Contains("start listening"))
        {
            listeningEnabled = true; matched = true; Console.WriteLine("[Gate] Listening enabled");
        }
        else if (norm.Contains("stop listening"))
        {
            listeningEnabled = false; matched = true; Console.WriteLine("[Gate] Listening disabled");
        }
        return matched;
    }

    // ===== MQTT helpers =====
    sealed class MqttConfig
    {
        private MqttConfig()
        {
            Enabled = false; Host = "127.0.0.1"; Port = 1883; Topic = "";
            ClientId = $"live-captions-{Environment.MachineName}";
            SourceLabel = "live_captions";
            TtsStateTopic = "robot/tts/state";
        }
        private MqttConfig(string host, int port, string topic, string clientId, string source, string ttsStateTopic)
        {
            Host = host; Port = port; Topic = topic; ClientId = clientId; SourceLabel = source; TtsStateTopic = ttsStateTopic;
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
            Console.WriteLine($"[MQTT] published: {config.Topic} ({payload.Length}B)");
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
            bool retain = (Environment.GetEnvironmentVariable("LIVE_CAPTIONS_MQTT_RETAIN") ?? "0") is string r &&
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

    static class MqttTtsSubscriber
    {
        public static async Task RunAsync(MqttConfig config, Action<bool, string> onTtsState)
        {
            for (;;)
            {
                try
                {
                    using var client = new TcpClient();
                    await client.ConnectAsync(config.Host, config.Port);
                    using var stream = client.GetStream();

                    byte[] connect = BuildConnectPacketLocal(config);
                    await stream.WriteAsync(connect, 0, connect.Length);
                    await stream.FlushAsync();
                    await ReadConnAckAsyncLocal(stream);

                    byte[] subscribePacket = BuildSubscribePacket(config.TtsStateTopic, packetId: 1);
                    await stream.WriteAsync(subscribePacket, 0, subscribePacket.Length);
                    await stream.FlushAsync();

                    var buffer = new byte[8192];
                    while (true)
                    {
                        int header = stream.ReadByte();
                        if (header < 0) break;
                        byte first = (byte)header;
                        int remaining = ReadRemainingLength(stream);
                        if (remaining <= 0 || remaining > buffer.Length)
                        {
                            SkipBytes(stream, remaining);
                            continue;
                        }
                        int read = ReadExact(stream, buffer, remaining);
                        if (read != remaining) break;

                        byte packetType = (byte)(first >> 4);
                        if (packetType == 3)
                        {
                            int idx = 0;
                            int topicLen = (buffer[idx] << 8) | buffer[idx + 1]; idx += 2;
                            string topic = Encoding.UTF8.GetString(buffer, idx, topicLen); idx += topicLen;
                            int payloadLen = remaining - idx;
                            if (payloadLen > 0)
                            {
                                string json = Encoding.UTF8.GetString(buffer, idx, payloadLen);
                                try
                                {
                                    using var doc = JsonDocument.Parse(json);
                                    if (doc.RootElement.TryGetProperty("speaking", out var speakingProp))
                                    {
                                        bool speaking = speakingProp.ValueKind == JsonValueKind.True ||
                                                        (speakingProp.ValueKind == JsonValueKind.Number && speakingProp.GetInt32() != 0);
                                        string ttsText = null;
                                        if (doc.RootElement.TryGetProperty("text", out var textProp) && textProp.ValueKind == JsonValueKind.String)
                                            ttsText = textProp.GetString();
                                        onTtsState?.Invoke(speaking, ttsText);
                                    }
                                }
                                catch {}
                            }
                        }
                    }
                }
                catch
                {
                    Thread.Sleep(500);
                }
            }
        }

        static int ReadRemainingLength(Stream s)
        {
            int multiplier = 1, value = 0;
            while (true)
            {
                int digit = s.ReadByte(); if (digit < 0) return -1;
                value += (digit & 127) * multiplier;
                if ((digit & 128) == 0) break;
                multiplier *= 128; if (multiplier > 128 * 128 * 128) return -1;
            }
            return value;
        }

        static int ReadExact(Stream s, byte[] buffer, int len)
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

        static void SkipBytes(Stream s, int len)
        {
            var tmp = new byte[1024]; int remaining = len;
            while (remaining > 0)
            {
                int n = s.Read(tmp, 0, Math.Min(remaining, tmp.Length));
                if (n <= 0) break;
                remaining -= n;
            }
        }

        static byte[] BuildSubscribePacket(string topic, ushort packetId)
        {
            byte[] topicBytes = Encoding.UTF8.GetBytes(topic);
            int remain = 2 + 2 + topicBytes.Length + 1;
            var header = BuildFixedHeaderLocal(0x82, remain);
            var packet = new byte[header.Length + remain];
            Buffer.BlockCopy(header, 0, packet, 0, header.Length);
            int offset = header.Length;
            packet[offset++] = (byte)((packetId >> 8) & 0xFF);
            packet[offset++] = (byte)(packetId & 0xFF);
            packet[offset++] = (byte)((topicBytes.Length >> 8) & 0xFF);
            packet[offset++] = (byte)(topicBytes.Length & 0xFF);
            Buffer.BlockCopy(topicBytes, 0, packet, offset, topicBytes.Length);
            offset += topicBytes.Length;
            packet[offset++] = 0x00; // QoS 0
            return packet;
        }

        static byte[] BuildFixedHeaderLocal(byte type, int len)
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

        static byte[] BuildConnectPacketLocal(MqttConfig c)
        {
            byte[] clientId = Encoding.UTF8.GetBytes(c.ClientId);
            const int header = 10;
            int remain = header + 2 + clientId.Length;
            byte[] fixedHeader = BuildFixedHeaderLocal(0x10, remain);
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

        static async Task ReadConnAckAsyncLocal(Stream s)
        {
            byte[] b = new byte[4];
            int read = 0;
            while (read < 4)
            {
                int n = await s.ReadAsync(b, read, 4 - read);
                if (n <= 0) throw new IOException("MQTT connack read failed");
                read += n;
            }
            if (b[0] != 0x20 || b[3] != 0x00)
                throw new IOException("MQTT broker rejected connection");
        }
    }
}
