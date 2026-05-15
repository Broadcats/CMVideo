"""Cross-platform user config directory + JSON config helpers."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


APP_NAME_DISPLAY = "CMVideo"   # used on Windows/macOS
APP_NAME_UNIX = "cmvideo"      # used on Linux (lowercase XDG convention)


def config_dir() -> Path:
    """Return the per-OS config directory.

    Follows the standard conventions:
    - Linux/BSD: `$XDG_CONFIG_HOME/cmvideo` (default `~/.config/cmvideo`)
    - Windows:   `%APPDATA%\\CMVideo` (default `%USERPROFILE%\\AppData\\Roaming\\CMVideo`)
    - macOS:     `~/Library/Application Support/CMVideo`
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME_DISPLAY
        return Path.home() / "AppData" / "Roaming" / APP_NAME_DISPLAY
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME_DISPLAY
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / APP_NAME_UNIX
    return Path.home() / ".config" / APP_NAME_UNIX


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> dict:
    """Read the on-disk config. Missing or corrupt files return {}."""
    p = config_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_config(data: dict) -> None:
    """Atomically write the config (POSIX 0600). Write failures swallowed."""
    d = config_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "config.json.tmp"
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        if not sys.platform.startswith("win"):
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
        tmp.replace(config_path())
    except OSError:
        pass
