"""User-installable yt-dlp plugin discovery.

CMVideo ships no third-party site extractors. Instead it registers a
per-user plugin folder with yt-dlp's plugin loader; any community
extractor dropped into that folder is picked up at download time.

Layout (mirrors yt-dlp's standard convention):

    ~/.config/cmvideo/plugins/
    +-- README.txt
    +-- <package>/
        +-- yt_dlp_plugins/
            +-- extractor/<file>.py
            +-- postprocessor/<file>.py

The outer `<package>` wrapper is required - yt-dlp recurses children
of the plugin folder expecting each to be a namespace-package root.
"""

from __future__ import annotations

from pathlib import Path

from .config_store import config_dir


_PLUGIN_README = """\
CMVideo / yt-dlp user plugins folder
====================================

Drop yt-dlp community extractors in here to add sites CMVideo doesn't
handle out of the box.

Layout
------

Two valid shapes:

  a. A GitHub repo that already matches:

       <repo>/yt_dlp_plugins/extractor/<file>.py

     Clone or unzip directly into this folder. Result:

       <this folder>/<repo>/yt_dlp_plugins/extractor/<file>.py

  b. A standalone .py extractor. Wrap it manually:

       <this folder>/myplugin/yt_dlp_plugins/extractor/<file>.py

Restart CMVideo to pick up newly added plugins.

Warnings
--------

* A malicious plugin runs arbitrary Python under your account. Only
  install from sources you trust.
* DRM-protected streams (Netflix, Disney+, paid Pornhub Premium,
  Brazzers, etc.) refuse to decrypt regardless of plugin. Screen-record
  with OBS and feed the MP4 into CMVideo instead.
* Plugins must be Python source, not compiled binaries.

Reference: https://github.com/yt-dlp/yt-dlp/wiki/Plugins
"""


def user_plugin_dir() -> Path:
    """Return the per-user plugin folder. Doesn't create it."""
    return config_dir() / "plugins"


def ensure_user_plugin_dir() -> Path:
    """Create the plugin folder (idempotent) and write a README the
    first time. Returns the folder path on success.

    Failures are swallowed silently - persistence is best-effort and
    must never block the app from launching or downloading.
    """
    p = user_plugin_dir()
    try:
        p.mkdir(parents=True, exist_ok=True)
        readme = p / "README.txt"
        if not readme.is_file():
            readme.write_text(_PLUGIN_README, encoding="utf-8")
    except OSError:
        pass
    return p


def _is_safely_owned(p: Path) -> bool:
    """POSIX-only: refuse to load plugins from a folder that any other
    local user can write to. Catches the "shared workstation" case
    where someone could drop a malicious extractor into your config
    dir before you launched the app.

    Always returns True on Windows - NTFS uses ACLs we can't reason
    about cheaply, and the typical Windows user-profile install path
    is already user-scoped.
    """
    import os
    import sys
    if sys.platform.startswith("win"):
        return True
    try:
        st = p.stat()
    except OSError:
        return False
    # Owner must be us. Bail out if we can't determine our uid.
    try:
        if st.st_uid != os.getuid():
            return False
    except AttributeError:  # extremely-stripped POSIX
        return True
    # World- or group-writable? Reject. We allow group-writable only
    # if the group is the user's primary group AND the directory has
    # the sticky bit set, but in practice the simpler test below is
    # enough for desktop installs.
    mode = st.st_mode
    if mode & 0o002:           # world-writable
        return False
    if mode & 0o020:           # group-writable
        return False
    return True


def register_with_yt_dlp() -> bool:
    """Add the plugin folder to yt-dlp's search path and load plugins.

    Idempotent. Returns True if registration ran (folder + yt-dlp both
    available); False if yt-dlp isn't importable. A True return doesn't
    imply any plugins were loaded - that only happens if files exist.

    Refuses to load plugins from a folder that's writable by other
    local users (POSIX). Plugins run as arbitrary Python under our
    account, so a world-writable plugin dir is a privilege-escalation
    primitive: any local user could drop a file into it and have it
    execute the next time the app starts.
    """
    p = ensure_user_plugin_dir()
    if not p.is_dir():
        return False
    if not _is_safely_owned(p):
        import logging
        logging.getLogger(__name__).warning(
            "Refusing to load plugins from %s - directory is writable by "
            "other users. Run `chmod 700 %s` to enable plugin loading.",
            p, p,
        )
        return False
    try:
        from yt_dlp.globals import plugin_dirs  # type: ignore[import-not-found]
        from yt_dlp.plugins import load_all_plugins  # type: ignore[import-not-found]
    except ImportError:
        return False
    target = str(p)
    current = list(plugin_dirs.value)
    if target in [str(x) for x in current]:
        return True
    new_list = list(current)
    if "default" not in [str(x) for x in new_list]:
        new_list.insert(0, "default")
    new_list.append(target)
    plugin_dirs.value = new_list
    try:
        load_all_plugins()
    except Exception:  # noqa: BLE001 - plugin errors must not crash the app.
        pass
    return True
