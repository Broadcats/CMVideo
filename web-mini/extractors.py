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
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Memory guard helpers
# ---------------------------------------------------------------------------
# Playwright + Chromium is the heavyweight in this module. A headless
# tab is roughly 150-300 MB resident, plus DOM + media buffers per page.
# Two of those running at once on a 16 GB HF Space free tier still
# leaves headroom, but we'd rather refuse to start a third than OOM
# the worker mid-job. The guard below is intentionally conservative
# and deliberately lazy - psutil is optional, we fall back to reading
# /proc/meminfo on Linux (the HF case) and to a "best-effort" answer
# everywhere else.

PLAYWRIGHT_MIN_FREE_MB = int(os.environ.get("CMVIDEO_PLAYWRIGHT_MIN_FREE_MB", "600"))
PLAYWRIGHT_MAX_CONCURRENCY = int(os.environ.get("CMVIDEO_PLAYWRIGHT_MAX_CONCURRENCY", "1"))
_playwright_semaphore = threading.BoundedSemaphore(PLAYWRIGHT_MAX_CONCURRENCY)


def _free_memory_mb() -> int | None:
    """Best-effort estimate of usable free memory, in MiB. None if we
    can't tell."""
    try:
        import psutil  # type: ignore[import-not-found]
        return int(psutil.virtual_memory().available / (1024 * 1024))
    except ImportError:
        pass
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    return kb // 1024
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def memory_status() -> dict[str, Any]:
    """Diagnostic snapshot used by /api/limits."""
    free_mb = _free_memory_mb()
    return {
        "free_mb": free_mb,
        "playwright_min_free_mb": PLAYWRIGHT_MIN_FREE_MB,
        "playwright_max_concurrency": PLAYWRIGHT_MAX_CONCURRENCY,
        "playwright_can_start_now": (free_mb is None or free_mb >= PLAYWRIGHT_MIN_FREE_MB),
    }

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

# lux's strongest sites (mostly East Asian video portals + a handful
# of Western sites). yt-dlp covers most of these too; lux is a
# fallback specifically for Bilibili / Douyin / Weibo where the
# Chinese-side extractor logic in yt-dlp lags upstream changes.
# Source: https://github.com/iawia002/lux#supported-sites
LUX_DOMAINS = {
    "bilibili.com", "www.bilibili.com", "b23.tv",
    "douyin.com", "www.douyin.com", "v.douyin.com",
    "iqiyi.com", "www.iqiyi.com",
    "youku.com", "v.youku.com",
    "weibo.com", "weibo.cn", "video.weibo.com",
    "qq.com", "v.qq.com",
    "huya.com",
    "douyu.com",
    "kuaishou.com",
    "ixigua.com",
    "miaopai.com",
}

