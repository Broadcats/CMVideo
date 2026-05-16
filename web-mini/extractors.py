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

import proxy_router as _proxy_router  # per-domain residential proxy

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

# Auth-hostile sites that block unauthenticated scraping for most
# reel/video URLs. The cheap CLI tools (gallery-dl, cobalt, lux,
# you-get) almost always fail on these without session cookies, so
# the dispatcher short-circuits them: yt-dlp first (still cheap to
# try, occasionally works), then jump straight to Playwright (slow
# but actually loads the page in a real browser, so public reels
# work). Skipping the middle tiers cuts the user-visible wait from
# ~3-4 minutes down to ~30-60 seconds.
# Defence-in-depth allowlist for tool-supplied identifiers we hand
# back to the same tool as a CLI argument (lux's `-stream <id>`,
# you-get's `--format <tag>`). These values come from parsing the
# tool's own enumeration output and *should* always be tame
# alphanumerics, but if upstream changed their output format and
# we ever surfaced a value starting with `-`, the next argv slot
# would interpret it as a fresh CLI flag (a classic argv-injection
# pattern). Reject anything outside the conservative tag charset,
# AND require the first character to be alphanumeric so a
# `-cookie=...` / `--something` shaped value can never sneak
# through even though `-` is otherwise legal mid-tag (`fmt-720p`).
_SAFE_TOOL_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


AUTH_HOSTILE_DOMAINS = {
    "instagram.com", "www.instagram.com",
    "facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch",
    "threads.net", "www.threads.net",
}

# Per-tool soft timeout overrides for hostile domains. Without this
# gallery-dl will sit on an IG URL for the full 180s before giving
# up, even though a successful gallery-dl IG fetch (when it works)
# completes in ~5s. Shortening the cap means users on hostile sites
# fall through to Playwright sooner.
HOSTILE_TOOL_TIMEOUT_S = 30


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


def _gallery_dl_quality_overrides(target_height: int) -> list[str]:
    """Build `-o key=value` overrides that nudge gallery-dl toward
    the requested height on the video-supporting extractors. Most
    gallery-dl traffic is images (where height is meaningless), but
    a handful of extractors (twitter, instagram, reddit, tumblr,
    bilibili) do support video and accept per-site quality keys.

    We map `target_height` to the closest bucket each extractor
    accepts. The `-o` flag silently ignores keys that the resolved
    extractor doesn't know about, so this is safe to spam.
    """
    h = int(target_height) if target_height else 720
    # Most extractors accept "lowest" / "low" / "medium" / "high" /
    # "highest"; some use raw heights. We use the closest bucket name.
    if h >= 1440:
        bucket = "highest"
    elif h >= 720:
        bucket = "high"
    elif h >= 480:
        bucket = "medium"
    else:
        bucket = "low"
    return [
        # twitter / x: variants list, pick by bucket name.
        "-o", f"extractor.twitter.videos=true",
        "-o", f"extractor.twitter.video-quality={bucket}",
        # bilibili: takes raw heights, falls through gracefully.
        "-o", f"extractor.bilibili.quality={h}",
        # reddit: dash/hls master selector.
        "-o", f"extractor.reddit.video-quality={bucket}",
        # tumblr / instagram: a few variants, "best" picks highest.
        "-o", f"extractor.tumblr.videos=true",
        "-o", f"extractor.instagram.videos=true",
    ]


def gallery_dl_download(
    url: str,
    dst_dir: Path,
    *,
    timeout: int = 180,
    target_height: int = 720,
) -> ExtractionResult:
    """Download via gallery-dl. Raises ExtractionError on failure.

    `target_height` is plumbed via per-site `-o` overrides for the
    extractors that support video. Image-only extractors silently
    ignore the keys, so this is safe to apply universally.
    """
    if not gallery_dl_available():
        raise ExtractionError("gallery-dl not installed", terminal=True)

    bin_path = _resolve_tool("gallery-dl") or "gallery-dl"
    cmd = [
        bin_path,
        "--quiet",
        "--no-mtime",
        "--directory", str(dst_dir),
    ]
    cmd += _gallery_dl_quality_overrides(target_height)
    cmd.append(url)
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


