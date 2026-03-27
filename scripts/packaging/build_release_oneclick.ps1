param(
    [Parameter(Mandatory = $true)]
    [string]$UnityBuildDir,
    [string]$Services = "default",
    [switch]$Clean,
    [switch]$SkipServiceBuild,
    [switch]$SkipInstallDeps,
    [string]$PythonExe = "",
    [string]$IsccPath = "",
    [string]$PiperRuntimeDir = ""
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
$BuildServices = Join-Path $RepoRoot "scripts\packaging\build_services_exe.ps1"
$BuildInstaller = Join-Path $RepoRoot "scripts\packaging\build_installer.ps1"
$ServicesDir = Join-Path $RepoRoot "dist\services"

if (-not (Test-Path $UnityBuildDir)) {
    throw "Unity build directory not found: $UnityBuildDir"
}

if (-not $SkipServiceBuild) {
    $svcArgs = @{
        Services   = $Services
    }
    if ($Clean) { $svcArgs.Clean = $true }
    if ($SkipInstallDeps) { $svcArgs.SkipInstall = $true }
    if ($PythonExe) { $svcArgs.PythonExe = $PythonExe }
    Write-Host "[release] building service executables..."
    & $BuildServices @svcArgs
}
else {
    Write-Host "[release] skip service executable build."
}

if (-not (Test-Path $ServicesDir)) {
    throw "Services output directory not found: $ServicesDir"
}

$installerArgs = @{
    UnityBuildDir = $UnityBuildDir
    ServicesDir   = $ServicesDir
}
if ($IsccPath) { $installerArgs.IsccPath = $IsccPath }
if ($PiperRuntimeDir) { $installerArgs.PiperRuntimeDir = $PiperRuntimeDir }

Write-Host "[release] building installer..."
& $BuildInstaller @installerArgs

Write-Host "[release] done."
