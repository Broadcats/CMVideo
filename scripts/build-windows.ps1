#requires -version 5.1
<#
 Build a single-file CMVideo executable for Windows x64.

 Prereqs (satisfied on GitHub-hosted runners): Windows 10/11 x64,
 internet access, Python 3.12 (install via actions/setup-python).

 Output: dist/CMVideo-<version>-win-amd64.exe (+ .sha256)

 The bundle is CPU-only (CUDA wheels stripped). Whisper weights still
 download on first transcribe into the user cache.

 Run from the repo root or pass -RepoRoot:
   pwsh ./scripts/build-windows.ps1
#>
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Banner($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host "ERROR: $m" -ForegroundColor Red; exit 1 }

$Here = Resolve-Path $RepoRoot
Set-Location $Here

# ----------------------------------------------------------------------
# Version string (must match censor/version.py)
# ----------------------------------------------------------------------
$VerLine = Get-Content (Join-Path $Here "censor\version.py") -Raw
if ($VerLine -notmatch 'APP_VERSION\s*=\s*"([^"]+)"') {
    Fail "Could not read APP_VERSION from censor/version.py"
}
$AppVersion = $Matches[1]
$OutExe = Join-Path $Here ("dist\CMVideo-{0}-win-amd64.exe" -f $AppVersion)

# ----------------------------------------------------------------------
# FFmpeg (portable essentials zip - same URL as install.ps1)
# ----------------------------------------------------------------------
$FfmpegUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
$FfmpegZip = Join-Path $env:TEMP ("cmv-ffmpeg-" + [Guid]::NewGuid().ToString("N") + ".zip")
$FfmpegStage = Join-Path $env:TEMP ("cmv-ffmpeg-stage-" + [Guid]::NewGuid().ToString("N"))

Banner "Downloading ffmpeg ($FfmpegUrl)"
Invoke-WebRequest -Uri $FfmpegUrl -OutFile $FfmpegZip -UseBasicParsing
New-Item -ItemType Directory -Path $FfmpegStage -Force | Out-Null
Expand-Archive -Path $FfmpegZip -DestinationPath $FfmpegStage -Force
Remove-Item $FfmpegZip -Force -ErrorAction SilentlyContinue

$FfmpegExe = Get-ChildItem -Path $FfmpegStage -Filter "ffmpeg.exe" -Recurse -File |
    Select-Object -First 1 -ExpandProperty FullName
$FfprobeExe = Get-ChildItem -Path $FfmpegStage -Filter "ffprobe.exe" -Recurse -File |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $FfmpegExe -or -not $FfprobeExe) {
    Fail "ffmpeg zip did not contain ffmpeg.exe / ffprobe.exe"
}

# ----------------------------------------------------------------------
# eSpeak NG (silent per-machine MSI - works on GitHub Actions)
# ----------------------------------------------------------------------
Banner "Installing eSpeak NG (silent MSI)"
$rel = Invoke-RestMethod -Uri "https://api.github.com/repos/espeak-ng/espeak-ng/releases/latest" -Headers @{
    "User-Agent" = "CMVideo-build-script"
}
# Asset name has churned across releases (espeak-ng-X64.msi, espeak-ng.msi, ...).
# Prefer an explicitly x64-named MSI; otherwise pick whichever .msi the release
# offers (1.52+ ships a single platform-neutral espeak-ng.msi).
$msiAsset = $rel.assets |
    Where-Object { $_.name -like "*.msi" } |
    Sort-Object @{ Expression = { if ($_.name -match "(?i)x64") { 0 } else { 1 } } }, name |
    Select-Object -First 1
if (-not $msiAsset) {
    Fail ("Could not find an .msi asset in espeak-ng release {0}" -f $rel.tag_name)
}
$msiUrl = $msiAsset.browser_download_url
Write-Host ("    Using {0} from espeak-ng {1}" -f $msiAsset.name, $rel.tag_name)
$MsiPath = Join-Path $env:TEMP ("cmv-espeak-" + [Guid]::NewGuid().ToString("N") + ".msi")
Invoke-WebRequest -Uri $msiUrl -OutFile $MsiPath -UseBasicParsing