def _cobalt_quality_str(target_height: int) -> str:
    """Map our 720/1080 height cap to one of Cobalt's accepted
    quality buckets. Cobalt v10 accepts:
      144, 240, 360, 480, 720, 1080, 1440, 2160, max
    We pick the bucket closest to but not exceeding the cap.
    """
    buckets = (144, 240, 360, 480, 720, 1080, 1440, 2160)
    h = int(target_height) if target_height else 720
    fit = max((b for b in buckets if b <= h), default=buckets[0])
    return str(fit)


def cobalt_download(
    url: str,
    dst_dir: Path,
    *,
    fmt: str = "mp4",
    timeout: int = 180,
    api_base: str | None = None,
    api_key: str | None = None,
    target_height: int = 720,
    max_filesize: int | None = None,
) -> ExtractionResult:
    """Resolve a media URL through a Cobalt instance and stream it to
    disk. Cobalt v10+ takes a POST to the instance root and returns
    either a `redirect` (direct CDN URL) or a `tunnel` URL which we
    then fetch ourselves.

    `target_height` is mapped to Cobalt's `videoQuality` bucket so the
    tool returns a CDN URL at the right resolution rather than its
    server-side default.

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
        "videoQuality": _cobalt_quality_str(target_height),
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

    # Streaming byte cap. A malicious or compromised Cobalt instance
    # could hand us a `media_url` pointing at an arbitrarily large
    # response (terabyte-scale) and watch the HF Space exhaust disk
    # before the connection drops. Cap to the per-job size cap (or
    # the default if the caller didn't pass one) and abort the
    # write the moment we cross it. We Content-Length-check first
    # for the cheap path, then enforce the cap during the stream
    # since servers can lie about (or omit) the header.
    fetch = urllib.request.Request(
        media_url,
        headers={"User-Agent": COBALT_USER_AGENT},
    )
    cap = int(max_filesize) if max_filesize and max_filesize > 0 else (2 * 1024 * 1024 * 1024)
    try:
        with urllib.request.urlopen(fetch, timeout=timeout) as resp:
            advertised = resp.headers.get("Content-Length")
            if advertised:
                try:
                    if int(advertised) > cap:
                        raise ExtractionError(
                            f"Cobalt media exceeds {cap // (1024*1024)} MB cap "
                            f"(advertised {int(advertised) // (1024*1024)} MB)",
                            terminal=True,
                        )
                except ValueError:
                    pass  # malformed header - fall through to stream-time cap
            written = 0
            chunk = 1 << 20
            with dst.open("wb") as out:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    written += len(buf)
                    if written > cap:
                        out.close()
                        try:
                            dst.unlink()
                        except OSError:
                            pass
                        raise ExtractionError(
                            f"Cobalt media exceeded {cap // (1024*1024)} MB cap mid-stream",
                            terminal=True,
                        )
                    out.write(buf)
    except ExtractionError:
        raise
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


def _lux_pick_stream(bin_path: str, url: str, *, target_height: int, timeout: int = 25) -> str:
    """Best-effort stream picker for lux. Calls `lux -i <url>` to
    enumerate available streams and parses the markdown-ish output for
    the highest stream at or below `target_height`. Returns the stream
    id (e.g. ``"hd"``, ``"360p"``, ``"flv720"``) or ``""`` if we can't
    pick - in which case the caller should run lux without a stream
    flag and let it default to its highest variant.
    """
    try:
        proc = subprocess.run(
            [bin_path, "-i", url],
            check=False, capture_output=True, text=True, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("lux -i probe failed: %s", exc)
        return ""
    if proc.returncode != 0:
        return ""

    # `lux -i` prints stream blocks roughly:
    #   1     Title:       ...
    #         Quality:     720P AVC ...
    #         Stream:      mp4-720p
    # We extract `(stream_id, height)` pairs and pick the highest <= cap.
    streams: list[tuple[str, int]] = []
    cur_id = ""
    cur_height = 0
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        m_id = re.match(r"(?i)stream:\s*(\S+)", line)
        if m_id:
            cur_id = m_id.group(1)
        m_q = re.search(r"(?i)quality:\s*([^\n]+)", line)
        if m_q:
            blob = m_q.group(1)
            m_h = re.search(r"(\d{3,4})\s*[Pp]\b", blob) or re.search(r"\b\d+x(\d{3,4})\b", blob)
            if m_h:
                try:
                    cur_height = int(m_h.group(1))
                except ValueError:
                    cur_height = 0
        # Each stream block ends at a blank line.
        if not line and cur_id:
            streams.append((cur_id, cur_height))
            cur_id, cur_height = "", 0
    if cur_id:
        streams.append((cur_id, cur_height))

    if not streams:
        return ""
    # Highest at or below the cap; if everything exceeds, pick lowest.
    cap = int(target_height) if target_height else 0
    within = [s for s in streams if cap == 0 or s[1] <= cap]
    if within:
        within.sort(key=lambda s: s[1], reverse=True)
        return within[0][0]
    streams.sort(key=lambda s: s[1])
    return streams[0][0]


def lux_download(
    url: str,
    dst_dir: Path,
    *,
    timeout: int = 240,
    target_height: int = 720,
) -> ExtractionResult:
    """Download via lux. Raises ExtractionError on failure.

    Quality control: we run `lux -i` first to enumerate streams and
    pass `-stream <id>` for the highest that fits `target_height`. If
    enumeration fails (lux's output format varies between versions and
    sites), we fall back to lux's default behavior.
    """
    bin_path = _resolve_tool("lux")
    if not bin_path:
        raise ExtractionError("lux not installed", terminal=True)

    stream_id = _lux_pick_stream(bin_path, url, target_height=target_height)
    cmd = [
        bin_path,
        "-o", str(dst_dir),
        "-O", "lux-out",
    ]
    # Defense-in-depth: stream IDs come from parsing lux's own
    # output, so they should always be tame, but the parser
    # tolerates upstream format changes which could surface a
    # value starting with "-" that the next argv slot would treat
    # as a fresh CLI flag (e.g. "--cookie=...", "-i ..."). Reject
    # anything outside the conservative tag charset.
    if stream_id and not _SAFE_TOOL_TAG_RE.match(stream_id):
        log.warning("lux: rejecting unsafe stream_id=%r; falling through to default", stream_id)
        stream_id = ""
    if stream_id:
        cmd += ["-stream", stream_id]
        log.info("lux: picked stream=%s for cap=%d", stream_id, target_height)
    else:
        log.info("lux: stream enumeration failed; using default", extra={"url": url[:80]})
    cmd.append(url)
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


def _you_get_pick_format(bin_path: str, url: str, *, target_height: int, timeout: int = 25) -> str:
    """Best-effort format picker for you-get. Calls `you-get -i <url>`
    to enumerate available streams and parses the indented `streams`
    block for the highest variant at or below `target_height`. Returns
    the format tag (passed via ``--format=<tag>``) or ``""`` to fall
    through to you-get's default.
    """
    try:
        proc = subprocess.run(
            [bin_path, "-i", url],
            check=False, capture_output=True, text=True, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("you-get -i probe failed: %s", exc)
        return ""
    if proc.returncode != 0:
        return ""

    # `you-get -i` prints blocks roughly:
    #   - format:        mp4-720p
    #     container:     mp4
    #     quality:       1280x720
    #     size:          ...
    streams: list[tuple[str, int]] = []
    cur_fmt = ""
    cur_height = 0
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        m_fmt = re.match(r"(?i)-?\s*format:\s*(\S+)", line)
        if m_fmt:
            if cur_fmt:
                streams.append((cur_fmt, cur_height))
            cur_fmt = m_fmt.group(1)
            cur_height = 0
            continue
        m_q = re.search(r"(?i)quality:\s*([^\n]+)", line)
        if m_q:
            blob = m_q.group(1)
            m_h = re.search(r"\b\d+x(\d{3,4})\b", blob) or re.search(r"(\d{3,4})\s*[Pp]\b", blob)
            if m_h:
                try:
                    cur_height = int(m_h.group(1))
                except ValueError:
                    cur_height = 0
    if cur_fmt:
        streams.append((cur_fmt, cur_height))

    if not streams:
        return ""
    cap = int(target_height) if target_height else 0
    within = [s for s in streams if cap == 0 or s[1] <= cap]
    if within:
        within.sort(key=lambda s: s[1], reverse=True)
        return within[0][0]
    streams.sort(key=lambda s: s[1])
    return streams[0][0]


def you_get_download(
    url: str,
    dst_dir: Path,
    *,
    timeout: int = 240,
    target_height: int = 720,
) -> ExtractionResult:
    """Download via you-get.

    Quality control: we run `you-get -i` first to enumerate streams
    and pass ``--format <tag>`` for the highest at-or-below
    `target_height`. Falls back to you-get's default if enumeration
    fails."""
    bin_path = _resolve_tool("you-get")
    if not bin_path:
        raise ExtractionError("you-get not installed", terminal=True)

    fmt_tag = _you_get_pick_format(bin_path, url, target_height=target_height)
    cmd = [
        bin_path,
        "--no-caption",
        "--output-dir", str(dst_dir),
        "--output-filename", "you-get-out",
    ]
    # Defense-in-depth: same reasoning as the lux stream_id check.
    # Format tags come from parsing you-get's own output and could
    # in principle surface a "-flag"-shaped value if upstream
    # format changed.
    if fmt_tag and not _SAFE_TOOL_TAG_RE.match(fmt_tag):
        log.warning("you-get: rejecting unsafe fmt_tag=%r; falling through to default", fmt_tag)
        fmt_tag = ""
    if fmt_tag:
        cmd += ["--format", fmt_tag]
        log.info("you-get: picked format=%s for cap=%d", fmt_tag, target_height)
    else:
        log.info("you-get: format enumeration failed; using default")
    cmd.append(url)
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
    target_height: int = 720,
) -> ExtractionResult:
    """Universal-ish fallback: spin up a headless Chromium, navigate
    to the page, sniff network responses for HLS/DASH manifests or
    direct video URLs, then hand the best candidate to ffmpeg.

    `target_height` is the max video height the user asked for. We use
    it to rank captured candidates by URL-encoded resolution hints
    (`_720p.mp4`, `?quality=1080`, etc) and to pick the right variant
    out of HLS master playlists.

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
        return _playwright_download_locked(
            url, dst_dir, fmt=fmt, timeout=timeout, target_height=target_height,
        )
    finally:
        _playwright_semaphore.release()


