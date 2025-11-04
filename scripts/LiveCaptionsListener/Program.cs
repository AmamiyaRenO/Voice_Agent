using System;
using System.Collections.Generic;
using System.Threading;
using System.Windows.Automation;

class Program
{
    static void Main()
    {
        Console.OutputEncoding = System.Text.Encoding.UTF8;
        Console.WriteLine("🔍 正在查找 Live Captions 窗口…");

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

        AutomationElement captionElement = null;
        AutomationEventHandler textChangedHandler = null;
        AutomationPropertyChangedEventHandler valueChangedHandler = null;
        string lastText = string.Empty;

        void RemoveHandlers()
        {
            if (captionElement == null) return;

            if (textChangedHandler != null)
            {
                Automation.RemoveAutomationEventHandler(TextPattern.TextChangedEvent, captionElement, textChangedHandler);
                textChangedHandler = null;
            }

            if (valueChangedHandler != null)
            {
                Automation.RemoveAutomationPropertyChangedEventHandler(captionElement, valueChangedHandler);
                valueChangedHandler = null;
            }
        }

        void OutputCaption(string caption)
        {
            caption = caption?.Trim();
            if (!string.IsNullOrEmpty(caption) && caption != lastText)
            {
                Console.WriteLine($"[字幕] {caption}");
                lastText = caption;
            }
        }

        string TryReadText(AutomationElement element)
        {
            if (element == null)
                return string.Empty;

            try
            {
                if (element.TryGetCurrentPattern(TextPattern.Pattern, out var textObj) && textObj is TextPattern textPattern)
                {
                    return textPattern.DocumentRange.GetText(-1);
                }

                if (element.TryGetCurrentPattern(ValuePattern.Pattern, out var valueObj) && valueObj is ValuePattern valuePattern)
                {
                    return valuePattern.Current.Value;
                }
            }
            catch (ElementNotAvailableException)
            {
                return string.Empty;
            }
            catch (InvalidOperationException)
            {
                return string.Empty;
            }

            return string.Empty;
        }

        AutomationElement FindCaptionElement()
        {
            try
            {
                var queue = new Queue<AutomationElement>();
                queue.Enqueue(live);

                while (queue.Count > 0)
                {
                    var current = queue.Dequeue();
                    if (current == null) continue;

                    try
                    {
                        if (current.TryGetCurrentPattern(TextPattern.Pattern, out _)
                            || current.TryGetCurrentPattern(ValuePattern.Pattern, out _))
                        {
                            var text = TryReadText(current);
                            if (!string.IsNullOrWhiteSpace(text))
                            {
                                return current;
                            }
                        }
                    }
                    catch (ElementNotAvailableException)
                    {
                        continue;
                    }

                    AutomationElementCollection children;
                    try
                    {
                        children = current.FindAll(TreeScope.Children, Condition.TrueCondition);
                    }
                    catch (ElementNotAvailableException)
                    {
                        continue;
                    }

                    foreach (AutomationElement child in children)
                    {
                        queue.Enqueue(child);
                    }
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"⚠️ 搜索字幕控件失败: {ex.Message}");
            }

            return null;
        }

        void AttachToElement(AutomationElement element)
        {
            RemoveHandlers();
            captionElement = element;

            if (captionElement == null)
            {
                return;
            }

            bool hasTextPattern = captionElement.TryGetCurrentPattern(TextPattern.Pattern, out var textPatternObj);
            bool hasValuePattern = captionElement.TryGetCurrentPattern(ValuePattern.Pattern, out var valuePatternObj);

            if (hasTextPattern && textPatternObj is TextPattern textPattern)
            {
                textChangedHandler = (sender, e) =>
                {
                    try
                    {
                        var source = sender as AutomationElement ?? captionElement;
                        if (source == null) return;
                        if (!source.TryGetCurrentPattern(TextPattern.Pattern, out var patternObj) || patternObj is not TextPattern pattern)
                            return;

                        string caption = pattern.DocumentRange.GetText(-1);
                        OutputCaption(caption);
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"⚠️ 文本读取失败: {ex.Message}");
                    }
                };

                Automation.AddAutomationEventHandler(TextPattern.TextChangedEvent, captionElement, TreeScope.Element, textChangedHandler);

                try
                {
                    OutputCaption(textPattern.DocumentRange.GetText(-1));
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"⚠️ 文本读取失败: {ex.Message}");
                }
            }
            else if (hasValuePattern && valuePatternObj is ValuePattern valuePattern)
            {
                valueChangedHandler = (sender, e) =>
                {
                    if (e.Property != ValuePattern.ValueProperty)
                        return;

                    try
                    {
                        var source = sender as AutomationElement ?? captionElement;
                        if (source == null) return;
                        if (!source.TryGetCurrentPattern(ValuePattern.Pattern, out var patternObj) || patternObj is not ValuePattern vp)
                            return;

                        OutputCaption(vp.Current.Value);
                    }
                    catch (Exception ex)
                    {
                        Console.WriteLine($"⚠️ 文本读取失败: {ex.Message}");
                    }
                };

                Automation.AddAutomationPropertyChangedEventHandler(captionElement, TreeScope.Element, valueChangedHandler, ValuePattern.ValueProperty);

                try
                {
                    OutputCaption(valuePattern.Current.Value);
                }
                catch (Exception ex)
                {
                    Console.WriteLine($"⚠️ 文本读取失败: {ex.Message}");
                }
            }
            else
            {
                Console.WriteLine("⚠️ 找到字幕控件但不支持 TextPattern 或 ValuePattern。");
            }
        }

        var initialElement = FindCaptionElement();
        if (initialElement == null)
        {
            Console.WriteLine("⚠️ 未找到字幕文本控件，等待窗口更新…");
        }
        else
        {
            Console.WriteLine("✅ 找到字幕文本控件！");
            AttachToElement(initialElement);
        }

        var structureHandler = new StructureChangedEventHandler((sender, e) =>
        {
            var updatedElement = FindCaptionElement();
            if (updatedElement != null && !updatedElement.Equals(captionElement))
            {
                Console.WriteLine("🔄 检测到字幕控件变化，重新绑定。");
                AttachToElement(updatedElement);
            }
        });

        Automation.AddStructureChangedEventHandler(live, TreeScope.Subtree, structureHandler);

        while (true) Thread.Sleep(100);
    }
}
