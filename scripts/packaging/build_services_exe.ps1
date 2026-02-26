param(
    [string]$Services = "default",
    [switch]$IncludeQwen,
    [switch]$Clean,
    [switch]$SkipInstall,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$BuildRoot = Join-Path $RepoRoot ".build\pyinstaller"
$VenvDir = Join-Path $BuildRoot "venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

function Resolve-BootstrapPython {
    param([string]$Preferred)
    if ($Preferred -and (Get-Command $Preferred -ErrorAction SilentlyContinue)) {
        return $Preferred
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $versions = @("-3.11", "-3.10", "-3")
        foreach ($ver in $versions) {
            & py $ver -c "import sys; print(sys.version)" *> $null
            if ($LASTEXITCODE -eq 0) {
                return "py $ver"
            }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return "python"
    }
    throw "Python not found. Install Python 3 first."
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Exe,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Args
    )
    & $Exe @Args
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit=$LASTEXITCODE): $Exe $($Args -join ' ')"
    }
}

function Get-PythonVersionTuple {
    param([string]$PythonPath)
    $ver = & $PythonPath -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to detect Python version from: $PythonPath"
    }
    $parts = "$ver".Trim().Split(".")
    if ($parts.Count -lt 2) {
        throw "Invalid Python version output: $ver"
    }
    return [int[]]@([int]$parts[0], [int]$parts[1])
}

function Is-SupportedPyVersion {
    param([int[]]$VersionTuple)
    if (-not $VersionTuple -or $VersionTuple.Count -lt 2) { return $false }
    return ($VersionTuple[0] -eq 3 -and $VersionTuple[1] -ge 10 -and $VersionTuple[1] -le 12)
}

function Resolve-PythonExecutablePath {
    param([string]$Candidate)
    $trimmed = "$Candidate".Trim()
    if (-not $trimmed) { return "" }
    if (Test-Path $trimmed) {
        return (Resolve-Path $trimmed).Path
    }
    $cmd = Get-Command $trimmed -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        return $cmd.Source
    }
    return ""
}

function New-PackagingVenv {
    param([string]$PreferredPython)
    New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
    if ($PreferredPython) {
        Write-Host "[build-exe] create venv with explicit python: $PreferredPython"
        Invoke-Checked $PreferredPython -m venv $VenvDir
        return
    }
    $bootstrap = Resolve-BootstrapPython -Preferred ""
    Write-Host "[build-exe] create venv: $VenvDir"
    Invoke-Expression "$bootstrap -m venv `"$VenvDir`""
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit=$LASTEXITCODE): $bootstrap -m venv $VenvDir"
    }
}

$requestedPythonPath = Resolve-PythonExecutablePath -Candidate $PythonExe
$requestedPyVer = $null
if ($requestedPythonPath) {
    $requestedPyVer = Get-PythonVersionTuple -PythonPath $requestedPythonPath
    if (-not (Is-SupportedPyVersion -VersionTuple $requestedPyVer)) {
        throw "Unsupported requested Python version $($requestedPyVer[0]).$($requestedPyVer[1]). Use Python 3.10/3.11/3.12 (recommended 3.11)."
    }
}
elseif ($PythonExe) {
    throw "Specified -PythonExe not found or not executable: $PythonExe"
}

if (Test-Path $VenvPython) {
    $venvVer = Get-PythonVersionTuple -PythonPath $VenvPython
    $recreate = $false

    if (-not (Is-SupportedPyVersion -VersionTuple $venvVer)) {
        $recreate = $true
        Write-Host "[build-exe] existing venv uses unsupported Python $($venvVer[0]).$($venvVer[1]); recreating."
    }
    elseif ($requestedPyVer -and ($venvVer[0] -ne $requestedPyVer[0] -or $venvVer[1] -ne $requestedPyVer[1])) {
        $recreate = $true
        Write-Host "[build-exe] existing venv Python $($venvVer[0]).$($venvVer[1]) does not match requested $($requestedPyVer[0]).$($requestedPyVer[1]); recreating."
    }

    if ($recreate -and (Test-Path $VenvDir)) {
        Remove-Item -Recurse -Force $VenvDir
    }
}

if (-not (Test-Path $VenvPython)) {
    New-PackagingVenv -PreferredPython $requestedPythonPath
}

$pyver = Get-PythonVersionTuple -PythonPath $VenvPython
if (-not (Is-SupportedPyVersion -VersionTuple $pyver)) {
    throw "Unsupported packaging Python version $($pyver[0]).$($pyver[1]). Use Python 3.10/3.11/3.12 (recommended 3.11)."
}

if (-not $SkipInstall) {
    Write-Host "[build-exe] install build/runtime dependencies"
    Invoke-Checked $VenvPython -m pip install --prefer-binary -U pip setuptools wheel pyinstaller
    Invoke-Checked $VenvPython -m pip install --prefer-binary -r (Join-Path $RepoRoot "python_voice_service\requirements.txt")
    Invoke-Checked $VenvPython -m pip install --prefer-binary -r (Join-Path $RepoRoot "scripts\intent_service\requirements.txt")
    Invoke-Checked $VenvPython -m pip install --prefer-binary -r (Join-Path $RepoRoot "scripts\dialog_service\requirements.txt")
    if ($IncludeQwen -or ($Services -match "(^|,)\s*qwen_tts_http\s*(,|$)")) {
        Invoke-Checked $VenvPython -m pip install --prefer-binary -r (Join-Path $RepoRoot "python_voice_service\requirements_qwen_tts.txt")
    }
}

$BuildScript = Join-Path $RepoRoot "scripts\packaging\build_services_exe.py"
$args = @($BuildScript, "--services", $Services)
if ($IncludeQwen) { $args += "--include-qwen" }
if ($Clean) { $args += "--clean" }

Write-Host "[build-exe] run build script"
Invoke-Checked $VenvPython @args
Write-Host "[build-exe] completed."
