param(
    [string]$UnityBuildDir = "",
    [string]$ServicesDir = "",
    [string]$IsccPath = "",
    [string]$PiperRuntimeDir = ""
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$InstallerScript = Join-Path $RepoRoot "installer\voice_agent_setup.iss"

function Resolve-IsccPath {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        if (-not (Test-Path $RequestedPath)) {
            throw "Provided ISCC.exe path not found: $RequestedPath"
        }
        return (Resolve-Path $RequestedPath).Path
    }

    $candidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe",
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return (Resolve-Path $candidate).Path
        }
    }

    $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and (Test-Path $cmd.Source)) {
        return (Resolve-Path $cmd.Source).Path
    }

    $registryRoots = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    )
    foreach ($root in $registryRoots) {
        if (-not (Test-Path $root)) {
            continue
        }

        foreach ($key in (Get-ChildItem $root -ErrorAction SilentlyContinue)) {
            try {
                $item = Get-ItemProperty $key.PSPath -ErrorAction Stop
                $displayName = "$($item.DisplayName)"
                if ($displayName -notlike "Inno Setup*") {
                    continue
                }

                $installLocation = "$($item.InstallLocation)".Trim()
                if ($installLocation) {
                    $fromInstall = Join-Path $installLocation "ISCC.exe"
                    if (Test-Path $fromInstall) {
                        return (Resolve-Path $fromInstall).Path
                    }
                }

                $displayIcon = "$($item.DisplayIcon)".Trim().Trim('"')
                if ($displayIcon) {
                    $iconDir = Split-Path -Parent $displayIcon
                    if ($iconDir) {
                        $fromIconDir = Join-Path $iconDir "ISCC.exe"
                        if (Test-Path $fromIconDir) {
                            return (Resolve-Path $fromIconDir).Path
                        }
                    }
                }
            }
            catch {
            }
        }
    }

    return ""
}

if (-not (Test-Path $InstallerScript)) {
    throw "Installer script not found: $InstallerScript"
}

if (-not $UnityBuildDir) { $UnityBuildDir = Join-Path $RepoRoot "dist\unity" }
if (-not $ServicesDir) { $ServicesDir = Join-Path $RepoRoot "dist\services" }
if (-not $PiperRuntimeDir) {
    $candidate = Join-Path $RepoRoot "third_party\piper_runtime"
    if (Test-Path $candidate) {
        $PiperRuntimeDir = $candidate
    }
}

if (-not (Test-Path $UnityBuildDir)) {
    throw "Unity build directory not found: $UnityBuildDir"
}
if (-not (Test-Path $ServicesDir)) {
    throw "Service executable directory not found: $ServicesDir"
}
if ($PiperRuntimeDir -and -not (Test-Path $PiperRuntimeDir)) {
    throw "Piper runtime directory not found: $PiperRuntimeDir"
}

$UnityBuildDir = (Resolve-Path $UnityBuildDir).Path
$ServicesDir = (Resolve-Path $ServicesDir).Path
if ($PiperRuntimeDir) {
    $PiperRuntimeDir = (Resolve-Path $PiperRuntimeDir).Path
}

$IsccPath = Resolve-IsccPath -RequestedPath $IsccPath

if (-not $IsccPath) {
    throw "ISCC.exe not found. Install Inno Setup 6 and pass -IsccPath."
}

Write-Host "[installer] ISCC: $IsccPath"
Write-Host "[installer] Unity build: $UnityBuildDir"
Write-Host "[installer] Services: $ServicesDir"
if ($PiperRuntimeDir) {
    Write-Host "[installer] Piper runtime: $PiperRuntimeDir"
}
else {
    Write-Host "[installer] Piper runtime: <not bundled>"
}

$isccArgs = @(
    # Use forward slashes for ISPP define values to avoid backslash escape
    # parsing edge cases in Inno preprocessor command-line defines.
    "/DUnityBuildDir=$($UnityBuildDir -replace '\\','/')",
    "/DServiceExeDir=$($ServicesDir -replace '\\','/')"
)
if ($PiperRuntimeDir) {
    $isccArgs += "/DPiperRuntimeDir=$($PiperRuntimeDir -replace '\\','/')"
}
$isccArgs += $InstallerScript
$buildStarted = Get-Date
& $IsccPath @isccArgs
if ($LASTEXITCODE -ne 0) {
    throw "ISCC.exe failed with exit code $LASTEXITCODE"
}

$installerOutput = Join-Path $RepoRoot "dist\installer\VoiceAgentSetup.exe"
if (-not (Test-Path $installerOutput)) {
    throw "Installer output not found: $installerOutput"
}
$outItem = Get-Item $installerOutput
if ($outItem.LastWriteTime -lt $buildStarted.AddSeconds(-2)) {
    throw "Installer output was not updated: $installerOutput"
}

Write-Host "[installer] completed. Output in dist\installer."
