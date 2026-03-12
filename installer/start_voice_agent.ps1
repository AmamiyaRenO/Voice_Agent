param(
    [switch]$ForceSetup,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$AppRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptsDir = Join-Path $AppRoot "scripts"
$AppDir = Join-Path $AppRoot "app"
$RuntimeServicesDir = Join-Path $AppRoot "runtime\services"
$LiveCaptionsRuntimeDir = Join-Path $AppRoot "runtime\live_captions"
$LiveCaptionsListenerExe = Join-Path $LiveCaptionsRuntimeDir "EnableLcMic.exe"
$StateDir = Join-Path $env:LOCALAPPDATA "VoiceAgent"
$DefaultConfigPath = Join-Path $ScriptsDir "local_services.default.json"
$UserConfigPath = Join-Path $StateDir "local_services.user.json"
$UserConfigSamplePath = Join-Path $ScriptsDir "local_services.user.sample.json"
$InstalledUserConfigTemplatePath = Join-Path $ScriptsDir "local_services.user.json"
$InstalledManifestPath = Join-Path $ScriptsDir "intent_service\manifest.json"
$UserManifestPath = Join-Path $StateDir "manifest.json"
$ServiceLauncherExe = Join-Path $RuntimeServicesDir "service_launcher.exe"
$PanelHealthUrl = "http://127.0.0.1:8787/healthz"
$PanelSetupUrl = "http://127.0.0.1:8787/setup.html"
$PanelIndexUrl = "http://127.0.0.1:8787/index.html"
$VoiceHealthUrl = "http://127.0.0.1:8000/healthz"
$OllamaTagsUrl = "http://127.0.0.1:11434/api/tags"

function Normalize-PathForCompare {
    param([string]$PathValue)
    if (-not $PathValue) {
        return ""
    }
    try {
        return ([System.IO.Path]::GetFullPath($PathValue)).TrimEnd('\').ToLowerInvariant()
    }
    catch {
        return ($PathValue.Trim()).TrimEnd('\').ToLowerInvariant()
    }
}

function Test-ManifestPathNeedsMigration {
    param(
        [string]$CurrentPath,
        [string[]]$InstalledCandidates
    )

    $text = "$CurrentPath".Trim()
    if (-not $text) {
        return $true
    }

    if (-not [System.IO.Path]::IsPathRooted($text)) {
        return $true
    }

    $normalized = Normalize-PathForCompare $text
    foreach ($candidate in $InstalledCandidates) {
        if ($normalized -eq $candidate) {
            return $true
        }
    }
    return $false
}

function Initialize-UserWritableConfig {
    if (-not (Test-Path $StateDir)) {
        New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    }

    if (-not (Test-Path $UserManifestPath)) {
        if (Test-Path $InstalledManifestPath) {
            Copy-Item -Path $InstalledManifestPath -Destination $UserManifestPath -Force
        }
        else {
            Set-Content -Path $UserManifestPath -Encoding UTF8 -Value "{`"games`":[]}"
        }
    }

    if (-not (Test-Path $UserConfigPath)) {
        if (Test-Path $UserConfigSamplePath) {
            Copy-Item -Path $UserConfigSamplePath -Destination $UserConfigPath -Force
        }
        elseif (Test-Path $InstalledUserConfigTemplatePath) {
            Copy-Item -Path $InstalledUserConfigTemplatePath -Destination $UserConfigPath -Force
        }
        else {
            Set-Content -Path $UserConfigPath -Encoding UTF8 -Value "{}"
        }
    }

    $config = $null
    try {
        $raw = Get-Content -Raw $UserConfigPath
        if (-not [string]::IsNullOrWhiteSpace($raw)) {
            $config = $raw | ConvertFrom-Json
        }
    }
    catch {
    }
    if (-not $config) {
        $config = [pscustomobject]@{}
    }
    if (-not ($config.PSObject.Properties.Name -contains "paths")) {
        Add-Member -InputObject $config -MemberType NoteProperty -Name paths -Value ([pscustomobject]@{})
    }
    if (-not $config.paths) {
        $config.paths = [pscustomobject]@{}
    }
    if (-not ($config.PSObject.Properties.Name -contains "env")) {
        Add-Member -InputObject $config -MemberType NoteProperty -Name env -Value ([pscustomobject]@{})
    }
    if (-not $config.env) {
        $config.env = [pscustomobject]@{}
    }

    $installedCandidates = @(
        Normalize-PathForCompare $InstalledManifestPath,
        Normalize-PathForCompare (Join-Path $AppRoot "app\scripts\intent_service\manifest.json")
    ) | Where-Object { $_ }

    $changed = $false
    $intentPath = "$($config.paths.intent_manifest)".Trim()
    $gamePath = "$($config.paths.game_manifest)".Trim()
    if (Test-ManifestPathNeedsMigration -CurrentPath $intentPath -InstalledCandidates $installedCandidates) {
        $config.paths.intent_manifest = $UserManifestPath
        $changed = $true
    }
    if (Test-ManifestPathNeedsMigration -CurrentPath $gamePath -InstalledCandidates $installedCandidates) {
        $config.paths.game_manifest = $UserManifestPath
        $changed = $true
    }

    if ("$($config.env.VOICE_CONVERSATION_PROFILE)".Trim() -ne "local") {
        Add-Member -InputObject $config.env -MemberType NoteProperty -Name VOICE_CONVERSATION_PROFILE -Value "local" -Force
        $changed = $true
    }
    if ("$($config.env.VOICE_LOCAL_STREAMING_ASR_MODE)".Trim() -ne "live-captions") {
        Add-Member -InputObject $config.env -MemberType NoteProperty -Name VOICE_LOCAL_STREAMING_ASR_MODE -Value "live-captions" -Force
        $changed = $true
    }
    if ("$($config.env.VOICE_CLOUD_STREAMING_ASR_MODE)".Trim() -ne "live-captions") {
        Add-Member -InputObject $config.env -MemberType NoteProperty -Name VOICE_CLOUD_STREAMING_ASR_MODE -Value "live-captions" -Force
        $changed = $true
    }

    if ($changed) {
        $config | ConvertTo-Json -Depth 20 | Set-Content -Path $UserConfigPath -Encoding UTF8
    }
}

function Test-TcpPort {
    param(
        [string]$HostName = "127.0.0.1",
        [int]$Port,
        [int]$TimeoutMs = 500
    )
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
            $client.Close()
            return $false
        }
        $client.EndConnect($iar)
        $client.Close()
        return $true
    }
    catch {
        return $false
    }
}

function Clear-LauncherOverrides {
    $keys = @(
        "VOICE_AGENT_HUB_CMD",
        "VOICE_AGENT_HUB_CWD",
        "VOICE_AGENT_ORCH_CMD",
        "VOICE_AGENT_ORCH_CWD",
        "VOICE_AGENT_VOICE_CMD",
        "VOICE_AGENT_VOICE_CWD",
        "VOICE_AGENT_PIPER_HTTP_CMD",
        "VOICE_AGENT_PIPER_HTTP_CWD",
        "VOICE_AGENT_QWEN_HTTP_CMD",
        "VOICE_AGENT_QWEN_HTTP_CWD",
        "VOICE_AGENT_INTENT_CMD",
        "VOICE_AGENT_INTENT_CWD",
        "VOICE_AGENT_DIALOG_CMD",
        "VOICE_AGENT_DIALOG_CWD",
        "VOICE_AGENT_LAUNCHER_CMD",
        "VOICE_AGENT_LAUNCHER_CWD",
        "VOICE_AGENT_TELEMETRY_CMD",
        "VOICE_AGENT_TELEMETRY_CWD",
        "VOICE_AGENT_ASR_PYTHON",
        "VOICE_AGENT_TTS_PYTHON"
    )

    foreach ($k in $keys) {
        if (Test-Path "Env:$k") {
            Remove-Item "Env:$k" -ErrorAction SilentlyContinue
        }
    }
}

function Clear-ExistingServiceProcesses {
    Write-Host "[oneclick] clearing existing service processes..."

    $imageNames = @(
        "service_launcher",
        "voice_service",
        "piper_http",
        "qwen_tts_http",
        "intent_service",
        "dialog_service",
        "telemetry_service",
        "game_launcher"
    )

    foreach ($name in $imageNames) {
        try {
            Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        }
        catch {
        }
    }

    $targets = @(
        "start_local_services.py",
        "intent_service\main.py",
        "dialog_service\main.py",
        "game_launcher\main.py",
        "telemetry_service\main.py",
        "python_voice_service\main.py",
        "python_voice_service\piper_http.py",
        "python_voice_service\qwen_tts_http.py"
    )

    $root = Normalize-PathForCompare $AppRoot
    if (-not $root) {
        return
    }

    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -and $_.Name -match '^(python|py)(\.exe)?$' } |
            ForEach-Object {
                $cmd = "$($_.CommandLine)".ToLowerInvariant()
                if (-not $cmd.Contains($root)) {
                    return
                }
                foreach ($target in $targets) {
                    if ($cmd.Contains($target.ToLowerInvariant())) {
                        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                        break
                    }
                }
            }
    }
    catch {
    }

    Start-Sleep -Milliseconds 800
}

function Resolve-MosquittoAvailable {
    if (Get-Command mosquitto -ErrorAction SilentlyContinue) {
        return $true
    }
    if ($env:VOICE_AGENT_MOSQUITTO_EXE -and (Test-Path $env:VOICE_AGENT_MOSQUITTO_EXE)) {
        return $true
    }
    $candidates = @(
        "C:\Program Files\mosquitto\mosquitto.exe",
        "C:\Program Files (x86)\mosquitto\mosquitto.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $true
        }
    }
    return $false
}

function Wait-HttpOk {
    param(
        [string]$Url,
        [int]$TimeoutSec = 45,
        [int]$IntervalMs = 800
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 -Method GET
            if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300) {
                return $true
            }
        }
        catch {
        }
        Start-Sleep -Milliseconds $IntervalMs
    }
    return $false
}

function Resolve-BundledPiperModelPath {
    param([string]$PiperRoot)
    if (-not (Test-Path $PiperRoot)) {
        return ""
    }

    $modelsDir = Join-Path $PiperRoot "models"
    if (-not (Test-Path $modelsDir)) {
        return ""
    }

    $preferred = @(
        "en_US-lessac-medium.onnx",
        "en_US-amy-medium.onnx",
        "en_US-ryan-high.onnx"
    )
    foreach ($name in $preferred) {
        $candidate = Join-Path $modelsDir $name
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    $first = Get-ChildItem -Path $modelsDir -Filter *.onnx -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($first) {
        return $first.FullName
    }
    return ""
}

function Apply-BundledPiperEnvironment {
    $piperRoot = Join-Path $AppRoot "runtime\\piper"
    $bundledExe = Join-Path $piperRoot "piper.exe"
    $resolvedExe = ""
    if (Test-Path $bundledExe) {
        $resolvedExe = (Resolve-Path $bundledExe).Path
    }

    $resolvedModel = Resolve-BundledPiperModelPath -PiperRoot $piperRoot

    $envExe = "$($env:PIPER_EXECUTABLE)".Trim()
    if ($envExe -and (Test-Path $envExe)) {
        $resolvedExe = (Resolve-Path $envExe).Path
    }
    elseif ($resolvedExe) {
        $env:PIPER_EXECUTABLE = $resolvedExe
    }

    $envModel = "$($env:PIPER_MODEL_PATH)".Trim()
    if ($envModel -and (Test-Path $envModel)) {
        $resolvedModel = (Resolve-Path $envModel).Path
    }
    elseif ($resolvedModel) {
        $env:PIPER_MODEL_PATH = $resolvedModel
    }

    if (-not ("$($env:OLLAMA_MODEL)".Trim())) {
        $env:OLLAMA_MODEL = "qwen3.5:0.8b"
    }

    if (-not ("$($env:OLLAMA_THINK)".Trim())) {
        $env:OLLAMA_THINK = "0"
    }

    if (-not ("$($env:OLLAMA_TEMPERATURE)".Trim())) {
        $env:OLLAMA_TEMPERATURE = "0.7"
    }

    if (-not ("$($env:OLLAMA_TOP_P)".Trim())) {
        $env:OLLAMA_TOP_P = "0.8"
    }

    if (-not ("$($env:OLLAMA_TOP_K)".Trim())) {
        $env:OLLAMA_TOP_K = "20"
    }

    if (-not ("$($env:DIALOG_ENABLE_CONTEXT_MEMORY)".Trim())) {
        $env:DIALOG_ENABLE_CONTEXT_MEMORY = "1"
    }

    if (-not ("$($env:DIALOG_ENABLE_POLICY)".Trim())) {
        $env:DIALOG_ENABLE_POLICY = "1"
    }

    if (-not ("$($env:DIALOG_HISTORY_TURNS)".Trim())) {
        $env:DIALOG_HISTORY_TURNS = "8"
    }

    if (-not ("$($env:DIALOG_SUMMARY_MAX_CHARS)".Trim())) {
        $env:DIALOG_SUMMARY_MAX_CHARS = "420"
    }

    if (-not ("$($env:DIALOG_CONTEXT_MAX_CHARS)".Trim())) {
        $env:DIALOG_CONTEXT_MAX_CHARS = "900"
    }

    if (-not ("$($env:PIPER_CONFIG_PATH)".Trim()) -and $resolvedModel) {
        $cfg1 = "$resolvedModel.json"
        $cfg2 = [System.IO.Path]::ChangeExtension($resolvedModel, ".onnx.json")
        if (Test-Path $cfg1) {
            $env:PIPER_CONFIG_PATH = (Resolve-Path $cfg1).Path
        }
        elseif (Test-Path $cfg2) {
            $env:PIPER_CONFIG_PATH = (Resolve-Path $cfg2).Path
        }
    }

    if (-not ("$($env:PIPER_EXECUTABLE)".Trim()) -or -not ("$($env:PIPER_MODEL_PATH)".Trim())) {
        Write-Warning "[oneclick] bundled Piper not fully configured (missing executable or model). TTS may fail until configured."
        return
    }

    Write-Host "[oneclick] Piper runtime ready."
}

function Test-OllamaReachable {
    try {
        $resp = Invoke-WebRequest -Uri $OllamaTagsUrl -UseBasicParsing -TimeoutSec 2 -Method GET
        return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300)
    }
    catch {
        return $false
    }
}

function Resolve-UnityExe {
    param([string]$Folder)
    if (-not (Test-Path $Folder)) {
        return $null
    }
    $candidates = Get-ChildItem -Path $Folder -Filter *.exe -File | Where-Object {
        $_.Name -notin @("UnityCrashHandler64.exe", "UnityCrashHandler32.exe")
    }
    if (-not $candidates) {
        return $null
    }
    return $candidates[0].FullName
}

function Start-ServiceStack {
    Clear-LauncherOverrides
    $env:VOICE_AGENT_AUTO_BOOTSTRAP_VENV = "0"
    Clear-ExistingServiceProcesses

    $launcherArgs = @("--no-wait")
    $qwenExe = Join-Path $RuntimeServicesDir "qwen_tts_http.exe"
    if (-not (Test-Path $qwenExe)) {
        # Avoid fallback to source-mode qwen command in installed environments.
        $env:VOICE_AGENT_QWEN_HTTP_CMD = ""
    }

    $brokerListening = Test-TcpPort -HostName "127.0.0.1" -Port 1883
    $mosquittoAvailable = Resolve-MosquittoAvailable
    if ($brokerListening) {
        $launcherArgs += "--no-hub"
        Write-Host "[oneclick] mqtt broker already active on 1883, skip internal broker."
    }
    elseif (-not $mosquittoAvailable) {
        $launcherArgs += "--no-hub"
        Write-Warning "[oneclick] mosquitto not found, start services in --no-hub mode."
    }

    if (Test-TcpPort -Port 8000) {
        Write-Host "[oneclick] voice service is already running on port 8000."
        return
    }

    if (Test-Path $ServiceLauncherExe) {
        Write-Host "[oneclick] starting packaged service launcher..."
        Start-Process -FilePath $ServiceLauncherExe -ArgumentList $launcherArgs -WorkingDirectory $RuntimeServicesDir -WindowStyle Hidden | Out-Null
        return
    }

    throw "Cannot start services: service_launcher.exe is missing from runtime\\services."
}

function Start-UnityClient {
    $unityExe = Resolve-UnityExe -Folder $AppDir
    if (-not $unityExe) {
        Write-Warning "[oneclick] Unity executable was not found under $AppDir."
        return
    }

    $procName = [System.IO.Path]::GetFileNameWithoutExtension($unityExe)
    $alreadyRunning = @(Get-Process -Name $procName -ErrorAction SilentlyContinue).Count -gt 0
    if ($alreadyRunning) {
        Write-Host "[oneclick] Unity process is already running: $procName"
        return
    }

    Write-Host "[oneclick] starting Unity app..."
    Start-Process -FilePath $unityExe -WorkingDirectory (Split-Path -Parent $unityExe) | Out-Null
}

function Get-StartupTargetUrl {
    $marker = Join-Path $StateDir "wizard_seen.marker"
    $ollamaHintMarker = Join-Path $StateDir "ollama_hint_seen.marker"
    if (-not (Test-Path $StateDir)) {
        New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
    }

    $firstRun = -not (Test-Path $marker)
    $configMissing = -not (Test-Path $UserConfigPath)
    $ollamaReady = Test-OllamaReachable
    $ollamaNeedsHint = (-not $ollamaReady) -and (-not (Test-Path $ollamaHintMarker))
    $openSetup = $ForceSetup -or $firstRun -or $configMissing -or $ollamaNeedsHint
    if ($openSetup) {
        try {
            Set-Content -Path $marker -Value (Get-Date).ToString("o") -Encoding UTF8
            if ($ollamaNeedsHint) {
                Set-Content -Path $ollamaHintMarker -Value (Get-Date).ToString("o") -Encoding UTF8
            }
        }
        catch {
        }
        return $PanelSetupUrl
    }
    return $PanelIndexUrl
}

Initialize-UserWritableConfig
$env:VOICE_AGENT_STATE_DIR = $StateDir
$env:DIALOG_USER_MEMORY_PATH = Join-Path $StateDir "user_memory.json"
$env:VOICE_AGENT_QMD_ROOT = Join-Path $StateDir "qmd"
$env:VOICE_AGENT_DEFAULT_CONFIG = $DefaultConfigPath
$env:VOICE_AGENT_LAUNCHER_CONFIG = $UserConfigPath
$env:VOICE_CONVERSATION_PROFILE = "local"
$env:VOICE_LOCAL_STREAMING_ASR_MODE = "live-captions"
$env:VOICE_CLOUD_STREAMING_ASR_MODE = "live-captions"
if (Test-Path $LiveCaptionsListenerExe) {
    $env:LIVE_CAPTIONS_LISTENER_EXE = (Resolve-Path $LiveCaptionsListenerExe).Path
}
$LiveCaptionsOutputDir = Join-Path $StateDir "live_captions"
if (-not (Test-Path $LiveCaptionsOutputDir)) {
    New-Item -ItemType Directory -Force -Path $LiveCaptionsOutputDir | Out-Null
}
$env:LIVE_CAPTIONS_OUTPUT_DIR = $LiveCaptionsOutputDir

Write-Host "[oneclick] app root: $AppRoot"
Write-Host "[oneclick] config: $UserConfigPath"
Apply-BundledPiperEnvironment

Start-ServiceStack

if (-not (Wait-HttpOk -Url $VoiceHealthUrl -TimeoutSec 35 -IntervalMs 700)) {
    Write-Warning "[oneclick] voice service did not pass health check in time: $VoiceHealthUrl"
}

Start-UnityClient
$panelOnline = Wait-HttpOk -Url $PanelHealthUrl -TimeoutSec 55 -IntervalMs 700
if (-not $panelOnline) {
    Write-Warning "[oneclick] panel health check timed out: $PanelHealthUrl"
}

if (-not $NoBrowser) {
    $target = Get-StartupTargetUrl
    Write-Host "[oneclick] opening browser: $target"
    Start-Process $target | Out-Null
}

exit 0