def _playwright_download_locked(
    url: str,
    dst_dir: Path,
    *,
    fmt: str,
    timeout: int,
    target_height: int = 720,
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
    # Per-domain residential proxy: when the page URL is on the
    # PROXY_DOMAINS allowlist (instagram, tiktok, tube sites, etc.),
    # route the entire Playwright session through the residential
    # proxy. Browser-level so all subresource fetches (manifests,
    # media segments, cookies, image assets) inherit the IP.
    pw_proxy = _proxy_router.playwright_proxy_for_url(url)
    launch_kwargs: dict[str, Any] = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",  # crucial inside Docker
            "--disable-blink-features=AutomationControlled",
        ],
    }
    if pw_proxy:
        launch_kwargs["proxy"] = pw_proxy
        log.info("playwright: routing session through residential proxy")
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
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

    # Quality-aware ranking: pick the best candidate at or below the
    # user's height cap (parsing height hints out of CDN URL paths
    # like `_720p.mp4`, `?quality=1080`, etc). Falls through to
    # legacy size-based tie-breaking when no height can be inferred.
    _log_candidates("pw[locked] pre-rank", media_urls)
    media_urls = _rank_candidates(media_urls, max_height=target_height)
    best_url = media_urls[0][0]
    log.info(
        "pw[locked] candidates=%d cap=%d picked_h=%d picked=%s",
        len(media_urls), target_height,
        _extract_height_hint(best_url, media_urls[0][1]),
        best_url[:160],
    )

    out_file = dst_dir / f"playwright.{fmt}"
    headers = dict(candidate_hdrs.get(best_url) or {})
    # If best_url is an HLS master, swap in the variant URL that
    # matches our height cap before handing it to ffmpeg. Lets us
    # avoid ffmpeg's "first variant in the manifest" default.
    fetch_url = _resolve_hls_variant(
        best_url,
        request_headers=headers,
        cookies=cookies_snapshot,
        max_height=target_height,
    )
    ok, err = _ffmpeg_capture_url(
        fetch_url,
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
            alt_fetch = _resolve_hls_variant(
                alt_url,
                request_headers=alt_headers,
                cookies=cookies_snapshot,
                max_height=target_height,
            )
            ok, err = _ffmpeg_capture_url(
                alt_fetch,
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
    target_height: int = 720,
    max_filesize: int | None = None,
) -> ExtractionResult:
    """Try each tool in order until one wins. Raises ExtractionError if
    every applicable tool is exhausted."""
    enabled_set = {e.lower() for e in enabled}
    domain = _domain_of(url)
    is_live = _looks_live(url)
    attempts: list[str] = []
    last_error: ExtractionError | None = None
    # Auth-hostile sites: skip the cheap CLI fallbacks (gallery-dl,
    # cobalt, lux, you-get) that almost never work without cookies
    # and go straight to Playwright after yt-dlp. Cuts the
    # user-visible wait from ~3-4 minutes to ~30-60s on Instagram /
    # Facebook / Threads URLs that yt-dlp can't crack.
    is_hostile = bool(domain) and (
        domain in AUTH_HOSTILE_DOMAINS or _ends_with_any(domain, AUTH_HOSTILE_DOMAINS)
    )

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

    # On auth-hostile domains, skip every cheap CLI fallback - they
    # all fail without session cookies and just chew up the
    # user-visible wait time. Playwright (next block) handles them.
    if not is_hostile:
        if "gallery-dl" in enabled_set and (not domain or domain in GALLERY_DL_DOMAINS or _ends_with_any(domain, GALLERY_DL_DOMAINS)):
            try:
                r = gallery_dl_download(url, dst_dir, target_height=target_height)
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
                r = cobalt_download(
                    url, dst_dir,
                    fmt=fmt,
                    target_height=target_height,
                    max_filesize=max_filesize,
                )
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
                r = lux_download(url, dst_dir, target_height=target_height)
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
                r = you_get_download(url, dst_dir, target_height=target_height)
                r.attempts = attempts + ["you-get: ok"]
                return r
            except ExtractionError as exc:
                _emit("you-get", exc.message)
                last_error = exc
    else:
        # Surface that we're skipping the CLI fallbacks so the
        # operator can see why on a hostile-domain failure trace.
        _emit("dispatcher", f"hostile domain {domain}; skipping cli fallbacks")

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
            r = _playwright_download_from_capture(
                shared_capture, dst_dir, fmt=fmt, target_height=target_height,
            )
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

    # Auth-hostile sites get a context-rich error message instead
    # of just the raw last-tier failure - users hitting an IG /
    # FB / Threads URL almost always need to know the cause is
    # site-wide (login wall) not a CMVideo bug.
    if is_hostile:
        site_pretty = (
            "Instagram" if "instagram" in (domain or "")
            else "Facebook" if ("facebook" in (domain or "") or "fb.watch" in (domain or ""))
            else "Threads" if "threads" in (domain or "")
            else (domain or "this site")
        )
        msg = (
            f"{site_pretty} blocked the download. Public reels / videos "
            f"sometimes work, but logged-in-only, private-account, or "
            f"newer posts won't extract without session cookies. "
            f"The desktop app at cmvideo.online supports cookies."
        )
        if last_error is not None:
            msg = f"{msg} (Last error: {last_error.message[:200]})"
        raise ExtractionError(msg, attempts=attempts)

    msg = "All extractors failed."
    if last_error is not None:
        msg = f"{msg} Last error: {last_error.message}"
    raise ExtractionError(msg, attempts=attempts)


# ---- Height-aware candidate ranking & HLS variant resolution ---------------
#
# Why these exist: the previous Playwright scorer ranked candidates by
# (HLS, DASH, MP4, response_size) which had two cascading bugs:
#
#   1. HLS master playlists won the rank, but a master `.m3u8` is just
#      a text file pointing at variant streams at different bitrates.
#      ffmpeg's default behavior on a master playlist is to pick the
#      FIRST variant in the manifest - usually the lowest. So every
#      quality choice on tube-style sites silently produced 240p/360p
#      regardless of what the user asked for.
#
#   2. When the master picked direct MP4s, the tiebreaker was response
#      size. A fully-loaded 480p variant beats a partially-loaded 1080p
#      variant - again, low quality wins.
#
# Fix: parse a height hint out of the URL/path/query string itself (CDN
# URLs almost universally encode height as `_720p`, `/720/`, `?res=720`,
# `?quality=720` or similar), rank candidates by height-within-cap
# first, AND resolve HLS master playlists server-side so we hand ffmpeg
# the specific variant URL we want rather than letting it pick.
#
# Anything that can't be parsed (`?quality=high`, opaque tokens) falls
# back to the legacy size-based tiebreak so we never regress to "no
# pick at all".

def _log_candidates(label: str, candidates: list[tuple[str, str, int]]) -> None:
    """Dump every captured candidate to the log with its parsed height
    hint, container hint, and response-size hint. Lets us tell from
    the HF logs whether a "low-quality result" came from the scorer
    picking the wrong variant or from Playwright never seeing higher
    variants in the first place. One INFO line per candidate is fine -
    typical pages produce <20.
    """
    for i, (u, ct, sz) in enumerate(candidates):
        h = _extract_height_hint(u, ct)
        log.info(
            "%s cand[%d] h=%d ct=%s sz=%d url=%s",
            label, i, h, (ct or "")[:40], sz or 0, (u or "")[:160],
        )


# Heights we care about. Anything between 144 and 4320 is plausible.
_HEIGHT_VALUES = (144, 240, 360, 480, 540, 720, 1080, 1440, 2160, 4320)

# Patterns we recognise inside URL paths/query strings. Each pattern
# captures the height as group 1.
_HEIGHT_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"[/_-](\d{3,4})p(?:[/_.\-?&]|$)",     # /720p/, _1080p., -480p?
    r"[?&](?:res|height|h|quality|q)=(\d{3,4})\b",
    r"[?&](?:resolution)=\d+x(\d{3,4})\b",
    r"[/_-](\d{3,4})x(?:\d{3,4})(?:[/_.\-?&]|$)",  # /1280x720/
    r"\bx(\d{3,4})(?:[/_.\-?&]|$)",        # ...x720.mp4
    r"[/_-](\d{3,4})(?:[/_.\-?&]|$)",      # bare /720/, _720. (last resort)
))


