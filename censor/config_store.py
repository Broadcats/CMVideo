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
    """Atomically write the config with strict permissions.

    Hardening: on POSIX we open the temp file with mode 0o600 *at
    creation time* (instead of writing it under the user's default
    umask and chmod'ing afterwards) so the file is never readable by
    other local users, even for the brief window between create and
    chmod. The parent dir is also clamped to 0o700 - no point making
    the file unreadable if someone can still ``ls`` the directory.
    Write failures are swallowed: the app still works without a
    persisted config.
    """
    d = config_dir()
    is_posix = not sys.platform.startswith("win")
    try:
        d.mkdir(parents=True, exist_ok=True)
        if is_posix:
            try:
                os.chmod(d, 0o700)
            except OSError:
                pass
        target = config_path()
        tmp = d / "config.json.tmp"
        payload = json.dumps(data, indent=2).encode("utf-8")
        if is_posix:
            # O_CREAT|O_WRONLY|O_TRUNC + mode 0o600 means the kernel
            # creates the file with our requested mode AS LONG AS the
            # active umask doesn't strip more bits (umask only ever
            # *removes* bits, so 0o600 -> at most 0o600). Bypassing
            # `Path.write_text` lets us avoid the permissive default.
            flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
            fd = os.open(tmp, flags, 0o600)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(payload)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
        else:
            tmp.write_bytes(payload)
        tmp.replace(target)
    except OSError:
        pass
