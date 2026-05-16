"""Multi-extractor fallback chain for URL downloads.

We treat yt-dlp as the universal default and layer two further tools
behind it so the user has more than one shot at any given site:

    1) yt-dlp     - 1800+ extractors, the universal pick
    2) gallery-dl - hand-written Python extractors that beat yt-dlp on
                    Twitter/X, Reddit, Tumblr, Pinterest and a handful
                    of other social-media short videos
    3) Cobalt API - https://api.cobalt.tools, hand-tuned per-site
                    extractors with their own residential IP pool;
                    materially better than yt-dlp on Twitter/X, Reddit,
                    Bilibili, Bluesky, OK.ru
    4) streamlink - live-stream specialist (Twitch live, YouTube live,
                    Kick live). Only invoked when the URL looks like a
                    live channel rather than a VOD.

Each adapter exposes a uniform `download(url, dst_dir, ...)` that
returns a Path to the saved file plus a metadata dict. The dispatcher
in `extract_with_fallbacks` iterates the chain until one succeeds, then
returns its result and a log of which tool actually won.

Tools further down the chain are *only* invoked when the previous one
fails with a "recoverable" error (an extractor / parser bug, a 4xx
that isn't a clean 404). True 404 / DRM / login-required errors are
considered terminal and short-circuit the chain.

This module is deliberately stdlib + tool-CLIs, so the same code
runs in the desktop app and in the (containerised) web mini.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

# Which sites each non-yt-dlp tool *actually* covers. The dispatcher
# uses these to skip a tool that has no chance of helping with a given
# URL, so we don't pay a tool-startup tax on every fallback.

GALLERY_DL_DOMAINS = {
    "twitter.com", "x.com", "mobile.twitter.com",
    "reddit.com", "www.reddit.com", "old.reddit.com", "i.redd.it", "v.redd.it",
    "tumblr.com",
    "pinterest.com", "www.pinterest.com", "pin.it",
    "instagram.com", "www.instagram.com",
    "bsky.app",
    "imgur.com", "i.imgur.com",
    "tiktok.com", "www.tiktok.com",
    "vk.com",
}

# Source: https://github.com/imputnet/cobalt-api/blob/main/api.cobalt.tools/services
COBALT_DOMAINS = {
    "twitter.com", "x.com",
    "reddit.com", "www.reddit.com", "old.reddit.com",
    "tiktok.com", "www.tiktok.com",
    "instagram.com", "www.instagram.com",
    "tumblr.com",
    "pinterest.com", "pin.it",
    "bsky.app",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv", "www.twitch.tv",   # clips only
    "soundcloud.com",
    "bilibili.com", "www.bilibili.com",
    "ok.ru",
    "vk.com",
    "loom.com",
    "streamable.com",
    "rutube.ru",
}

# URLs that look like live streams - streamlink is the specialist here.
LIVE_URL_PATTERNS = (
    re.compile(r"^https?://(www\.)?twitch\.tv/[^/]+/?(\?|$)", re.I),
    re.compile(r"^https?://(www\.)?youtube\.com/(@[^/]+/live|.*/live)", re.I),
    re.compile(r"^https?://(www\.)?youtu\.be/live/", re.I),
    re.compile(r"^https?://(www\.)?kick\.com/[^/]+/?(\?|$)", re.I),
)


# ---------------------------------------------------------------------------
# Result + dispatcher types
# ---------------------------------------------------------------------------
@dataclass
class ExtractionResult:
    path: Path
    title: str
    duration: float | None
    extractor: str         # which tool won: yt-dlp / gallery-dl / cobalt / streamlink
    site: str              # the resolved extractor or domain
    attempts: list[str] = field(default_factory=list)


@dataclass
class ExtractionError(Exception):
    message: str
    attempts: list[str] = field(default_factory=list)
    terminal: bool = False  # True = no further fallback would help

    def __str__(self) -> str:
        if self.attempts:
            return f"{self.message} (tried: {', '.join(self.attempts)})"
        return self.message


# Errors yt-dlp throws that are *not* worth retrying with another tool.
# These are real "the content isn't available" outcomes.
_TERMINAL_HINTS = (
    "private video",
    "video unavailable",
    "this video has been removed",
    "video has been removed by the user",
    "DRM-protected",
    "login required",          # gallery-dl + cobalt won't help auth
    "members-only",
    "channel does not exist",
    "this video is not available",
    "deleted",
    "does not exist",
    "404 not found",
    "410 gone",
    "geo-restricted",           # geo blocks aren't fixed by another tool
    "geo-blocked",
)

# Errors that LOOK terminal but actually mean "yt-dlp's extractor is
# wedged" - we want to fall through these.
_RECOVERABLE_HINTS = (
    "unable to download webpage",
    "unable to extract",
    "unable to parse",
    "no video formats",
    "no formats found",
    "extractor crash",
    "list indices must be integers",
    "ParseError",
    "cannot parse data",
    "json",
    "ssl",
    "rate limit",
    "rate-limit",
    "throttled",
    "tls",
    "cloudflare",
    "captcha",
)


def _is_terminal(error_text: str) -> bool:
    t = (error_text or "").lower()
    if any(h in t for h in _RECOVERABLE_HINTS):
        return False
    return any(h in t for h in _TERMINAL_HINTS)


def _domain_of(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return ""
    return host.lower()


def _looks_live(url: str) -> bool:
    return any(p.search(url) for p in LIVE_URL_PATTERNS)


# ---------------------------------------------------------------------------
# yt-dlp adapter (the existing path; this module just standardises the
# return shape so the dispatcher can treat it like the others).
# ---------------------------------------------------------------------------
def yt_dlp_download(
    url: str,
    dst_dir: Path,
    *,
    fmt: str = "mp4",
    ydl_opts: dict[str, Any] | None = None,
) -> ExtractionResult:
    """Download via yt-dlp. Raises ExtractionError on failure."""
    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
        from yt_dlp.utils import DownloadError  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ExtractionError("yt-dlp not installed", terminal=True) from exc

    opts = dict(ydl_opts or {})
    opts.setdefault("outtmpl", str(dst_dir / "%(title).180B [%(id)s].%(ext)s"))
    opts.setdefault("quiet", True)
    opts.setdefault("no_warnings", True)
    opts.setdefault("noplaylist", True)

    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            base = Path(ydl.prepare_filename(info))
    except DownloadError as exc:
        raise ExtractionError(str(exc), terminal=_is_terminal(str(exc))) from exc
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(str(exc), terminal=_is_terminal(str(exc))) from exc

    candidate = base
    if not candidate.exists() and fmt:
        alt = base.with_suffix("." + fmt)
        if alt.exists():
            candidate = alt
    if not candidate.exists():
        # FFmpegExtractAudio / FFmpegVideoRemuxer rewrites the suffix.
        siblings = sorted(dst_dir.glob(base.stem + ".*"))
        if siblings:
            candidate = max(siblings, key=lambda p: p.stat().st_size)

    if not candidate.exists():
        raise ExtractionError(f"Download finished but file is missing at {base}")

    return ExtractionResult(
        path=candidate,
        title=str(info.get("title") or "Untitled")[:200],
        duration=float(info["duration"]) if isinstance(info.get("duration"), (int, float)) else None,
        extractor="yt-dlp",
        site=str(info.get("extractor_key") or _domain_of(url)),
    )


# ---------------------------------------------------------------------------
# gallery-dl adapter
# ---------------------------------------------------------------------------
def _resolve_tool(name: str) -> str | None:
    """Find a tool on PATH or in the active interpreter's bin/Scripts dir."""
    found = shutil.which(name)
    if found:
        return found
    import sys as _sys
    bindir = Path(_sys.executable).parent
    for candidate in (bindir / name, bindir / f"{name}.exe", bindir / "Scripts" / name, bindir / "Scripts" / f"{name}.exe"):
        if candidate.exists():
            return str(candidate)
    return None