def _extract_height_hint(url: str, content_type: str = "") -> int:
    """Return the resolution (height in lines) suggested by the URL's
    path/query, or 0 if we can't tell. Conservative: only returns a
    plausible height (one of `_HEIGHT_VALUES`) so accidental matches
    on timestamps or IDs don't poison ranking.
    """
    if not url:
        return 0
    # Decode percent-escapes once so `?quality%3D720` matches too.
    try:
        sample = urllib.parse.unquote(url)
    except Exception:  # noqa: BLE001
        sample = url
    for pat in _HEIGHT_PATTERNS:
        m = pat.search(sample)
        if not m:
            continue
        try:
            n = int(m.group(1))
        except (ValueError, IndexError):
            continue
        if n in _HEIGHT_VALUES:
            return n
    return 0


def _rank_candidates(
    candidates: list[tuple[str, str, int]],
    *,
    max_height: int,
) -> list[tuple[str, str, int]]:
    """Return `candidates` sorted from best to worst given a height cap.

    Ranking (each tuple element is descending):
      0. height_within_cap: known height that fits at or below cap wins
         over anything else. Higher height (closer to cap) is better.
      1. container_score: prefer mp4 > webm > m3u8 (master) > mpd >
         other. Master playlists are demoted because ffmpeg's variant
         pick is unreliable; we still try them last so we never lose
         the one-and-only manifest case.
      2. height_above_cap_penalty: known heights ABOVE the cap can
         still be used as a fallback (better something than nothing)
         but rank below within-cap candidates.
      3. size: legacy tiebreaker - bigger response = more likely real
         media, not a tracking pixel.
    """
    cap = int(max_height) if max_height else 0

    def container_score(u_l: str, ct: str) -> int:
        ct = (ct or "").lower()
        if ".mp4" in u_l or "video/mp4" in ct:
            return 4
        if ".webm" in u_l or "video/webm" in ct:
            return 3
        if ".m3u8" in u_l or "mpegurl" in ct:
            return 2  # demoted from "winner" to "use only if nothing else"
        if ".mpd" in u_l or "dash" in ct:
            return 1
        return 0

    def key(item: tuple[str, str, int]) -> tuple:
        u, ct, sz = item
        u_l = u.lower()
        h = _extract_height_hint(u, ct)
        if cap > 0 and h > 0 and h <= cap:
            within = h            # 144..1080 in cap; bigger is better
            penalty = 0
        elif cap > 0 and h > cap:
            within = 0
            penalty = -(h - cap)  # closer-to-cap-from-above is better
        else:
            within = 0            # unknown height
            penalty = 0
        return (
            within,
            container_score(u_l, ct),
            penalty,
            sz or 0,
        )

    return sorted(candidates, key=key, reverse=True)


