#!/usr/bin/env bash
# Remove the "CMVideo" launcher entries (and any leftover legacy entries
# from older "Clean My Video" installs). Leaves the project files alone.

set -e

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
APPS_DIR="$HOME/.local/share/applications"

# Current names
APPS_FILES=(
    "$APPS_DIR/cmvideo.desktop"
    "$APPS_DIR/clean-my-video.desktop"        # legacy
)
DESKTOP_FILES=(
    "$DESKTOP_DIR/CMVideo.desktop"
    "$DESKTOP_DIR/Clean My Video.desktop"     # legacy
)

removed_any=0
for f in "${APPS_FILES[@]}" "${DESKTOP_FILES[@]}"; do
    if [ -f "$f" ]; then
        rm -f "$f"
        echo "Removed: $f"
        removed_any=1
    fi
done

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
fi

if [ "$removed_any" = "0" ]; then
    echo "Nothing to remove - no launcher entries found."
fi
echo "Done. Project files in $(dirname "$(realpath "$0")") were left intact."