def gallery_dl_available() -> bool:
    return _resolve_tool("gallery-dl") is not None


def gallery_dl_download(
    url: str,
    dst_dir: Path,
    *,
    timeout: int = 180,
) -> ExtractionResult:
    """Download via gallery-dl. Raises ExtractionError on failure."""
    if not gallery_dl_available():
        raise ExtractionError("gallery-dl not installed", terminal=True)

    bin_path = _resolve_tool("gallery-dl") or "gallery-dl"
    cmd = [
        bin_path,
        "--quiet",
        "--no-mtime",
        "--directory", str(dst_dir),
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(
            f"gallery-dl timed out after {timeout}s",
            terminal=False,
        ) from exc

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        msg = err[-1] if err else f"gallery-dl exited {proc.returncode}"
        raise ExtractionError(msg, terminal=_is_terminal(msg))

    # gallery-dl writes one or more files under dst_dir. Grab the most
    # recently-modified one as the primary asset; multi-asset URLs are
    # uncommon for the social-media domains we route here.
    files = sorted(
        (p for p in dst_dir.rglob("*") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        raise ExtractionError("gallery-dl produced no output file")
    primary = files[-1]

    return ExtractionResult(
        path=primary,
        title=primary.stem[:200],
        duration=None,
        extractor="gallery-dl",
        site=_domain_of(url),
    )


# ---------------------------------------------------------------------------
# Cobalt API adapter
# ---------------------------------------------------------------------------
# Cobalt v10+ (Nov 2024) requires JWT auth on its public API. The old
# `api.cobalt.tools/api/json` endpoint was retired. To use Cobalt you
# now either self-host a Cobalt instance (one Docker container, see
# https://github.com/imputnet/cobalt) or get a token from a community-
# run instance.
#
# We read the instance URL + optional API key from env so the same
# code path works for self-hosted or third-party endpoints. If the
# env vars are unset the adapter skips itself with a clean log line
# rather than failing every call.
import os as _os
COBALT_API_BASE = _os.environ.get("COBALT_API_BASE", "").strip()
COBALT_API_KEY = _os.environ.get("COBALT_API_KEY", "").strip()
COBALT_USER_AGENT = "CMVideo/1.0 (+https://cmvideo.online)"


def cobalt_available() -> bool:
    """True if a Cobalt instance is configured. The public api.cobalt.tools
    endpoint is rejected unless an API key is supplied because it now
    requires JWT auth and an anonymous call is a guaranteed 400."""
    if not COBALT_API_BASE:
        return False
    if "cobalt.tools" in COBALT_API_BASE and not COBALT_API_KEY:
        return False
    return True


def cobalt_download(
    url: str,
    dst_dir: Path,
    *,
    fmt: str = "mp4",
    timeout: int = 180,
    api_base: str | None = None,
    api_key: str | None = None,
) -> ExtractionResult:
    """Resolve a media URL through a Cobalt instance and stream it to
    disk. Cobalt v10+ takes a POST to the instance root and returns
    either a `redirect` (direct CDN URL) or a `tunnel` URL which we
    then fetch ourselves.

    Configuration:
      COBALT_API_BASE  - required, e.g. https://your-cobalt.fly.dev
      COBALT_API_KEY   - optional, sent as `Authorization: Api-Key <k>`
    """
    base = (api_base or COBALT_API_BASE).rstrip("/")
    key = (api_key if api_key is not None else COBALT_API_KEY)
    if not base:
        raise ExtractionError(
            "Cobalt is not configured (set COBALT_API_BASE to a self-hosted instance)",
            terminal=True,
        )

    body = json.dumps({
        "url": url,
        "videoQuality": "720",
        "audioFormat": "mp3" if fmt == "mp3" else "best",
        "downloadMode": "audio" if fmt == "mp3" else "auto",
    }).encode("utf-8")

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": COBALT_USER_AGENT,
    }
    if key:
        headers["Authorization"] = f"Api-Key {key}"

    req = urllib.request.Request(base, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"Cobalt API error: {exc}") from exc

    status = (payload.get("status") or "").lower()
    if status in {"error", "rate-limit"}:
        err = payload.get("error") or payload.get("text") or "Cobalt declined this URL"
        msg = err.get("code") if isinstance(err, dict) else str(err)
        raise ExtractionError(f"Cobalt: {msg}", terminal=_is_terminal(str(msg)))
    if status not in {"redirect", "tunnel", "stream", "success"}:
        # picker mode = multi-asset gallery, not what we want here
        raise ExtractionError(f"Cobalt returned unsupported status: {status or payload}")

    media_url = payload.get("url")
    if not media_url:
        raise ExtractionError("Cobalt response missing url field")

    # Filename from response or fall back to the resolved path.
    suggested_name = payload.get("filename") or "cobalt-download"
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", suggested_name)[:180] or "cobalt"
    if not safe_name.lower().endswith(("." + fmt,)):
        safe_name = f"{safe_name}.{fmt}"
    dst = dst_dir / safe_name

    fetch = urllib.request.Request(
        media_url,
        headers={"User-Agent": COBALT_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(fetch, timeout=timeout) as resp, dst.open("wb") as out:
            shutil.copyfileobj(resp, out, length=1 << 20)
    except Exception as exc:  # noqa: BLE001
        raise ExtractionError(f"Cobalt media fetch failed: {exc}") from exc

    if not dst.exists() or dst.stat().st_size == 0:
        raise ExtractionError("Cobalt produced an empty file")

    return ExtractionResult(
        path=dst,
        title=safe_name,
        duration=None,
        extractor="cobalt",
        site=_domain_of(url),
    )


# ---------------------------------------------------------------------------
# streamlink adapter (live streams only)
# ---------------------------------------------------------------------------
def streamlink_available() -> bool:
    return _resolve_tool("streamlink") is not None


def streamlink_download(
    url: str,
    dst_dir: Path,
    *,
    fmt: str = "mp4",
    duration: int = 600,
    timeout: int = 720,
) -> ExtractionResult:
    """Capture `duration` seconds of a live stream via streamlink+ffmpeg."""
    if not streamlink_available():
        raise ExtractionError("streamlink not installed", terminal=True)

    out_file = dst_dir / f"livecap.{fmt}"
    # Pipe streamlink through ffmpeg to enforce the duration cap and
    # remux to the target container. `--hls-duration` is honoured by
    # streamlink for HLS streams, which is what Twitch / Kick / YT-live
    # all serve.
    bin_path = _resolve_tool("streamlink") or "streamlink"
    cmd = [
        bin_path,
        "--quiet",
        "--hls-duration", str(duration),
        "-O",
        url,
        "best",
    ]
    ff = [
        "ffmpeg",
        "-loglevel", "error",
        "-y",
        "-i", "pipe:0",
        "-t", str(duration),
        "-c", "copy",
        str(out_file),
    ]
    try:
        sl = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ffp = subprocess.Popen(ff, stdin=sl.stdout, stderr=subprocess.PIPE)
        if sl.stdout:
            sl.stdout.close()
        sl_err = b""
        try:
            ffp.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            ffp.kill()
            sl.kill()
            raise ExtractionError(f"streamlink+ffmpeg timed out after {timeout}s")
        sl_err = (sl.stderr.read() if sl.stderr else b"")
        sl.wait(timeout=10)
        if ffp.returncode != 0 or not out_file.exists() or out_file.stat().st_size == 0:
            err = sl_err.decode("utf-8", errors="replace")[-400:].strip() or "streamlink failed"
            raise ExtractionError(err, terminal=_is_terminal(err))
    except FileNotFoundError as exc:
        raise ExtractionError("ffmpeg not on PATH", terminal=True) from exc

    return ExtractionResult(
        path=out_file,
        title=f"Live capture ({duration}s)",
        duration=float(duration),
        extractor="streamlink",
        site=_domain_of(url),
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------
def extract_with_fallbacks(
    url: str,
    dst_dir: Path,
    *,
    fmt: str = "mp4",
    ydl_opts: dict[str, Any] | None = None,
    enabled: Iterable[str] = ("yt-dlp", "gallery-dl", "cobalt", "streamlink"),
    on_attempt: Callable[[str, str], None] | None = None,
) -> ExtractionResult:
    """Try each tool in order until one wins. Raises ExtractionError if
    every applicable tool is exhausted."""
    enabled_set = {e.lower() for e in enabled}
    domain = _domain_of(url)
    is_live = _looks_live(url)
    attempts: list[str] = []
    last_error: ExtractionError | None = None

    def _emit(tool: str, msg: str) -> None:
        attempts.append(f"{tool}: {msg[:120]}")
        if on_attempt:
            try:
                on_attempt(tool, msg)
            except Exception:  # noqa: BLE001
                pass

    if is_live and "streamlink" in enabled_set:
        try:
            r = streamlink_download(url, dst_dir, fmt=fmt)
            r.attempts = attempts + [f"streamlink: ok"]
            return r
        except ExtractionError as exc:
            _emit("streamlink", exc.message)
            last_error = exc
            if exc.terminal:
                raise ExtractionError(exc.message, attempts=attempts, terminal=True) from exc

    if "yt-dlp" in enabled_set:
        try:
            r = yt_dlp_download(url, dst_dir, fmt=fmt, ydl_opts=ydl_opts)
            r.attempts = attempts + [f"yt-dlp: ok"]
            return r
        except ExtractionError as exc:
            _emit("yt-dlp", exc.message)
            last_error = exc
            if exc.terminal:
                raise ExtractionError(exc.message, attempts=attempts, terminal=True) from exc

    if "gallery-dl" in enabled_set and (not domain or domain in GALLERY_DL_DOMAINS or _ends_with_any(domain, GALLERY_DL_DOMAINS)):
        try:
            r = gallery_dl_download(url, dst_dir)
            r.attempts = attempts + [f"gallery-dl: ok"]
            return r
        except ExtractionError as exc:
            _emit("gallery-dl", exc.message)
            last_error = exc
            if exc.terminal:
                pass  # gallery-dl's "terminal" hints aren't authoritative for non-yt-dlp tools

    if (
        "cobalt" in enabled_set
        and cobalt_available()
        and (not domain or domain in COBALT_DOMAINS or _ends_with_any(domain, COBALT_DOMAINS))
    ):
        try:
            r = cobalt_download(url, dst_dir, fmt=fmt)
            r.attempts = attempts + [f"cobalt: ok"]
            return r
        except ExtractionError as exc:
            _emit("cobalt", exc.message)
            last_error = exc

    msg = "All extractors failed."
    if last_error is not None:
        msg = f"{msg} Last error: {last_error.message}"
    raise ExtractionError(msg, attempts=attempts)


def _ends_with_any(host: str, suffixes: Iterable[str]) -> bool:
    """`x.com` matches `x.com`, `mobile.twitter.com` matches `twitter.com`."""
    for s in suffixes:
        if host == s or host.endswith("." + s):
            return True
    return False


# ---------------------------------------------------------------------------
# Diagnostic helpers (called by the desktop "About" panel and the mini
# /api/limits endpoint).
# ---------------------------------------------------------------------------
def available_tools() -> dict[str, bool]:
    """Map of tool name -> is-actually-usable. Cobalt only counts as
    available when an instance has been configured via env."""
    out = {
        "yt-dlp": False,
        "gallery-dl": gallery_dl_available(),
        "cobalt": cobalt_available(),
        "streamlink": streamlink_available(),
    }
    try:
        import yt_dlp  # noqa: F401
        out["yt-dlp"] = True
    except ImportError:
        pass
    return out


def tool_versions() -> dict[str, str]:
    """Best-effort version probe for diagnostics."""
    versions: dict[str, str] = {}
    try:
        import yt_dlp
        versions["yt-dlp"] = getattr(yt_dlp, "__version__", "unknown")
    except ImportError:
        pass
    gd_bin = _resolve_tool("gallery-dl")
    if gd_bin:
        try:
            r = subprocess.run([gd_bin, "--version"], capture_output=True, text=True, timeout=5)
            versions["gallery-dl"] = (r.stdout or r.stderr).strip().splitlines()[0]
        except Exception:  # noqa: BLE001
            versions["gallery-dl"] = "installed"
    sl_bin = _resolve_tool("streamlink")
    if sl_bin:
        try:
            r = subprocess.run([sl_bin, "--version"], capture_output=True, text=True, timeout=5)
            versions["streamlink"] = (r.stdout or r.stderr).strip().splitlines()[0]
        except Exception:  # noqa: BLE001
            versions["streamlink"] = "installed"
    if cobalt_available():
        versions["cobalt"] = COBALT_API_BASE
    else:
        versions["cobalt"] = "not configured (set COBALT_API_BASE)"
    return versions
