using System;
using System.Linq;
using System.Threading;
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
        private static string lastText = string.Empty;
        private static string sentenceBuffer = string.Empty;
        private static DateTime lastUpdate = DateTime.Now;
        private static Timer? silenceTimer;

        private static void Main()
        {
            Console.Title = "Live Captions Listener";
            Console.WriteLine("等待 Live Captions 窗口...");

            AutomationElement? captionElement = WaitForLiveCaptions();
            if (captionElement == null)
            {
                Console.WriteLine("未找到 Live Captions 窗口，请先按 Win+Ctrl+L 打开。");
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

            Console.ReadLine();
            Automation.RemoveAllEventHandlers();
            silenceTimer?.Dispose();
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
                // Replace this with MQTT/Named Pipe/WebSocket publishing to Unity as needed.
            }

            sentenceBuffer = string.Empty;
        }
    }
}
