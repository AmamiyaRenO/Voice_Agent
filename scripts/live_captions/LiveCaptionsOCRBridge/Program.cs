using System;
using System.Diagnostics;
using System.Drawing;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
using Windows.Media.Ocr;
using Windows.Graphics.Imaging;

class Program
{
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);
    [DllImport("user32.dll")] static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
    delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }

    static IntPtr FindLiveCaptionsWindow()
    {
        IntPtr result = IntPtr.Zero;
        EnumWindows((hWnd, lParam) =>
        {
            if (!IsWindowVisible(hWnd)) return true;
            StringBuilder sb = new(256);
            GetWindowText(hWnd, sb, sb.Capacity);
            string title = sb.ToString();
            if (title.Contains("Live Captions", StringComparison.OrdinalIgnoreCase) ||
                title.Contains("实时字幕") || title.Contains("字幕") || title.Contains("Captions"))
            {
                result = hWnd;
                return false;
            }
            return true;
        }, IntPtr.Zero);
        return result;
    }

    static async Task Main()
    {
        Console.WriteLine("🔍 正在查找 Live Captions 窗口...");
        IntPtr hwnd = FindLiveCaptionsWindow();
        if (hwnd == IntPtr.Zero)
        {
            Console.WriteLine("❌ 未找到字幕窗口，请确保已打开 (Win + Ctrl + L)。");
            return;
        }

        GetWindowRect(hwnd, out RECT rect);
        int width = rect.Right - rect.Left;
        int height = rect.Bottom - rect.Top;
        Console.WriteLine($"✅ 找到字幕窗口，尺寸: {width}×{height}");

        var ocr = OcrEngine.TryCreateFromLanguage(new Windows.Globalization.Language("en"));
        Console.WriteLine("📡 OCR 引擎初始化成功，开始捕获字幕…");

        string lastText = "";
        string stableText = "";
        int stableCount = 0;
        const int stableThreshold = 5;   // 连续 5 帧稳定 ≈ 0.6 秒

        while (true)
        {
            try
            {
                using var bmp = new Bitmap(width, height);
                using (var g = Graphics.FromImage(bmp))
                    g.CopyFromScreen(rect.Left, rect.Top, 0, 0, new Size(width, height));

                var ms = new System.IO.MemoryStream();
                bmp.Save(ms, System.Drawing.Imaging.ImageFormat.Bmp);
                ms.Seek(0, System.IO.SeekOrigin.Begin);

                var decoder = await BitmapDecoder.CreateAsync(ms.AsRandomAccessStream());
                var bitmap = await decoder.GetSoftwareBitmapAsync();
                var result = await ocr.RecognizeAsync(bitmap);
                string text = result.Text.Trim();

                if (!string.IsNullOrEmpty(text))
                {
                    text = text.Replace("\n", " ").Trim();

                    if (text == lastText)
                    {
                        stableCount++;
                        if (stableCount == stableThreshold && !string.IsNullOrEmpty(stableText))
                        {
                            Console.WriteLine($"[字幕] {stableText}");
                            stableText = "";
                        }
                    }
                    else
                    {
                        stableText = text;
                        stableCount = 0;
                    }
                    lastText = text;
                }
                else
                {
                    // 清空时立即输出残余句
                    if (!string.IsNullOrEmpty(stableText))
                    {
                        Console.WriteLine($"[字幕] {stableText}");
                        stableText = "";
                    }
                    stableCount = 0;
                    lastText = "";
                }
            }
            catch (Exception ex)
            {
                Console.WriteLine($"⚠️ OCR 错误: {ex.Message}");
            }

            await Task.Delay(120); // 约 8 次/秒
        }
    }
}
