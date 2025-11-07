@echo off
setlocal
set "TMPPS1=%TEMP%\_hide_captions.ps1"

> "%TMPPS1%" (
  echo $patterns = @('^*实时辅助字幕*','^*实时字幕*','^*Live captions*')
  echo $targets = Get-Process ^| Where-Object {
  echo ^    $t = $_.MainWindowTitle
  echo ^    if ([string]::IsNullOrWhiteSpace($t)) { return $false }
  echo ^    foreach ($pat in $patterns) { if ($t -like $pat) { return $true } }
  echo ^    return $false
  echo }
  echo if (-not $targets) { return }
  echo $sig = '[DllImport("user32.dll", SetLastError=true)] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);'
  echo $type = Add-Type -MemberDefinition $sig -Name Win32ShowWindow -Namespace Win32 -PassThru
  echo foreach ($p in $targets) { [void]$type::ShowWindow($p.MainWindowHandle, 0) }
)

chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%TMPPS1%"
del "%TMPPS1%" >nul 2>&1
endlocal
