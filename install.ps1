# CMVideo - Windows installer
#
# Sets up everything needed to run CMVideo on Windows:
#   - Python 3.10+                  (via winget, else opens python.org)
#   - ffmpeg + ffprobe              (winget, else portable download to .\bin\)
#   - espeak-ng (Fun mode)          (winget, else portable download to .\bin\)
#   - Local .venv with pip deps     (always created in the project folder)
#   - Start Menu + Desktop shortcut (no admin needed; user-scope)
#
# Re-runnable. Already have something installed? It detects and skips it.
# Nothing here touches HKLM, the system PATH, or Program Files - all
# user-scope, so no UAC prompt is required.

#requires -version 5.1
[CmdletBinding()]
param(
    [switch]$SkipShortcuts,
    [switch]$Force  # force re-download of portable binaries even if present
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # speeds up Invoke-WebRequest a lot

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppName = "CMVideo"
$BinDir = Join-Path $Here "bin"
$VenvDir = Join-Path $Here ".venv"

function Banner($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Info($msg)   { Write-Host "    $msg" -ForegroundColor Gray }
function Ok($msg)     { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg)   { Write-Host "    $msg" -ForegroundColor Yellow }
function Fail($msg)   { Write-Host "    $msg" -ForegroundColor Red; exit 1 }

function Refresh-Path {
    # winget adds binaries to PATH but only future processes see them.
    # Re-read both Machine and User scopes so the rest of this script
    # can find newly-installed Python / ffmpeg / espeak-ng.
    $machine = [Environment]::GetEnvironmentVariable("PATH", "Machine")
    $user = [Environment]::GetEnvironmentVariable("PATH", "User")
    $env:PATH = "$machine;$user"
}

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Find-Python {
    foreach ($cmd in @("py", "python", "python3")) {
        if (-not (Test-Command $cmd)) { continue }
        try {
            $v = & $cmd -c "import sys; print('{}.{}'.format(*sys.version_info[:2]))" 2>$null
            if ($v -and [version]$v -ge [version]"3.10") {
                return @{ Cmd = $cmd; Version = $v }
            }
        } catch { }
    }
    return $null
}

function Find-Exe {
    param([string]$Name)
    if (Test-Command $Name) { return (Get-Command $Name).Source }
    return $null
}

function Download-File {
    param([string]$Url, [string]$Dest)
    Info "Downloading $Url"
    Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing
}

function Expand-And-Flatten {
    # Unzip an archive into $TargetDir. If the archive contains a single
    # top-level directory (the common case for ffmpeg / espeak-ng builds),
    # promote its contents up so the final layout is predictable.
    param([string]$Zip, [string]$TargetDir)
    $staging = Join-Path $env:TEMP ("cmv-stage-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $staging | Out-Null
    Expand-Archive -Path $Zip -DestinationPath $staging -Force
    $children = Get-ChildItem $staging
    if ($children.Count -eq 1 -and $children[0].PSIsContainer) {
        $src = $children[0].FullName
    } else {
        $src = $staging
    }
    if (Test-Path $TargetDir) { Remove-Item -Recurse -Force $TargetDir }
    New-Item -ItemType Directory -Path (Split-Path $TargetDir -Parent) -Force | Out-Null
    Move-Item $src $TargetDir
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
}

# ----------------------------------------------------------------------
# 0. Detect winget
# ----------------------------------------------------------------------
Banner "Checking package manager"
$HasWinget = Test-Command "winget"
if ($HasWinget) {
    Ok "winget is available"
} else {
    Warn "winget is not available - portable downloads will be used instead"
    Info "(winget ships with Windows 10 1809+ and Windows 11. App Installer in the Microsoft Store.)"
}

# ----------------------------------------------------------------------
# 1. Python 3.10+
# ----------------------------------------------------------------------
Banner "Checking Python"
$Py = Find-Python
if ($Py) {
    Ok "Found Python $($Py.Version) (`"$($Py.Cmd)`")"
} elseif ($HasWinget) {
    Info "Installing Python via winget..."
    winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements --scope user | Out-Host
    Refresh-Path
    $Py = Find-Python
    if (-not $Py) {
        Fail "Python install via winget didn't expose Python on PATH. Open a new terminal and re-run install.ps1, or install Python from https://www.python.org/downloads/ manually."
    }
    Ok "Installed Python $($Py.Version)"
} else {
    Warn "No Python 3.10+ found and winget is unavailable."
    Start-Process "https://www.python.org/downloads/"
    Fail "Install Python 3.10+ (tick 'Add python.exe to PATH' during install), then re-run install.ps1."
}

# ----------------------------------------------------------------------
# 2. ffmpeg + ffprobe
# ----------------------------------------------------------------------
Banner "Checking ffmpeg"
$FfmpegLocal = Join-Path $BinDir "ffmpeg\bin\ffmpeg.exe"
$NeedFfmpeg = -not (Find-Exe "ffmpeg") -and -not (Test-Path $FfmpegLocal)
if ($Force -or $NeedFfmpeg) {
    $installed = $false
    if ($HasWinget) {
        Info "Trying winget (Gyan.FFmpeg)..."
        try {
            winget install --id Gyan.FFmpeg --silent --accept-package-agreements --accept-source-agreements --scope user | Out-Host
            Refresh-Path
            if (Find-Exe "ffmpeg") {
                Ok "Installed ffmpeg via winget"
                $installed = $true
            }
        } catch {
            Warn "winget install failed: $($_.Exception.Message)"
        }
    }
    if (-not $installed) {
        Info "Downloading portable ffmpeg (essentials build, ~80 MB)..."
        $url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        $zip = Join-Path $env:TEMP "cmv-ffmpeg.zip"
        Download-File $url $zip
        $dest = Join-Path $BinDir "ffmpeg"
        Expand-And-Flatten $zip $dest
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        if (-not (Test-Path $FfmpegLocal)) {
            Fail "Portable ffmpeg extracted but ffmpeg.exe was not at $FfmpegLocal"
        }
        Ok "Portable ffmpeg installed at .\bin\ffmpeg\"
    }
} else {
    if (Find-Exe "ffmpeg") { Ok "Found ffmpeg on PATH" }
    else { Ok "Found portable ffmpeg at .\bin\ffmpeg\" }
}

# ----------------------------------------------------------------------
# 3. espeak-ng (optional - powers 'Fun' censor mode)
# ----------------------------------------------------------------------
Banner "Checking espeak-ng (powers the Fun censor mode)"

function Find-Espeak {
    # Look on PATH first, then in the usual Program Files locations
    # winget drops it into, then in our portable .\bin\ folder.
    $candidates = @(
        "espeak-ng", "espeak",
        "$env:ProgramFiles\eSpeak NG\espeak-ng.exe",
        "${env:ProgramFiles(x86)}\eSpeak NG\espeak-ng.exe",
        (Join-Path $BinDir "espeak-ng\espeak-ng.exe")
    )
    foreach ($c in $candidates) {
        if ([string]::IsNullOrWhiteSpace($c)) { continue }
        if (Test-Path $c) { return $c }
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    return $null
}

$EspeakPath = Find-Espeak
if (-not $Force -and $EspeakPath) {
    Ok "espeak-ng already available at $EspeakPath"
} else {
    $installed = $false
    if ($HasWinget) {
        Info "Trying winget (eSpeak-NG.eSpeak-NG)..."
        try {
            winget install --id eSpeak-NG.eSpeak-NG --silent --accept-package-agreements --accept-source-agreements | Out-Host
            Refresh-Path
            $EspeakPath = Find-Espeak
            if ($EspeakPath) {
                Ok "Installed espeak-ng via winget ($EspeakPath)"
                $installed = $true
            }
        } catch {
            Warn "winget install failed: $($_.Exception.Message)"
        }
    }
    if (-not $installed) {
        # No portable build is published on GitHub, so we fall back to
        # the official MSI. msiexec /qn will pop a single UAC prompt
        # the first time; if the user declines, we just warn and move
        # on - the rest of the app works fine without espeak-ng.
        $url = "https://github.com/espeak-ng/espeak-ng/releases/latest/download/espeak-ng-X64.msi"
        $msi = Join-Path $env:TEMP "cmv-espeak-ng.msi"
        try {
            Info "Downloading espeak-ng MSI from GitHub..."
            Download-File $url $msi
            Info "Running msiexec (UAC prompt may appear)..."
            $p = Start-Process "msiexec.exe" -ArgumentList "/i", "`"$msi`"", "/qn", "/norestart" -Wait -PassThru
            Remove-Item $msi -Force -ErrorAction SilentlyContinue
            Refresh-Path
            $EspeakPath = Find-Espeak
            if ($EspeakPath) {
                Ok "espeak-ng installed at $EspeakPath"
                $installed = $true
            } elseif ($p.ExitCode -ne 0) {
                Warn "msiexec exited with code $($p.ExitCode)."
            }
        } catch {
            Warn "Couldn't fetch espeak-ng automatically: $($_.Exception.Message)"
        }
    }
    if (-not $installed) {
        Warn "espeak-ng was not installed. CMVideo will still work; only"
        Warn "the 'Fun' censor mode needs it. Install it later from"
        Warn "https://github.com/espeak-ng/espeak-ng/releases"
    }
}

# ----------------------------------------------------------------------
# 4. Python venv + pip dependencies
# ----------------------------------------------------------------------
Banner "Setting up Python environment in .venv"
if (-not (Test-Path $VenvDir)) {
    Info "Creating venv with $($Py.Cmd)..."
    & $Py.Cmd -m venv $VenvDir
} else {
    Ok ".venv already exists"
}
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) { Fail "venv setup failed; $VenvPython missing" }

