Get-Process | Where-Object { $_.MainWindowTitle -like "*实时辅助字幕*" } |
ForEach-Object {
  $hwnd = $_.MainWindowHandle
  $sig = '[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
  $type = Add-Type -MemberDefinition $sig -Name Win32ShowWindow -Namespace Win32 -PassThru
  $type::ShowWindow($hwnd, 0)
}