def _resolve_hls_variant(
    master_url: str,
    *,
    request_headers: dict[str, str] | None,
    cookies: list[dict] | None,
    max_height: int,
) -> str:
    """If `master_url` points at an HLS master playlist, fetch it,
    pick the variant whose RESOLUTION fits inside `max_height`
    (preferring the highest such variant), and return that variant's
    absolute URL. If `master_url` isn't actually a master, or we
    can't parse it, return `master_url` unchanged - ffmpeg will then
    handle it as before.

    This is the fix for tube-style sites where the captured candidate
    is `index-master.m3u8` and ffmpeg's default variant pick lands on
    the lowest-bitrate stream. With this resolver, we hand ffmpeg the
    specific 720p (or whatever fits) variant URL directly.
    """
    if not master_url or ".m3u8" not in master_url.lower():
        return master_url
    headers = dict(request_headers or {})
    cookie_header = _build_cookie_header(cookies, master_url)
    if cookie_header:
        headers["Cookie"] = cookie_header
    headers.setdefault(
        "User-Agent",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    )
    try:
        req = urllib.request.Request(master_url, headers=headers)
        # Per-domain residential proxy: HLS masters on IG / TT / FB
        # CDNs need the residential IP just as much as the segment
        # fetches. Falls through to default urlopen for everything
        # else (cheaper, no proxy hop).
        proxy_opener = _proxy_router.urllib_opener_for_url(master_url)
        if proxy_opener is not None:
            opener = proxy_opener.open(req, timeout=10)
        else:
            opener = urllib.request.urlopen(req, timeout=10)
        with opener as resp:
            body = resp.read(256 * 1024).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.info("hls master fetch failed (%s); deferring to ffmpeg", exc)
        return master_url

    # Master playlists contain `#EXT-X-STREAM-INF:` lines; media
    # playlists contain `#EXTINF:`. If we don't see any STREAM-INF
    # this isn't a master - leave the URL alone.
    if "#EXT-X-STREAM-INF" not in body:
        return master_url

    cap = int(max_height) if max_height else 0
    variants: list[tuple[int, int, str]] = []  # (height, bandwidth, url)
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        # The very next non-comment line is the variant URL.
        v_url = ""
        for j in range(i + 1, len(lines)):
            cand = lines[j].strip()
            if cand and not cand.startswith("#"):
                v_url = cand
                break
        if not v_url:
            continue
        # RESOLUTION=1280x720, BANDWIDTH=2400000
        h_match = re.search(r"RESOLUTION=\d+x(\d+)", line)
        b_match = re.search(r"BANDWIDTH=(\d+)", line)
        height = int(h_match.group(1)) if h_match else 0
        bandwidth = int(b_match.group(1)) if b_match else 0
        v_abs = urllib.parse.urljoin(master_url, v_url)
        variants.append((height, bandwidth, v_abs))

    if not variants:
        return master_url

    # Log every variant we saw so failed pulls leave a complete trail.
    for h, bw, vu in variants:
        log.info("hls master variant h=%d bw=%d url=%s", h, bw, (vu or "")[:160])

    # Prefer the highest variant that fits the cap. Fall back to the
    # lowest variant ABOVE the cap if everything is over (so a 1440p-
    # only stream still produces something rather than failing).
    within = [v for v in variants if cap == 0 or v[0] <= cap]
    if within:
        within.sort(key=lambda v: (v[0], v[1]), reverse=True)
        chosen = within[0]
    else:
        variants.sort(key=lambda v: (v[0], v[1]))
        chosen = variants[0]
    log.info(
        "hls variant pick: cap=%d picked_h=%d picked_bw=%d (of %d variants)",
        cap, chosen[0], chosen[1], len(variants),
    )
    return chosen[2]


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
    # Per-domain residential proxy: when media_url is on the
    # PROXY_DOMAINS allowlist (cdninstagram.com, fbcdn.net, tube
    # CDNs, etc.), set http_proxy / https_proxy in ffmpeg's env
    # so it pulls segments through the residential pool. ffmpeg
    # respects the standard env-var convention. No-op when proxy
    # isn't configured or media_url isn't on the allowlist.
    ff_env = _proxy_router.subprocess_env_for_url(media_url)
    try:
        proc = subprocess.run(
            ff_cmd, check=False, capture_output=True, text=True, timeout=timeout,
            env=ff_env,
        )
    except subprocess.TimeoutExpired:
        return False, "ffmpeg capture timed out"
    if proc.returncode != 0 or not out_file.exists() or out_file.stat().st_size == 0:
        return False, _summarize_ffmpeg_error(proc.stderr or proc.stdout or "")
    return True, ""


