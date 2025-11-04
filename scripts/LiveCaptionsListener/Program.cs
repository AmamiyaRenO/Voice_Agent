using System;
using System.Text;
using System.Threading;
using System.Windows.Automation;

class Program
{
    [STAThread]
    static void Main()
    {
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

        // ⚙️ 这里可以发布到 MQTT
        // var payload = Encoding.UTF8.GetBytes(sentence);
        // mqttClient.Publish("captions/output", payload);
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
}
