#!/usr/bin/env bash
# CMVideo - one-click launcher.
# On first run this creates a local .venv and installs requirements.
# Subsequent runs just launch the app.

set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# fail() shows a popup if we're not in a terminal, otherwise prints.
# This matters when launched from a desktop icon: terminal output is invisible.
fail() {
    local msg="$1"
    if [ -t 1 ]; then
        echo "Error: $msg" >&2
    elif command -v zenity >/dev/null 2>&1; then
        zenity --error --title="CMVideo" --width=420 --text="$msg" 2>/dev/null || true
    elif command -v kdialog >/dev/null 2>&1; then
        kdialog --error "$msg" 2>/dev/null || true
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send -u critical "CMVideo" "$msg" 2>/dev/null || true
    else
        echo "Error: $msg" >&2
    fi
    exit 1
}

# Sanity check: ffmpeg
if ! command -v ffmpeg >/dev/null 2>&1; then
    fail "ffmpeg is not installed.

Install it with:
    sudo apt install ffmpeg"
fi

# Sanity check: tkinter
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
    fail "Python's tkinter module is not installed.

Install it with:
    sudo apt install python3-tk"
fi

# Sanity check: venv
if ! python3 -c "import venv" >/dev/null 2>&1; then
    fail "Python's venv module is not installed.

Install it with:
    sudo apt install python3-venv"
fi

# Detect GPU once; if present we'll also install the CUDA runtime wheels
# during first-run setup so transcription uses the GPU automatically.
HAS_GPU="0"
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    HAS_GPU="1"
fi

# Core install steps - used by both the terminal path and the zenity path.
do_install() {
    python3 -m venv .venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    pip install --upgrade pip
    pip install -r requirements.txt
    if [ "$HAS_GPU" = "1" ]; then
        echo ">>> NVIDIA GPU detected - installing CUDA runtime libraries (~1 GB)..."
        pip install "nvidia-cublas-cu12" "nvidia-cudnn-cu12" || \
            echo "Warning: GPU libs failed to install. App will fall back to CPU."
    fi
}

# Create venv on first run, with a progress dialog if we're headless.
if [ ! -d ".venv" ]; then
    if [ -t 1 ]; then
        echo "First run: creating virtual environment and installing dependencies..."
        if [ "$HAS_GPU" = "1" ]; then
            echo "(GPU detected - will also install CUDA runtime, ~1 GB total)"
        fi
        echo "(this may take a few minutes)"
        do_install
    else
        # GUI: stream install output into a zenity progress dialog
        if command -v zenity >/dev/null 2>&1; then
            msg="Installing dependencies (this may take a few minutes)..."
            if [ "$HAS_GPU" = "1" ]; then
                msg="Installing dependencies + CUDA runtime (~1 GB, may take 5+ minutes)..."
            fi
            do_install 2>&1 | zenity --progress \
                --title="CMVideo - First-run setup" \
                --text="$msg" \
                --pulsate --auto-close --width=460 || \
                fail "Setup was cancelled or failed."
        else
            do_install
        fi
    fi
fi

# shellcheck disable=SC1091
source .venv/bin/activate

exec python app.py "$@"