_FFMPEG_DIAGNOSTIC_PATTERNS = (
    re.compile(r"HTTP error \d+\s+[^\r\n]+", re.IGNORECASE),
    re.compile(r"Server returned \d+\s+[^\r\n]+", re.IGNORECASE),
    re.compile(r"Connection (?:refused|reset|timed out)", re.IGNORECASE),
    re.compile(r"Failed to open[^\r\n]+", re.IGNORECASE),
    re.compile(r"Invalid data found[^\r\n]+", re.IGNORECASE),
    re.compile(r"No such file or directory", re.IGNORECASE),
    re.compile(r"Protocol not found", re.IGNORECASE),
    re.compile(r"Cannot open[^\r\n]+", re.IGNORECASE),
    re.compile(r"403 Forbidden|404 Not Found|410 Gone|429 Too Many", re.IGNORECASE),
)


def _summarize_ffmpeg_error(stderr: str) -> str:
    """Pull a diagnostic substring out of ffmpeg's stderr.

    Naively truncating to the last N chars lands inside whichever
    long URL was being copied at the failure point - the user just
    sees opaque URL fragments instead of the error reason. Instead
    we walk known error patterns (HTTP status, connection failures,
    decoder errors) and return the first match. Falls back to the
    last non-empty *short* line so HLS-master URL spam doesn't
    swallow the actual cause.
    """
    if not stderr:
        return "ffmpeg failed (no stderr)"
    text = stderr.strip()
    # Pattern hit wins.
    for pat in _FFMPEG_DIAGNOSTIC_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0).strip()[:200]
    # No pattern hit: find the last short-ish line (<= 200 chars) so
    # we skip the multi-kilobyte URL line that triggered the failure
    # but still surface whatever ffmpeg actually printed last.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    short_lines = [ln for ln in lines if len(ln) <= 200]
    if short_lines:
        return short_lines[-1][:200]
    # Everything's a giant URL line: fall back to a head-of-stderr
    # snippet rather than the tail (the head usually has the error
    # context, the tail is "from the URL we tried to open").
    return text[:200]