$p = Start-Process -FilePath "msiexec.exe" -ArgumentList @(
    "/i", "`"$MsiPath`"",
    "/qn",
    "/norestart",
    "ALLUSERS=1"
) -Wait -PassThru -NoNewWindow
Remove-Item $MsiPath -Force -ErrorAction SilentlyContinue
if ($p.ExitCode -ne 0) {
    Fail "msiexec failed installing eSpeak NG (exit $($p.ExitCode))"
}

$EspeakRoot = $null
foreach ($cand in @(
        (Join-Path $env:ProgramFiles "eSpeak NG"),
        (Join-Path ${env:ProgramFiles(x86)} "eSpeak NG")
    )) {
    if ($cand -and (Test-Path (Join-Path $cand "espeak-ng.exe"))) {
        $EspeakRoot = $cand
        break
    }
}
if (-not $EspeakRoot) {
    Fail "eSpeak NG MSI ran but espeak-ng.exe was not found under Program Files"
}
$EspeakExe = Join-Path $EspeakRoot "espeak-ng.exe"
$EspeakData = Join-Path $EspeakRoot "espeak-ng-data"
if (-not (Test-Path $EspeakData)) {
    Fail "espeak-ng-data folder missing at $EspeakData"
}

# ----------------------------------------------------------------------
# Python venv + deps
# ----------------------------------------------------------------------
$VenvDir = Join-Path $Here ".venv-winexe"
$Py = Join-Path $VenvDir "Scripts\python.exe"
$TmpSpec = Join-Path $env:TEMP ("cmvideo-" + [Guid]::NewGuid().ToString("N"))

if (-not (Test-Path $Py)) {
    Banner "Creating venv at $VenvDir"
    python -m venv $VenvDir
}
& $Py -m pip install --quiet --upgrade pip wheel
& $Py -m pip install --quiet pyinstaller
& $Py -m pip install --quiet -r (Join-Path $Here "requirements.txt")
& $Py -m pip install --quiet pillow

& $Py -m pip uninstall -y nvidia-cublas-cu12 nvidia-cudnn-cu12 nvidia-cuda-nvrtc-cu12 2>$null

# ----------------------------------------------------------------------
# PyInstaller (one-file / windowed)
# ----------------------------------------------------------------------
Banner "Running PyInstaller (one-file)"

$DistPath = Join-Path $Here "dist"
$WorkPath = Join-Path $Here "build\winexe\work"

$Data = @(
    "$(Join-Path $Here 'wordlists');wordlists",
    "$(Join-Path $Here 'assets');assets",
    "$(Join-Path $Here 'icon.png');.",
    "$(Join-Path $Here 'icon-32.png');.",
    "$(Join-Path $Here 'icon-64.png');.",
    "$(Join-Path $Here 'icon-128.png');.",
    "$(Join-Path $Here 'icon.svg');."
)

$Binaries = @(
    "$FfmpegExe;.",
    "$FfprobeExe;.",
    "$EspeakExe;."
)

Get-ChildItem -Path $EspeakRoot -Filter "*.dll" -File | ForEach-Object {
    $Binaries += , ($_.FullName + ";.")
}

$Data += , ($EspeakData + ";espeak-ng-data")

$Args = @(
    "-m", "PyInstaller",
    "--noconfirm", "--clean",
    "--onefile", "--windowed",
    "--name", "CMVideo",
    "--paths", $Here,
    "--distpath", $DistPath,
    "--workpath", $WorkPath,
    "--specpath", $TmpSpec,
    "--collect-all", "tkinterdnd2",
    "--collect-all", "customtkinter",
    "--collect-all", "faster_whisper",
    "--collect-all", "yt_dlp",
    "--collect-all", "ctranslate2",
    "--collect-all", "onnxruntime",
    "--collect-all", "av",
    "--collect-submodules", "censor"
)

foreach ($d in $Data) { $Args += @("--add-data", $d) }
foreach ($b in $Binaries) { $Args += @("--add-binary", $b) }

$Ico = Join-Path $Here "icon.ico"
if (Test-Path $Ico) {
    $Args += @("--icon", $Ico)
}

$Args += (Join-Path $Here "app.py")

& $Py @Args
if ($LASTEXITCODE -ne 0) {
    Fail "PyInstaller failed (exit $LASTEXITCODE)"
}

Remove-Item $TmpSpec -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $WorkPath -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $FfmpegStage -Recurse -Force -ErrorAction SilentlyContinue

$Built = Join-Path $DistPath "CMVideo.exe"
if (-not (Test-Path $Built)) {
    Fail "Expected output missing: $Built"
}

if (Test-Path $OutExe) { Remove-Item $OutExe -Force }
Move-Item -Path $Built -Destination $OutExe

Banner "SHA256"
$hash = Get-FileHash -Path $OutExe -Algorithm SHA256
("$($hash.Hash.ToLowerInvariant())  $(Split-Path $OutExe -Leaf)" |
    Out-File -Encoding ascii ($OutExe + ".sha256"))

Write-Host ""
Write-Host ("Built: {0} ({1:N0} bytes)" -f $OutExe, (Get-Item $OutExe).Length) -ForegroundColor Green
