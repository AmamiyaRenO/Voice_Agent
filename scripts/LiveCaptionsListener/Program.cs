using System;
using System.Linq;
using System.Threading;
using System.Windows.Automation;

class Program
{
    static void Main()
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        Console.WriteLine("🔍 正在查找 Live Captions 窗口…");

        // 在整个桌面自动化树中寻找“Live Captions”窗口
        var root = AutomationElement.RootElement;
        var live = root.FindFirst(TreeScope.Subtree,
            new OrCondition(
                new PropertyCondition(AutomationElement.NameProperty, "Live Captions"),
                new PropertyCondition(AutomationElement.NameProperty, "实时字幕")
            ));

        if (live == null)
        {
            Console.WriteLine("❌ 未找到 Live Captions，请先按 Win + Ctrl + L 开启。");
            return;
        }

        Console.WriteLine("✅ 找到字幕窗口！");
        Console.WriteLine("📡 正在监听字幕变化…");

        // 寻找内部的文本元素
        var textElement = live.FindFirst(TreeScope.Descendants,
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Text));

        if (textElement == null)
        {
            Console.WriteLine("⚠️ 未找到文本控件。请确保字幕窗口显示中。");
            return;
        }

        var textPattern = textElement.GetCurrentPattern(TextPattern.Pattern) as TextPattern;
        string lastText = "";

        // 订阅 TextChanged 事件
        Automation.AddAutomationEventHandler(
            TextPattern.TextChangedEvent,
            textElement,
            TreeScope.Element,
            (sender, e) =>
            {
                try
                {
                    var range = textPattern.DocumentRange;
                    string caption = range.GetText(-1).Trim();

                    if (!string.IsNullOrEmpty(caption) && caption != lastText)
                    {
                        Console.WriteLine($"[字幕] {caption}");
                        lastText = caption;
                    }
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"⚠️ 错误: {ex.Message}");
                }
            });

        // 保持主线程运行
        while (true) Thread.Sleep(100);
    }
}