# you-get's coverage overlaps yt-dlp heavily; it's mostly useful as a
# tertiary fallback for niche Chinese / Japanese sites where yt-dlp
# extractors broke and lux doesn't cover the URL shape.
# Source: https://github.com/soimort/you-get#supported-sites
YOU_GET_DOMAINS = {
    "bilibili.com", "www.bilibili.com",
    "iqiyi.com", "www.iqiyi.com",
    "youku.com", "v.youku.com",
    "weibo.com",
    "qq.com", "v.qq.com",
    "nicovideo.jp",
    "tudou.com",
    "le.com", "letv.com",
    "sohu.com", "tv.sohu.com",
    "baomihua.com",
    "yinyuetai.com",
    "ku6.com",
    "panda.tv",
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
# lux adapter (Go binary, niche East-Asian portals)
# ---------------------------------------------------------------------------
def lux_available() -> bool:
    return _resolve_tool("lux") is not None


def lux_download(url: str, dst_dir: Path, *, timeout: int = 240) -> ExtractionResult:
    """Download via lux. Raises ExtractionError on failure."""
    bin_path = _resolve_tool("lux")
    if not bin_path:
        raise ExtractionError("lux not installed", terminal=True)

    cmd = [
        bin_path,
        "-o", str(dst_dir),
        "-O", "lux-out",
        url,
    ]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(f"lux timed out after {timeout}s", terminal=False) from exc

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        last = msg[-1] if msg else f"lux exited {proc.returncode}"
        raise ExtractionError(last[:300], terminal=_is_terminal(last))

    files = sorted(
        (p for p in dst_dir.rglob("*") if p.is_file() and p.suffix.lower() not in {".part", ".tmp"}),
        key=lambda p: p.stat().st_size,
    )
    if not files:
        raise ExtractionError("lux produced no output file")
    primary = files[-1]
    return ExtractionResult(
        path=primary,
        title=primary.stem[:200],
        duration=None,
        extractor="lux",
        site=_domain_of(url),
    )


# ---------------------------------------------------------------------------
# you-get adapter (Python, niche overlap with yt-dlp)
# ---------------------------------------------------------------------------
def you_get_available() -> bool:
    return _resolve_tool("you-get") is not None


def you_get_download(url: str, dst_dir: Path, *, timeout: int = 240) -> ExtractionResult:
    """Download via you-get."""
    bin_path = _resolve_tool("you-get")
    if not bin_path:
        raise ExtractionError("you-get not installed", terminal=True)

    cmd = [
        bin_path,
        "--no-caption",
        "--output-dir", str(dst_dir),
        "--output-filename", "you-get-out",
        url,
    ]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError(f"you-get timed out after {timeout}s", terminal=False) from exc

    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        last = msg[-1] if msg else f"you-get exited {proc.returncode}"
        raise ExtractionError(last[:300], terminal=_is_terminal(last))

    files = sorted(
        (p for p in dst_dir.rglob("*") if p.is_file() and p.suffix.lower() not in {".part", ".tmp", ".xml"}),
        key=lambda p: p.stat().st_size,
    )
    if not files:
        raise ExtractionError("you-get produced no output file")
    primary = files[-1]
    return ExtractionResult(
        path=primary,
        title=primary.stem[:200],
        duration=None,
        extractor="you-get",
        site=_domain_of(url),
    )


# ---------------------------------------------------------------------------
# Playwright adapter (universal last-resort browser-based capture)
# ---------------------------------------------------------------------------
# We import Playwright lazily so the module load cost stays at zero
# until something actually needs it. Playwright + Chromium together
# pull in roughly 250 MB compressed / 450 MB on disk once installed.
PLAYWRIGHT_NAV_TIMEOUT_MS = 30_000
PLAYWRIGHT_IDLE_DWELL_MS = 5_000
PLAYWRIGHT_MAX_WALL_S = int(os.environ.get("CMVIDEO_PLAYWRIGHT_MAX_WALL_S", "120"))

# Heuristic: which response URLs / content-types look like media
# manifests we can hand to ffmpeg. Order matters - HLS/DASH first
# because they tend to point at the canonical playable stream, raw
# .mp4 second.
_MEDIA_URL_HINTS = (".m3u8", ".mpd", ".mp4", ".webm", ".m4s", ".ts")
_MEDIA_CT_HINTS = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
    "video/",
)


def playwright_available() -> bool:
    """True if both the Python package and the Chromium binary are
    installed. Chromium without the Python bindings is useless to us."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    # The Chromium binary lives under ms-playwright in HOME by default
    # or under PLAYWRIGHT_BROWSERS_PATH if set. We just check that the
    # `playwright` CLI reports it installed.
    cli = _resolve_tool("playwright")
    if not cli:
        return False
    try:
        r = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return False
    except Exception:  # noqa: BLE001
        return False
    return True


def _playwright_memory_check() -> None:
    """Refuse to start a Chromium tab when free RAM is below the
    configured floor. Returning normally means it's safe to proceed."""
    free = _free_memory_mb()
    if free is None:
        return  # unknown, trust the caller
    if free < PLAYWRIGHT_MIN_FREE_MB:
        raise ExtractionError(
            f"Refusing to launch Playwright: only {free} MB free "
            f"(need {PLAYWRIGHT_MIN_FREE_MB} MB to be safe). Free up "
            f"memory or use the desktop app."
        )


def playwright_download(
    url: str,
    dst_dir: Path,
    *,
    fmt: str = "mp4",
    timeout: int = PLAYWRIGHT_MAX_WALL_S,
) -> ExtractionResult:
    """Universal-ish fallback: spin up a headless Chromium, navigate
    to the page, sniff network responses for HLS/DASH manifests or
    direct video URLs, then hand the best candidate to ffmpeg.

    Will refuse to run if free RAM is below `PLAYWRIGHT_MIN_FREE_MB`
    or if `PLAYWRIGHT_MAX_CONCURRENCY` browsers are already running.
    Both knobs are env-var overridable.
    """
    if not playwright_available():
        raise ExtractionError("Playwright + Chromium not installed", terminal=True)

    _playwright_memory_check()

    if not _playwright_semaphore.acquire(blocking=False):
        raise ExtractionError(
            "Another Playwright fallback is already running. Try again "
            "in a few seconds.",
            terminal=False,
        )

    try:
        return _playwright_download_locked(url, dst_dir, fmt=fmt, timeout=timeout)
    finally:
        _playwright_semaphore.release()


