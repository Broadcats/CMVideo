#!/usr/bin/env bash
# Install "CMVideo" on Linux.
#
# Detects the distro and installs the system packages CMVideo needs
# (ffmpeg, espeak-ng, python3-tk, python3-venv) via the appropriate
# package manager. Then registers a .desktop entry in the applications
# menu and on the Desktop.
#
# Re-running this is safe: missing packages are added, existing ones
# left alone.
#
# Supported package managers: apt (Debian/Ubuntu/Mint), dnf (Fedora/RHEL),
# pacman (Arch/Manjaro), zypper (openSUSE).

set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="cmvideo"
APP_NAME="CMVideo"
APP_COMMENT="Remove swears and slurs from video or audio"

APPS_DIR="$HOME/.local/share/applications"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"

# ----- pretty output -----
banner() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
info()   { printf '    %s\n' "$*"; }
ok()     { printf '\033[1;32m    %s\033[0m\n' "$*"; }
warn()   { printf '\033[1;33m    %s\033[0m\n' "$*"; }
fail()   { printf '\033[1;31m    %s\033[0m\n' "$*" >&2; exit 1; }

# ----- distro detection -----
banner "Detecting Linux distribution"
PM=""
if   command -v apt-get >/dev/null 2>&1;  then PM="apt"
elif command -v dnf     >/dev/null 2>&1;  then PM="dnf"
elif command -v pacman  >/dev/null 2>&1;  then PM="pacman"
elif command -v zypper  >/dev/null 2>&1;  then PM="zypper"
fi
if [ -z "$PM" ]; then
    warn "Couldn't detect apt/dnf/pacman/zypper. You'll need to install"
    warn "these packages yourself before launching the app:"
    warn "  ffmpeg, python3-tk (or python3-tkinter), python3-venv, espeak-ng"
else
    ok "Using package manager: $PM"
fi

# ----- system packages -----
# Per-distro package names for the same logical dependency.
case "$PM" in
    apt)    PKGS=(ffmpeg python3-tk python3-venv espeak-ng) ;;
    dnf)    PKGS=(ffmpeg python3-tkinter espeak-ng) ;;     # venv ships with python3
    pacman) PKGS=(ffmpeg tk espeak-ng) ;;
    zypper) PKGS=(ffmpeg python3-tk espeak-ng) ;;
    *)      PKGS=() ;;
esac

# Skip packages that are already installed so we don't ask for sudo
# unnecessarily and so the prompt below lists only what's actually new.
missing=()
for pkg in "${PKGS[@]}"; do
    case "$PM" in
        apt)
            dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg") ;;
        dnf)
            rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg") ;;
        pacman)
            pacman -Qq "$pkg" >/dev/null 2>&1 || missing+=("$pkg") ;;
        zypper)
            rpm -q "$pkg" >/dev/null 2>&1 || missing+=("$pkg") ;;
    esac
done

if [ "${#missing[@]}" -gt 0 ]; then
    banner "Installing system packages: ${missing[*]}"
    info "This needs your sudo password."
    case "$PM" in
        apt)    sudo apt-get update && sudo apt-get install -y "${missing[@]}" ;;
        dnf)    sudo dnf install -y "${missing[@]}" ;;
        pacman) sudo pacman -S --noconfirm --needed "${missing[@]}" ;;
        zypper) sudo zypper install -y "${missing[@]}" ;;
    esac
    ok "System packages installed"
elif [ "${#PKGS[@]}" -gt 0 ]; then
    ok "All required system packages already installed"
fi

# ----- ensure helper scripts are executable -----
chmod +x "$HERE/run.sh"
chmod +x "$HERE/app.py" 2>/dev/null || true
[ -f "$HERE/enable-gpu.sh" ] && chmod +x "$HERE/enable-gpu.sh"
[ -f "$HERE/uninstall.sh" ]  && chmod +x "$HERE/uninstall.sh"

# ----- clean up legacy "Clean My Video" entries -----
LEGACY_APPS=("clean-my-video.desktop")
LEGACY_DESKTOP=("Clean My Video.desktop")
for f in "${LEGACY_APPS[@]}"; do
    [ -f "$APPS_DIR/$f" ] && rm -f "$APPS_DIR/$f"
done
for f in "${LEGACY_DESKTOP[@]}"; do
    [ -f "$DESKTOP_DIR/$f" ] && rm -f "$DESKTOP_DIR/$f"
done

# ----- 1) Apps menu entry -----
banner "Registering app launcher"
mkdir -p "$APPS_DIR"
APPS_FILE="$APPS_DIR/$APP_ID.desktop"

cat > "$APPS_FILE" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=$APP_NAME
GenericName=Video Censor
Comment=$APP_COMMENT
Exec="$HERE/run.sh"
Icon=$HERE/icon.svg
Terminal=false
Categories=AudioVideo;
StartupNotify=true
StartupWMClass=CMVideo
EOF

chmod +x "$APPS_FILE"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

# ----- 2) Desktop shortcut -----
if [ -d "$DESKTOP_DIR" ]; then
    DESKTOP_FILE="$DESKTOP_DIR/$APP_NAME.desktop"
    cp "$APPS_FILE" "$DESKTOP_FILE"
    chmod +x "$DESKTOP_FILE"
    if command -v gio >/dev/null 2>&1; then
        gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
    fi
fi

echo
ok "Installed!"
echo
info "'$APP_NAME' is now in your applications menu."
if [ -d "$DESKTOP_DIR" ]; then
    info "A clickable shortcut was placed on your Desktop:"
    info "  $DESKTOP_DIR/$APP_NAME.desktop"
    info "If double-clicking does nothing on GNOME, right-click and choose 'Allow Launching'."
fi
echo
info "First launch creates a local .venv/ and downloads the Python deps"
info "(faster-whisper, yt-dlp, etc). A progress dialog appears - that step"
info "takes 1-3 minutes the first time, then launches are instant."
echo
info "Remove later with:  ./uninstall.sh"
