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


def _harden_windows_acl(target: Path) -> None:
    """Restrict the Windows ACL on `target` to the current user only.

    On POSIX we already create the file with mode 0o600, which is the
    canonical "owner-only" pattern. Windows uses ACLs instead of mode
    bits, and Python's `os.open(..., 0o600)` is a no-op on Windows -
    so without this helper a config.json with an ElevenLabs API key
    inherits whatever ACL `%APPDATA%\\CMVideo` already had, which on
    a fresh user profile is usually "Users" group readable through
    inheritance.

    Strategy: shell out to `icacls` to remove inheritance and grant
    full control to the current user only. If `icacls` is missing or
    fails we leave the default ACL alone (it's still under
    `%APPDATA%`, not world-readable C:\\). This is best-effort
    hardening, not a hard guarantee - swallowing failures keeps a
    config save from breaking just because the ACL change errored.
    """
    if not sys.platform.startswith("win"):
        return
    import subprocess  # local import - keeps POSIX import-time cost at zero
    user = os.environ.get("USERNAME", "")
    if not user:
        return
    try:
        # /inheritance:r removes inherited permissions, then we grant
        # the current user full control. Together that yields an ACL
        # equivalent to POSIX 0o600 (owner-only RWX).
        subprocess.run(
            ["icacls", str(target), "/inheritance:r", "/grant", f"{user}:(F)"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # icacls missing on stripped-down Windows installs, or the
        # call timed out; either way we don't want config save to
        # fail because of a hardening upgrade.
        pass


def save_config(data: dict) -> None:
    """Atomically write the config with strict permissions.

    Hardening: on POSIX we open the temp file with mode 0o600 *at
    creation time* (instead of writing it under the user's default
    umask and chmod'ing afterwards) so the file is never readable by
    other local users, even for the brief window between create and
    chmod. The parent dir is also clamped to 0o700 - no point making
    the file unreadable if someone can still ``ls`` the directory.

    On Windows we apply an equivalent ACL via `icacls` (remove
    inheritance, grant full control to the current user only).
    Best-effort: if the ACL call fails the config still saves.

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
        else:
            # Tighten the parent dir's ACL on Windows the same way we
            # do for the file itself. `icacls` accepts directories.
            _harden_windows_acl(d)
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
            # Apply ACL to the temp file BEFORE replacing the target,
            # so the final file inherits the tightened ACL even on
            # the (brief) window where rename hasn't happened yet.
            _harden_windows_acl(tmp)
        tmp.replace(target)
        if not is_posix:
            # Ensure the renamed-into-place file also has the
            # tightened ACL (replace() should preserve the ACL we set
            # on tmp, but we re-apply defensively in case some
            # filesystems don't propagate it).
            _harden_windows_acl(target)
    except OSError:
        pass
