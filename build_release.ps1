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
        Write-Error "Compiled binary not found at $ExePath. Run build_release.ps1 first."
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

$IconScript = Join-Path $RepoRoot "scripts\generate_icon.py"
$IconPath = Join-Path $RepoRoot "assets\icon.ico"
if (-not (Test-Path $IconPath)) {
    & $PythonExe $IconScript
}

$WebDir = Join-Path $RepoRoot "web"
$SrcDir = Join-Path $RepoRoot "src"
$MainPy = Join-Path $SrcDir "main.py"
$BuildWork = Join-Path $RepoRoot "build\pyinstaller"
$BuildSpec = Join-Path $RepoRoot "build"
$DistDir = Join-Path $RepoRoot "dist"

Write-Host "Packaging RebuttalSidecar.exe v1.0.0 with PyInstaller..."
& $PythonExe -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --noconsole `
    --name RebuttalSidecar `
    --icon $IconPath `
    --add-data "$WebDir;web" `
    --add-data "$IconPath;assets" `
    --collect-all webview `
    --collect-all pythonnet `
    --collect-all clr_loader `
    --paths $SrcDir `
    --specpath $BuildSpec `
    --workpath $BuildWork `
    --distpath $DistDir `
    $MainPy

if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
Write-Host "Built standalone release binary: $ExePath"
