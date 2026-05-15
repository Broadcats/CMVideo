#!/usr/bin/env bash
# Install the CUDA runtime libraries needed by faster-whisper for GPU
# acceleration. The user already needs an NVIDIA GPU and a working driver
# (run `nvidia-smi` to check).
#
# This adds ~1 GB to the venv (cuBLAS + cuDNN wheels) but typically makes
# transcription 10-50x faster than CPU.

set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "Warning: nvidia-smi not found. You don't seem to have an NVIDIA"
    echo "GPU driver installed. GPU acceleration won't work even after this."
    read -rp "Continue anyway? [y/N] " yn
    [[ "$yn" =~ ^[Yy]$ ]] || exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Error: .venv/ not found. Run ./run.sh once first to create the venv."
    exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Installing CUDA runtime libraries (cuBLAS + cuDNN)..."
echo "This downloads ~1 GB."
pip install --upgrade \
    "nvidia-cublas-cu12" \
    "nvidia-cudnn-cu12"

echo
echo "GPU acceleration enabled. Launch the app and transcription should"
echo "automatically use CUDA. If it still falls back to CPU, run nvidia-smi"
echo "to confirm your driver and GPU are healthy."