Info "Upgrading pip..."
& $VenvPython -m pip install --quiet --upgrade pip
Info "Installing requirements (this can take a few minutes the first time)..."
& $VenvPython -m pip install --quiet -r (Join-Path $Here "requirements.txt")
Ok "Python dependencies installed"

# CUDA runtime: install only if we can see an NVIDIA GPU. faster-whisper
# will silently fall back to CPU if these are missing or fail to load,
# so it's safe to skip on non-NVIDIA machines.
if (Test-Command "nvidia-smi") {
    try {
        & nvidia-smi -L 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Info "NVIDIA GPU detected - installing cuBLAS + cuDNN wheels (~1 GB)..."
            & $VenvPython -m pip install --quiet "nvidia-cublas-cu12" "nvidia-cudnn-cu12"
            if ($LASTEXITCODE -eq 0) {
                Ok "GPU runtime installed"
            } else {
                Warn "CUDA wheels failed to install. App will fall back to CPU."
            }
        }
    } catch {
        Warn "Couldn't query nvidia-smi: $($_.Exception.Message). Skipping CUDA setup."
    }
}

# ----------------------------------------------------------------------
# 5. Start Menu + Desktop shortcut
# ----------------------------------------------------------------------
if (-not $SkipShortcuts) {
    Banner "Creating shortcuts"
    $WshShell = New-Object -ComObject WScript.Shell
    # Per-user Start Menu\Programs (no admin needed)
    $StartMenuDir = [Environment]::GetFolderPath('Programs')
    $Desktop = [Environment]::GetFolderPath('Desktop')
    $Icon = Join-Path $Here "icon.ico"
    if (-not (Test-Path $Icon)) { $Icon = Join-Path $Here "icon.png" }

    foreach ($folder in @($StartMenuDir, $Desktop)) {
        if (-not (Test-Path $folder)) { continue }
        $lnk = Join-Path $folder "$AppName.lnk"
        $s = $WshShell.CreateShortcut($lnk)
        $s.TargetPath = Join-Path $Here "run.bat"
        $s.WorkingDirectory = $Here
        $s.IconLocation = $Icon
        $s.Description = "Auto-censor swears and slurs from video and audio"
        $s.WindowStyle = 7  # minimized: hides the cmd window briefly
        $s.Save()
        Ok "Created shortcut: $lnk"
    }
}

Write-Host ""
Write-Host "$AppName is installed!" -ForegroundColor Green
Write-Host "Launch it from the Start menu or Desktop, or by double-clicking run.bat in this folder."
Write-Host ""
Write-Host "To remove (keeps ffmpeg/espeak-ng/Python around):  powershell -ExecutionPolicy Bypass -File uninstall.ps1"
