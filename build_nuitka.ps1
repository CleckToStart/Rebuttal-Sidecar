param(
    [switch]$RegisterContextMenu,
    [switch]$UnregisterContextMenu
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ExePath = Join-Path $RepoRoot "dist\RebuttalSidecar.exe"

if ($RegisterContextMenu) {
    if (-not (Test-Path $ExePath)) {
        Write-Error "Compiled binary not found at $ExePath. Run build_nuitka.ps1 first."
    }
    $regKey = "HKCU:\Software\Classes\Directory\shell\RebuttalSidecar"
    New-Item -Path $regKey -Force | Out-Null
    Set-ItemProperty -Path $regKey -Name "(Default)" -Value "用 Rebuttal Sidecar 打开"
    Set-ItemProperty -Path $regKey -Name "Icon" -Value "$ExePath,0"
    New-Item -Path "$regKey\command" -Force | Out-Null
    Set-ItemProperty -Path "$regKey\command" -Name "(Default)" -Value "`"$ExePath`" `"%1`""
    Write-Host "Registered Explorer context menu under HKCU."
    exit 0
}

if ($UnregisterContextMenu) {
    Remove-Item -Path "HKCU:\Software\Classes\Directory\shell\RebuttalSidecar" -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "Removed Explorer context menu."
    exit 0
}

if (-not (Test-Path $PythonExe)) {
    Write-Error ".venv not found. Create .venv and install dependencies first."
}

# Detect MSVC first; fall back to MinGW64 when Visual Studio Build Tools are absent.
$compilerArgs = @("--mingw64", "--assume-yes-for-downloads")
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if ((Get-Command cl -ErrorAction SilentlyContinue) -or ((Test-Path $vswhere) -and (& $vswhere -latest -products * -property installationPath))) {
    $compilerArgs = @("--msvc=latest")
}

Write-Host "Building RebuttalSidecar.exe with Nuitka ($($compilerArgs[0]))..."
& $PythonExe -m nuitka `
    --standalone `
    --onefile `
    @compilerArgs `
    --windows-console-mode=disable `
    --windows-icon-from-ico=assets/icon.ico `
    --enable-plugin=anti-bloat `
    --include-package-data=webview `
    --include-data-dir=web=web `
    --include-data-files=assets/icon.ico=assets/icon.ico `
    --nofollow-import-to=pytest,unittest,tkinter,PIL,numpy `
    --output-dir=dist `
    --output-filename=RebuttalSidecar.exe `
    src/main.py
