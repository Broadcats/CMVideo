#!/usr/bin/env bash
# Build a self-contained CMVideo AppImage for Linux x86_64.
#
# Output: dist/CMVideo-<version>-x86_64.AppImage  (+ .sha256)
# The AppImage bundles Python, the app, ffmpeg, ffprobe and espeak-ng.
# Whisper model weights are downloaded on demand at first transcribe.
#
# CPU-only build: CUDA wheels are stripped to keep the image under ~250 MB.
# Users with an NVIDIA GPU can drop CUDA libs next to the AppImage if they
# want GPU transcription (out of scope here).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

VERSION="$(grep -oP 'APP_VERSION\s*=\s*"\K[^"]+' censor/version.py)"
ARCH="x86_64"
APPIMAGE_NAME="CMVideo-${VERSION}-${ARCH}.AppImage"

DIST="$HERE/dist"
BUILD="$HERE/build/appimage"
APPDIR="$BUILD/AppDir"
TOOLS="$HERE/build/tools"
VENV="$HERE/.venv-appimage"

mkdir -p "$DIST" "$BUILD" "$TOOLS"

note() { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 0. Host sanity
# ---------------------------------------------------------------------------
note "Host check"
command -v python3 >/dev/null || die "python3 missing"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
    || die "Need Python >= 3.10 to bundle (found $(python3 -V))"
for tool in ffmpeg ffprobe espeak-ng wget; do
    command -v "$tool" >/dev/null \
        || die "Missing host tool '$tool'. Install it: sudo apt install ffmpeg espeak-ng wget"
done
python3 -c 'import tkinter' 2>/dev/null \
    || die "python3-tk missing. Install it: sudo apt install python3-tk"

# ---------------------------------------------------------------------------
# 1. Build venv (clean, CPU-only)
# ---------------------------------------------------------------------------
note "Build venv at $VENV"
if [ ! -x "$VENV/bin/python" ]; then
    python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip wheel --quiet
python -m pip install --quiet pyinstaller
python -m pip install --quiet -r requirements.txt pillow

# Strip CUDA wheels pulled in transitively by ctranslate2. The CPU code path
# in ctranslate2 doesn't need them; removing them saves ~1.3 GB in the bundle.
python -m pip uninstall -y nvidia-cublas-cu12 nvidia-cudnn-cu12 \
    nvidia-cuda-nvrtc-cu12 2>/dev/null || true

# ---------------------------------------------------------------------------
# 2. PyInstaller bundle
# ---------------------------------------------------------------------------
note "PyInstaller (onedir, windowed)"
rm -rf "$BUILD/pyinstaller"
pyinstaller \
    --noconfirm --clean \
    --name cmvideo \
    --windowed \
    --paths "$HERE" \
    --collect-all tkinterdnd2 \
    --collect-all faster_whisper \
    --collect-all yt_dlp \
    --collect-all ctranslate2 \
    --collect-all onnxruntime \
    --collect-all av \
    --collect-submodules censor \
    --add-data "$HERE/wordlists:wordlists" \
    --add-data "$HERE/icon.png:." \
    --add-data "$HERE/icon-32.png:." \
    --add-data "$HERE/icon-64.png:." \
    --add-data "$HERE/icon-128.png:." \
    --add-data "$HERE/icon.svg:." \
    --distpath "$BUILD/pyinstaller/dist" \
    --workpath "$BUILD/pyinstaller/work" \
    --specpath "$BUILD/pyinstaller" \
    "$HERE/app.py"

[ -d "$BUILD/pyinstaller/dist/cmvideo" ] || die "PyInstaller output missing"

# ---------------------------------------------------------------------------
# 3. AppDir scaffold
# ---------------------------------------------------------------------------
note "AppDir scaffold"
rm -rf "$APPDIR"
mkdir -p \
    "$APPDIR/usr/bin" \
    "$APPDIR/usr/share/applications" \
    "$APPDIR/usr/share/icons/hicolor/256x256/apps" \
    "$APPDIR/opt/cmvideo"

cp -a "$BUILD/pyinstaller/dist/cmvideo/." "$APPDIR/opt/cmvideo/"

# System binaries we shell out to. linuxdeploy will sweep their .so deps.
cp "$(command -v ffmpeg)"    "$APPDIR/usr/bin/ffmpeg"
cp "$(command -v ffprobe)"   "$APPDIR/usr/bin/ffprobe"
cp "$(command -v espeak-ng)" "$APPDIR/usr/bin/espeak-ng"

# espeak voice / phoneme data (path varies by distro)
ESPEAK_DATA=""
for cand in \
    /usr/share/espeak-ng-data \
    /usr/lib/x86_64-linux-gnu/espeak-ng-data \
    /usr/local/share/espeak-ng-data ; do
    [ -d "$cand" ] && ESPEAK_DATA="$cand" && break
done
[ -n "$ESPEAK_DATA" ] || warn "espeak-ng-data not found; fun TTS may break"
if [ -n "$ESPEAK_DATA" ]; then
    mkdir -p "$APPDIR/usr/share/espeak-ng-data"
    cp -a "$ESPEAK_DATA/." "$APPDIR/usr/share/espeak-ng-data/"
fi

# Icons (both top-level and hicolor for appimaged integration)
cp icon-128.png "$APPDIR/cmvideo.png"
cp icon-128.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/cmvideo.png"
cp icon.svg     "$APPDIR/usr/share/icons/hicolor/256x256/apps/cmvideo.svg" || true

# AppRun: put bundled binaries first on PATH, point espeak-ng at its data.
cat > "$APPDIR/AppRun" <<'APPRUN_EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
export PATH="$HERE/usr/bin:$PATH"
export ESPEAK_DATA_PATH="$HERE/usr/share/espeak-ng-data"
export LD_LIBRARY_PATH="$HERE/usr/lib:${LD_LIBRARY_PATH:-}"
exec "$HERE/opt/cmvideo/cmvideo" "$@"
APPRUN_EOF
chmod +x "$APPDIR/AppRun"

# .desktop (both at AppDir root and in usr/share/applications)
cat > "$APPDIR/cmvideo.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=CMVideo
GenericName=Video Censor
Comment=Remove swears and slurs from video or audio
Exec=cmvideo
Icon=cmvideo
Terminal=false
Categories=AudioVideo;
StartupNotify=true
StartupWMClass=CMVideo
EOF
cp "$APPDIR/cmvideo.desktop" "$APPDIR/usr/share/applications/cmvideo.desktop"

# ---------------------------------------------------------------------------
# 4. linuxdeploy: sweep .so deps + emit AppImage
# ---------------------------------------------------------------------------
note "Fetch linuxdeploy if missing"
LD="$TOOLS/linuxdeploy"
if [ ! -x "$LD" ]; then
    wget -qO "$LD" \
        "https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage"
    chmod +x "$LD"
fi

note "linuxdeploy --output appimage"
cd "$BUILD"
# Force the AppImage arch — some bundled Python wheels ship multi-arch .so
# files (e.g. onnxruntime provider libs) which confuses appimagetool's
# auto-detection.
ARCH="$ARCH" \
LINUXDEPLOY_OUTPUT_VERSION="$VERSION" \
"$LD" \
    --appdir "$APPDIR" \
    --executable "$APPDIR/usr/bin/ffmpeg" \
    --executable "$APPDIR/usr/bin/ffprobe" \
    --executable "$APPDIR/usr/bin/espeak-ng" \
    --output appimage

# linuxdeploy names it CMVideo-<VERSION>-x86_64.AppImage in CWD.
SRC="$BUILD/CMVideo-${VERSION}-${ARCH}.AppImage"
if [ ! -f "$SRC" ]; then
    # Fall back: glob and pick the only one.
    SRC="$(ls "$BUILD"/*.AppImage 2>/dev/null | head -1)"
fi
[ -f "$SRC" ] || die "AppImage not produced"
mv -f "$SRC" "$DIST/$APPIMAGE_NAME"

# ---------------------------------------------------------------------------
# 5. Checksum + summary
# ---------------------------------------------------------------------------
cd "$DIST"
sha256sum "$APPIMAGE_NAME" > "${APPIMAGE_NAME}.sha256"

SIZE_H="$(du -h "$APPIMAGE_NAME" | awk '{print $1}')"
note "Built: $DIST/$APPIMAGE_NAME ($SIZE_H)"
sha256sum "$APPIMAGE_NAME"