def _playwright_download_from_capture(
    capture, dst_dir: Path, *, fmt: str, target_height: int = 720,
) -> ExtractionResult:
    """Pick the best media URL from an already-captured page and run
    ffmpeg against it. Used by both the regular Playwright tier and
    the shared-capture path when Tier 8 follows. Refactored out of
    `playwright_download` so both call sites share scoring logic.

    `target_height` is the user's quality cap (720 / 1080). Used for
    height-aware candidate ranking and HLS variant selection."""
    if not capture.media_candidates:
        raise ExtractionError("Playwright saw no media manifests on this page")

    _log_candidates("pw[shared] pre-rank", list(capture.media_candidates))
    media = _rank_candidates(list(capture.media_candidates), max_height=target_height)
    best_url = media[0][0]
    log.info(
        "pw[shared] candidates=%d cap=%d picked_h=%d picked=%s",
        len(media), target_height,
        _extract_height_hint(best_url, media[0][1]),
        best_url[:160],
    )

    candidate_hdrs = getattr(capture, "candidate_headers", {}) or {}
    cookies = getattr(capture, "cookies", []) or []
    headers = dict(candidate_hdrs.get(best_url) or {})

    out_file = dst_dir / f"playwright.{fmt}"
    fetch_url = _resolve_hls_variant(
        best_url,
        request_headers=headers,
        cookies=cookies,
        max_height=target_height,
    )
    ok, err = _ffmpeg_capture_url(
        fetch_url,
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
            alt_fetch = _resolve_hls_variant(
                alt_url,
                request_headers=alt_headers,
                cookies=cookies,
                max_height=target_height,
            )
            ok, err = _ffmpeg_capture_url(
                alt_fetch,
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
    # Cobalt + LLM endpoints can be self-hosted internal URLs or
    # fingerprint the provider; we surface "configured" instead of
    # the raw URL so /api/limits doesn't volunteer that detail to
    # anonymous callers. Operators can still confirm a value was
    # parsed - they just have to read their own env vars.
    if cobalt_available():
        versions["cobalt"] = "configured"
    else:
        versions["cobalt"] = "not configured (set COBALT_API_BASE)"
    try:
        _llm = _import_llm_extract()
        if _llm.llm_available():
            versions["llm"] = f"{_llm.LLM_MODEL} (configured)"
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
