[CmdletBinding()]
param(
    [string] $Python = "",
    [switch] $Publish
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$WindowsDir = $PSScriptRoot
$DesktopDir = Split-Path -Parent $WindowsDir
$RepoRoot = Split-Path -Parent $DesktopDir
$Requirements = Join-Path $RepoRoot "requirements-windows.txt"
$Spec = Join-Path $WindowsDir "sidecar-win.spec"
$SidecarDist = Join-Path $DesktopDir "build\windows-sidecar"
$PyInstallerWork = Join-Path $DesktopDir "build\pyinstaller-windows"

if (-not $Python) {
    $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Windows venv Python was not found at $Python"
}
if (-not (Test-Path -LiteralPath $Requirements -PathType Leaf)) {
    throw "Windows requirements were not found at $Requirements"
}
if (-not (Test-Path -LiteralPath (Join-Path $WindowsDir "sidecar-win.py") -PathType Leaf)) {
    throw "The Windows Electron sidecar entrypoint is missing"
}
if (-not (Test-Path -LiteralPath (Join-Path $DesktopDir "package-lock.json") -PathType Leaf)) {
    throw "The shared Electron package-lock.json must be integrated before the Windows build"
}

function Assert-LastExitCode {
    param([string] $Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with exit code $LASTEXITCODE"
    }
}

$env:PYTHONUTF8 = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"

Write-Host "[windows] installing the Serena and build dependencies"
& $Python -m pip install --disable-pip-version-check -e "${RepoRoot}[dev]"
Assert-LastExitCode "Serena dependency installation"
& $Python -m pip install --disable-pip-version-check -r $Requirements "pyinstaller==6.21.0"
Assert-LastExitCode "Windows dependency installation"

Write-Host "[windows] running the mocked ConPTY contract tests"
& $Python -m pytest (Join-Path $RepoRoot "tests\test_windows_pty_backend.py") -q
Assert-LastExitCode "Windows PTY tests"

if (Test-Path -LiteralPath $SidecarDist) {
    Remove-Item -LiteralPath $SidecarDist -Recurse -Force
}
if (Test-Path -LiteralPath $PyInstallerWork) {
    Remove-Item -LiteralPath $PyInstallerWork -Recurse -Force
}
New-Item -ItemType Directory -Path $SidecarDist -Force | Out-Null
New-Item -ItemType Directory -Path $PyInstallerWork -Force | Out-Null

Write-Host "[windows] building the PyInstaller onedir sidecar"
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --distpath $SidecarDist `
    --workpath $PyInstallerWork `
    $Spec
Assert-LastExitCode "PyInstaller sidecar build"

$SidecarExe = Join-Path $SidecarDist "serena-web-sidecar\serena-web-sidecar.exe"
if (-not (Test-Path -LiteralPath $SidecarExe -PathType Leaf)) {
    throw "PyInstaller did not produce $SidecarExe"
}

Write-Host "[windows] smoke-testing the frozen PTY backend"
$PtySmoke = Start-Process -FilePath $SidecarExe `
    -ArgumentList @("--pty-smoke") `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
if ($PtySmoke.ExitCode -ne 0) {
    throw "the frozen PTY smoke test failed with exit code $($PtySmoke.ExitCode)"
}

# The exe existing proves nothing. It is built console=False, so a hidden import
# PyInstaller failed to trace does not fail the build and does not print: the
# process just dies and Electron waits forever on a port that never opens.
# Boot it for real and poll the probe ui.web serves. This is the only step that
# distinguishes "packaged" from "works".
Write-Host "[windows] smoke-testing the frozen sidecar"
$Listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$Listener.Start()
$SmokePort = $Listener.LocalEndpoint.Port
$Listener.Stop()

$Smoke = Start-Process -FilePath $SidecarExe `
    -ArgumentList @("--host", "127.0.0.1", "--port", "$SmokePort") `
    -WindowStyle Hidden `
    -PassThru
try {
    $Probe = "http://127.0.0.1:$SmokePort/api/health"
    $Deadline = (Get-Date).AddSeconds(120)
    $Healthy = $false
    while ((Get-Date) -lt $Deadline) {
        if ($Smoke.HasExited) {
            throw "the frozen sidecar exited with code $($Smoke.ExitCode) before answering $Probe"
        }
        try {
            $Response = Invoke-WebRequest -Uri $Probe -UseBasicParsing -TimeoutSec 5
            if ($Response.StatusCode -eq 200) {
                $Healthy = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $Healthy) {
        throw "the frozen sidecar never answered $Probe"
    }
    Write-Host "[windows] sidecar answered $Probe"
}
finally {
    if (-not $Smoke.HasExited) {
        Stop-Process -Id $Smoke.Id -Force
    }
}

$PublishMode = if ($Publish) { "always" } else { "never" }
Push-Location $DesktopDir
try {
    Write-Host "[windows] installing the locked Electron dependencies"
    & npm ci
    Assert-LastExitCode "npm ci"
    & npm ls electron-updater --omit=dev --depth=0
    Assert-LastExitCode "electron-updater runtime dependency check"

    Write-Host "[windows] building the NSIS installer (publish=$PublishMode)"
    & npx --no-install electron-builder `
        --config windows/electron-builder.win.yml `
        --win nsis `
        --x64 `
        --publish $PublishMode
    Assert-LastExitCode "electron-builder"
}
finally {
    Pop-Location
}

$Package = Get-Content -LiteralPath (Join-Path $DesktopDir "package.json") -Raw |
    ConvertFrom-Json
$InstallerPath = Join-Path `
    $DesktopDir `
    "dist\windows\Serena-Setup-$($Package.version)-x64.exe"
if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
    throw "electron-builder did not produce the expected NSIS installer: $InstallerPath"
}
$Installer = Get-Item -LiteralPath $InstallerPath

Write-Host "[windows] sidecar: $SidecarExe"
Write-Host "[windows] installer: $($Installer.FullName)"