def _playwright_download_locked(
    url: str,
    dst_dir: Path,
    *,
    fmt: str,
    timeout: int,
) -> ExtractionResult:
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    media_urls: list[tuple[str, str, int]] = []  # (url, content-type, response-bytes-hint)
    candidate_hdrs: dict[str, dict[str, str]] = {}

    def _on_response(resp):  # type: ignore[no-untyped-def]
        try:
            r_url = resp.url or ""
            ct = (resp.headers.get("content-type") or "").lower()
            req = resp.request
        except Exception:  # noqa: BLE001
            return
        url_lc = r_url.lower()
        is_media_url = any(h in url_lc for h in _MEDIA_URL_HINTS)
        is_media_ct = any(ct.startswith(h) or h in ct for h in _MEDIA_CT_HINTS)
        if not (is_media_url or is_media_ct):
            return
        size = 0
        try:
            cl = resp.headers.get("content-length")
            size = int(cl) if cl else 0
        except Exception:  # noqa: BLE001
            size = 0
        media_urls.append((r_url, ct, size))
        # Capture the request headers Chromium sent for this exact
        # media URL. Without these, ffmpeg gets a 403 from any CDN
        # that checks Referer (thisvid, the *.tube family, etc.).
        try:
            if req is not None:
                hdrs: dict[str, str] = {}
                for k, v in (req.headers or {}).items():
                    if not isinstance(k, str) or not isinstance(v, str):
                        continue
                    if k.lower() in (
                        "referer", "user-agent", "origin", "cookie",
                        "accept", "accept-language", "x-requested-with",
                    ):
                        hdrs[k] = v
                if hdrs:
                    candidate_hdrs[r_url] = hdrs
        except Exception:  # noqa: BLE001
            pass

    deadline = time.monotonic() + timeout
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",  # crucial inside Docker
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
            )
            page = ctx.new_page()
            page.on("response", _on_response)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                raise ExtractionError(f"Playwright navigation failed: {exc}") from exc

            # Try clicking common "play" affordances. Many sites only
            # request the manifest after a user gesture. Best-effort,
            # we don't care if any individual click fails.
            for selector in (
                'button[aria-label*="lay" i]',
                'button[aria-label*="layer" i]',
                'button[title*="lay" i]',
                'button:has-text("Play")',
                '[role="button"]:has-text("Play")',
                'video',
            ):
                if time.monotonic() > deadline:
                    break
                try:
                    el = page.locator(selector).first
                    el.click(timeout=1500)
                    break
                except Exception:  # noqa: BLE001
                    continue

            page.wait_for_timeout(min(PLAYWRIGHT_IDLE_DWELL_MS, max(0, int((deadline - time.monotonic()) * 1000))))
            # Snapshot cookies before tearing the context down. Same
            # rationale as the LLM tier: CDNs that gate hot-linking
            # often want session cookies in addition to Referer.
            try:
                cookies_snapshot: list[dict] = list(ctx.cookies()) or []
            except Exception:  # noqa: BLE001
                cookies_snapshot = []
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    if not media_urls:
        raise ExtractionError("Playwright saw no media manifests on this page")

    # Score candidates: prefer HLS, then DASH, then mp4, then size hint.
    def _score(item):
        u, ct, sz = item
        u_l = u.lower()
        return (
            ".m3u8" in u_l or "mpegurl" in ct,
            ".mpd" in u_l or "dash" in ct,
            ".mp4" in u_l or "video/mp4" in ct,
            sz,
        )

    media_urls.sort(key=_score, reverse=True)
    best_url = media_urls[0][0]
    log.info("playwright candidates=%d, picked=%s", len(media_urls), best_url[:120])

    out_file = dst_dir / f"playwright.{fmt}"
    headers = dict(candidate_hdrs.get(best_url) or {})
    ok, err = _ffmpeg_capture_url(
        best_url,
        out_file,
        request_headers=headers,
        cookies=cookies_snapshot,
        duration_cap=timeout - 30,
        timeout=max(60, timeout),
    )
    if not ok:
        for alt_url, _ct, _sz in media_urls[1:3]:
            if alt_url == best_url:
                continue
            log.info("playwright primary candidate failed; retrying %s", alt_url[:120])
            alt_headers = dict(candidate_hdrs.get(alt_url) or headers)
            ok, err = _ffmpeg_capture_url(
                alt_url,
                out_file,
                request_headers=alt_headers,
                cookies=cookies_snapshot,
                duration_cap=timeout - 30,
                timeout=max(60, timeout),
            )
            if ok:
                best_url = alt_url
                break
    if not ok:
        if err == "ffmpeg capture timed out":
            raise ExtractionError("Playwright + ffmpeg capture timed out")
        raise ExtractionError(f"ffmpeg refused the captured manifest: {err}")

    return ExtractionResult(
        path=out_file,
        title=f"Captured via Playwright ({_domain_of(url)})",
        duration=None,
        extractor="playwright",
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
    enabled: Iterable[str] = (
        "yt-dlp", "gallery-dl", "cobalt",
        "lux", "you-get", "streamlink", "playwright",
        "llm",  # Tier 8: LLM-assisted, only fires if env-configured
    ),
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

    if (
        "lux" in enabled_set
        and lux_available()
        and (not domain or _ends_with_any(domain, LUX_DOMAINS))
    ):
        try:
            r = lux_download(url, dst_dir)
            r.attempts = attempts + ["lux: ok"]
            return r
        except ExtractionError as exc:
            _emit("lux", exc.message)
            last_error = exc

    if (
        "you-get" in enabled_set
        and you_get_available()
        and (not domain or _ends_with_any(domain, YOU_GET_DOMAINS))
    ):
        try:
            r = you_get_download(url, dst_dir)
            r.attempts = attempts + ["you-get: ok"]
            return r
        except ExtractionError as exc:
            _emit("you-get", exc.message)
            last_error = exc

    # Playwright is the universal last resort. It's slow (~20-60s) and
    # heavy (~300 MB Chromium tab), so we only invoke it if every
    # cheaper tool has failed AND the URL clearly points at a public
    # http(s) page. Live-stream URLs already routed to streamlink at
    # the top of this function.
    # Shared Playwright capture: when the heuristic Playwright tier
    # fires, it already pays the cost of opening Chromium and
    # collecting the network log. Tier 8 (LLM) needs the same data
    # to reason over, so we hand the capture down rather than
    # re-opening Chromium a second time.
    shared_capture = None

    if (
        "playwright" in enabled_set
        and playwright_available()
        and url.lower().startswith(("http://", "https://"))
        and not is_live  # streamlink already handled this case
    ):
        try:
            _llm = _import_llm_extract()
            _playwright_memory_check()
            shared_capture = _llm.capture_page(url)
            r = _playwright_download_from_capture(shared_capture, dst_dir, fmt=fmt)
            r.attempts = attempts + ["playwright: ok"]
            return r
        except ExtractionError as exc:
            _emit("playwright", exc.message)
            last_error = exc
        except Exception as exc:  # noqa: BLE001
            # Capture itself blew up (Chromium crash, OOM, etc.) -
            # surface as a recoverable failure so Tier 8 can still
            # try its own capture if it wants.
            _emit("playwright", f"capture failed: {exc}")
            shared_capture = None

    # Tier 8: LLM-assisted extraction. Only fires when the env vars
    # are set AND every cheaper tool above has failed. Re-uses the
    # Playwright capture from the previous tier whenever possible.
    if (
        "llm" in enabled_set
        and url.lower().startswith(("http://", "https://"))
        and not is_live
    ):
        try:
            _llm = _import_llm_extract()
            if not _llm.llm_available():
                raise ExtractionError(
                    "LLM tier disabled (no CMVIDEO_LLM_BASE_URL / _API_KEY set)",
                    terminal=True,
                )
            _playwright_memory_check()
            cap = shared_capture or _llm.capture_page(url)
            out_path, decision = _llm.llm_extract(url, dst_dir, fmt=fmt, capture=cap)
            return ExtractionResult(
                path=out_path,
                title=cap.page_title or _domain_of(url),
                duration=None,
                extractor="llm",
                site=_domain_of(url),
                attempts=attempts + [f"llm: ok (conf={decision.confidence:.2f})"],
            )
        except (ExtractionError, RuntimeError) as exc:
            _emit("llm", str(exc))
            last_error = ExtractionError(str(exc))

    msg = "All extractors failed."
    if last_error is not None:
        msg = f"{msg} Last error: {last_error.message}"
    raise ExtractionError(msg, attempts=attempts)


def _build_cookie_header(cookies: list[dict] | None, target_url: str) -> str:
    """Flatten a Playwright-shaped cookie list into a single
    ``Cookie:`` header value matching `target_url`'s host. Returns
    an empty string when there's nothing to send.

    We deliberately don't try to be RFC-perfect here: same-site rules,
    host-only flags, secure/path matching all just devolve to "send
    the cookies whose domain looks like a suffix of the target host".
    That matches what ffmpeg can use and what every CDN we've seen
    accepts.
    """
    if not cookies:
        return ""
    try:
        from urllib.parse import urlparse  # local import to keep top-level cheap
        host = (urlparse(target_url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    if not host:
        return ""
    pairs: list[str] = []
    seen: set[str] = set()
    for c in cookies:
        try:
            name = str(c.get("name") or "")
            value = str(c.get("value") or "")
            domain = str(c.get("domain") or "").lower().lstrip(".")
        except Exception:  # noqa: BLE001
            continue
        if not name or not value or not domain:
            continue
        # Domain match: target host equals or ends with the cookie's
        # registered domain (the same thing browsers do when picking
        # which cookies to attach to an outgoing request).
        if not (host == domain or host.endswith("." + domain)):
            continue
        if name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _ffmpeg_capture_url(
    media_url: str,
    out_file: Path,
    *,
    request_headers: dict[str, str] | None = None,
    cookies: list[dict] | None = None,
    duration_cap: int,
    timeout: int,
) -> tuple[bool, str]:
    """Pull `media_url` to `out_file` with ffmpeg, replaying the
    Referer / User-Agent / Cookie that Chromium sent so CDNs that
    enforce hot-linking protection (thisvid, the *.tube tubes, most
    porn-CDN dragnet vendors) don't 403 us.

    Returns ``(success, last_stderr_tail)``. Caller decides whether
    to raise.
    """
    headers = dict(request_headers or {})
    # Pull out the special-cased ones so we can use ffmpeg's dedicated
    # flags - they're more robust than stuffing everything into
    # `-headers` and they survive HLS segment redirects.
    user_agent = ""
    referer = ""
    for k in list(headers.keys()):
        lk = k.lower()
        if lk == "user-agent":
            user_agent = headers.pop(k)
        elif lk == "referer":
            referer = headers.pop(k)
        elif lk == "cookie":
            # We rebuild Cookie from the snapshot below so we don't
            # ship a stale single-shot cookie header that's missing
            # whatever the page set during dwell.
            headers.pop(k)

    cookie_header = _build_cookie_header(cookies, media_url)

    ff_cmd: list[str] = ["ffmpeg", "-y", "-loglevel", "error"]
    if user_agent:
        ff_cmd += ["-user_agent", user_agent]
    else:
        ff_cmd += ["-user_agent", (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )]
    if referer:
        ff_cmd += ["-referer", referer]
    extra_lines: list[str] = []
    for k, v in headers.items():
        if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
            extra_lines.append(f"{k.strip()}: {v.strip()}")
    if cookie_header:
        extra_lines.append(f"Cookie: {cookie_header}")
    if extra_lines:
        ff_cmd += ["-headers", "\\r\\n".join(extra_lines) + "\\r\\n"]
    ff_cmd += [
        "-i", media_url,
        "-t", str(max(60, duration_cap)),
        "-c", "copy",
        str(out_file),
    ]
    try:
        proc = subprocess.run(
            ff_cmd, check=False, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "ffmpeg capture timed out"
    if proc.returncode != 0 or not out_file.exists() or out_file.stat().st_size == 0:
        return False, (proc.stderr or proc.stdout or "").strip()[-300:]
    return True, ""


def _playwright_download_from_capture(
    capture, dst_dir: Path, *, fmt: str
) -> ExtractionResult:
    """Pick the best media URL from an already-captured page and run
    ffmpeg against it. Used by both the regular Playwright tier and
    the shared-capture path when Tier 8 follows. Refactored out of
    `playwright_download` so both call sites share scoring logic."""
    if not capture.media_candidates:
        raise ExtractionError("Playwright saw no media manifests on this page")

    def _score(item):
        u, ct, sz = item
        u_l = u.lower()
        return (
            ".m3u8" in u_l or "mpegurl" in ct,
            ".mpd" in u_l or "dash" in ct,
            ".mp4" in u_l or "video/mp4" in ct,
            sz,
        )

    media = sorted(capture.media_candidates, key=_score, reverse=True)
    best_url = media[0][0]
    log.info("playwright candidates=%d, picked=%s", len(media), best_url[:120])

    candidate_hdrs = getattr(capture, "candidate_headers", {}) or {}
    cookies = getattr(capture, "cookies", []) or []
    headers = dict(candidate_hdrs.get(best_url) or {})

    out_file = dst_dir / f"playwright.{fmt}"
    ok, err = _ffmpeg_capture_url(
        best_url,
        out_file,
        request_headers=headers,
        cookies=cookies,
        duration_cap=PLAYWRIGHT_MAX_WALL_S - 30,
        timeout=PLAYWRIGHT_MAX_WALL_S,
    )
    if not ok:
        # If the highest-scored candidate failed, try the next one or
        # two before giving up. CDNs sometimes serve a tracking-pixel
        # mp4 ahead of the real manifest.
        for alt_url, _ct, _sz in media[1:3]:
            if alt_url == best_url:
                continue
            log.info("playwright primary candidate failed; retrying %s", alt_url[:120])
            alt_headers = dict(candidate_hdrs.get(alt_url) or headers)
            ok, err = _ffmpeg_capture_url(
                alt_url,
                out_file,
                request_headers=alt_headers,
                cookies=cookies,
                duration_cap=PLAYWRIGHT_MAX_WALL_S - 30,
                timeout=PLAYWRIGHT_MAX_WALL_S,
            )
            if ok:
                best_url = alt_url
                break
    if not ok:
        raise ExtractionError(f"ffmpeg refused the captured manifest: {err}")

    return ExtractionResult(
        path=out_file,
        title=capture.page_title or f"Captured ({_domain_of(capture.page_url)})",
        duration=None,
        extractor="playwright",
        site=_domain_of(capture.page_url),
    )


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
    available when an instance has been configured via env;
    Playwright counts only when both the Python package and Chromium
    are installed."""
    out = {
        "yt-dlp": False,
        "gallery-dl": gallery_dl_available(),
        "cobalt": cobalt_available(),
        "lux": lux_available(),
        "you-get": you_get_available(),
        "streamlink": streamlink_available(),
        "playwright": playwright_available(),
        "llm": False,
    }
    try:
        import yt_dlp  # noqa: F401
        out["yt-dlp"] = True
    except ImportError:
        pass
    try:
        _llm = _import_llm_extract()
        out["llm"] = _llm.llm_available()
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
    for name in ("gallery-dl", "streamlink", "lux", "you-get", "playwright"):
        bin_path = _resolve_tool(name)
        if not bin_path:
            continue
        try:
            r = subprocess.run([bin_path, "--version"], capture_output=True, text=True, timeout=5)
            versions[name] = ((r.stdout or r.stderr).strip().splitlines() or ["installed"])[0][:80]
        except Exception:  # noqa: BLE001
            versions[name] = "installed"
    if cobalt_available():
        versions["cobalt"] = COBALT_API_BASE
    else:
        versions["cobalt"] = "not configured (set COBALT_API_BASE)"
    try:
        _llm = _import_llm_extract()
        if _llm.llm_available():
            versions["llm"] = f"{_llm.LLM_MODEL} @ {_llm.LLM_BASE_URL}"
        else:
            versions["llm"] = "not configured (set CMVIDEO_LLM_BASE_URL + _API_KEY)"
    except ImportError:
        versions["llm"] = "module missing"
    return versions


def _import_llm_extract():
    """Import the llm_extract module under both layouts:
       desktop: `censor/llm_extract.py` (package import)
       mini:    `web-mini/llm_extract.py` (top-level module)
    """
    try:
        from . import llm_extract as _llm  # package layout (desktop)
        return _llm
    except ImportError:
        import llm_extract as _llm  # flat layout (mini)
        return _llm
