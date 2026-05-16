"""CMVideo Mini - the URL/file -> MP4/MP3 slice of CMVideo, on the web.

Deploys as a Hugging Face Space (Docker SDK). Three modes:

* `download` - URL only; yt-dlp pulls the clip and returns the file.
* `silence`  - URL or uploaded file; transcribe with whisper-tiny.en,
               mute every match against the bundled wordlists.
* `beep`     - URL or uploaded file; same matching as `silence` but
               overlays a 1 kHz tone on every match.

Caps stay tight on purpose - the full desktop app at
https://cmvideo.online is the upsell.
"""

import asyncio
import ipaddress
import logging
import os
import re
import shutil
import socket
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.background import BackgroundTask

import mini_censor
import extractors as _extractors  # multi-tool fallback chain (yt-dlp -> gallery-dl -> Cobalt -> streamlink)


# ---------------------------------------------------------------------------
# Caps. The "mini" pitch only holds if these stay tight.
# ---------------------------------------------------------------------------
MAX_DOWNLOAD_DURATION_SECONDS = 60 * 60          # URL download only (1 hour)
MAX_DOWNLOAD_FILESIZE_BYTES = 800 * 1024 * 1024  # URL download only
MAX_CENSOR_DURATION_SECONDS = 8 * 60             # transcription is slow on free CPU
MAX_CENSOR_FILESIZE_BYTES = 100 * 1024 * 1024    # cap upload + output
MAX_UPLOAD_BYTES = 100 * 1024 * 1024             # multipart body size
MAX_VIDEO_HEIGHT = 720
AUDIO_BITRATE_KBPS = "192"

# 1-hour 720p AVC files land around 600-800 MB. From the HF Space's
# datacenter peering that pulls in roughly 60-180s depending on
# origin throttling, so we budget 6 minutes - tight enough that a
# stuck connection still gives up promptly, generous enough to
# actually finish the new 1-hour cap on a slow source.
DOWNLOAD_TIMEOUT_SECONDS = 360
CENSOR_TIMEOUT_SECONDS = 240

RATE_LIMIT_PER_HOUR = "5/hour"

ALLOWED_FORMATS = {"mp4", "mp3"}
ALLOWED_MODES = {"download", "silence", "beep"}
ALLOWED_FPS = {"source", "30", "60"}

WORDLISTS_DIR = Path(os.environ.get("CMVIDEO_WORDLISTS_DIR", "wordlists")).resolve()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cmvideo-mini")


# ---------------------------------------------------------------------------
# App boilerplate
# ---------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    """Resolve the real client IP behind HF Spaces' reverse proxy.

    `slowapi.util.get_remote_address` reads `request.client.host`, which
    on HF Spaces is always the platform's edge proxy - so every visitor
    on Earth shared the same rate-limit bucket. The proxy populates
    `X-Forwarded-For` with the chain `client, proxy1, proxy2, ...`;
    we take the left-most entry (the original client) and fall back to
    the socket peer for direct/unproxied access."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        # Tolerate IPv6 with brackets / zone IDs by feeding ipaddress
        # only the bit it'll accept; if parsing fails we still return
        # the raw string so the limiter buckets on something.
        if first:
            try:
                ipaddress.ip_address(first.split("%", 1)[0])
            except ValueError:
                pass
            return first
    return get_remote_address(request) or "0.0.0.0"


limiter = Limiter(key_func=_client_ip, default_limits=[])

_WORDS_CACHE: Optional[set] = None


def _wordlist_tokens() -> set:
    global _WORDS_CACHE
    if _WORDS_CACHE is None:
        _WORDS_CACHE = mini_censor.load_wordlists(WORDLISTS_DIR)
    return _WORDS_CACHE


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info(
        "CMVideo Mini starting. Caps: download %d min / %d MB, censor %d min / %d MB, %s/IP",
        MAX_DOWNLOAD_DURATION_SECONDS // 60,
        MAX_DOWNLOAD_FILESIZE_BYTES // (1024 * 1024),
        MAX_CENSOR_DURATION_SECONDS // 60,
        MAX_CENSOR_FILESIZE_BYTES // (1024 * 1024),
        RATE_LIMIT_PER_HOUR,
    )
    # Warm the wordlists; the whisper model loads lazily on first use.
    _wordlist_tokens()
    yield


app = FastAPI(title="CMVideo Mini", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS: cmvideo.online embeds the widget in its hero and calls this
# Space cross-origin. The Space's own / endpoint is fallback for direct
# visitors and is same-origin from there.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cmvideo.online",
        "https://www.cmvideo.online",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
    expose_headers=["Content-Disposition"],
    max_age=86400,
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Tack on standard security headers.

    No CSP here - the widget on cmvideo.online embeds *us*, and HF
    Spaces also embeds us in a frame, so any aggressive CSP/X-Frame
    policy will break legitimate use. The headers we *do* add are
    universally safe: stop MIME-sniffing, narrow the referrer to
    origins, and refuse opt-in capabilities (camera/mic/geo) that
    we never need."""
    response: Response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    return response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Ports we'll let yt-dlp talk to. Anything else (SSH 22, SMTP 25,
# MySQL 3306, Redis 6379, internal admin panels on 9000/9090, ...) is
# refused at the validation gate.
_ALLOWED_REMOTE_PORTS = {None, 80, 443, 8080, 8443}

# Cloud metadata endpoints. is_link_local already covers 169.254.0.0/16
# but list them explicitly so the intent is grep-able and future cloud
# providers can be added in one place.
_BLOCKED_HOSTS_LITERAL = {
    "169.254.169.254",   # AWS / GCP / Azure / DigitalOcean
    "metadata.google.internal",
    "metadata.goog",
    "100.100.100.200",   # Alibaba
}


def _ip_is_internal(ip: ipaddress._BaseAddress) -> bool:
    """True if the IP is anything we never want our backend to reach
    on behalf of an arbitrary HTTP request from the internet."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _assert_public_url(url: str) -> str:
    """Validate that `url` is safe for yt-dlp / requests to fetch.

    Belt-and-suspenders SSRF defence:
      * scheme must be http/https
      * no userinfo (`user:pass@host`) - credential phishing surface
      * port must be in our allow-list (web ports only)
      * hostname is resolved here and *every* resulting A/AAAA record
        must be public; private/loopback/link-local/multicast/reserved
        all reject
      * literal-IP and well-known metadata hostnames reject regardless

    Hostname resolution here is best-effort: yt-dlp will re-resolve at
    request time and may follow redirects. The companion
    `_install_socket_ssrf_guard()` patches `socket.getaddrinfo` so any
    later resolution to an internal IP also fails."""
    raw = (url or "").strip()
    if not URL_RE.match(raw):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    try:
        parsed = urlparse(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="That URL didn't parse.")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise HTTPException(status_code=400, detail="URL has no hostname.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="URLs with embedded credentials are not allowed.")
    try:
        port = parsed.port
    except ValueError:
        raise HTTPException(status_code=400, detail="URL has an invalid port.")
    if port not in _ALLOWED_REMOTE_PORTS:
        raise HTTPException(status_code=400, detail=f"Port {port} is not allowed.")
    if host in _BLOCKED_HOSTS_LITERAL:
        raise HTTPException(status_code=400, detail="That host is blocked.")

    # Literal-IP fast path. urlparse strips the brackets from
    # `http://[::1]/` so `host` is already the bare IP string here.
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if _ip_is_internal(literal_ip):
            raise HTTPException(status_code=400, detail="That URL points at an internal address.")
        return raw  # public literal IP - allow

    # Resolve. Any DNS failure here is a hard reject; if we can't
    # verify the destination is public we don't fetch it. Our
    # `_install_socket_ssrf_guard` will *also* raise gaierror when
    # the public name resolves to an internal IP, so this also
    # catches DNS rebinding scenarios.
    try:
        infos = _REAL_GETADDRINFO(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Couldn't resolve that hostname.")
    for family, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
        except ValueError:
            raise HTTPException(status_code=400, detail="That host resolved to something we couldn't parse.")
        if _ip_is_internal(ip):
            log.warning("SSRF reject: host=%s resolved to internal IP %s", host, ip)
            raise HTTPException(status_code=400, detail="That URL points at an internal address.")
    return raw


# Backwards-compat alias - older call sites used `_validate_url` purely
# for scheme checking. Route all callers through the hardened guard.
_validate_url = _assert_public_url


# ---------------------------------------------------------------------------
# Socket-level SSRF guard. yt-dlp will follow redirects and re-resolve
# hostnames internally; our `_assert_public_url` only sees the first URL
# the user passed in. By patching `socket.getaddrinfo` once at import we
# turn private-IP destinations into immediate connection failures no
# matter who triggers them - yt-dlp, urllib, requests, anything.
# ---------------------------------------------------------------------------
_REAL_GETADDRINFO = socket.getaddrinfo


class _SSRFBlocked(socket.gaierror):
    pass


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    infos = _REAL_GETADDRINFO(host, port, *args, **kwargs)
    # If `host` itself is literally one of our blocked metadata names,
    # bail before returning results. (Belt-and-suspenders; the URL
    # validator already catches these by name.)
    if isinstance(host, str) and host.lower() in _BLOCKED_HOSTS_LITERAL:
        raise _SSRFBlocked(f"blocked host: {host}")
    for family, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
        except ValueError:
            continue
        if _ip_is_internal(ip):
            raise _SSRFBlocked(f"blocked internal IP: {ip_str}")
    return infos


def _install_socket_ssrf_guard() -> None:
    # Idempotent: if our wrapper is already installed (re-import in
    # tests) leave it alone.
    if getattr(socket.getaddrinfo, "_cmvm_ssrf_guarded", False):
        return
    _guarded_getaddrinfo._cmvm_ssrf_guarded = True  # type: ignore[attr-defined]
    socket.getaddrinfo = _guarded_getaddrinfo  # type: ignore[assignment]
    log.info("Installed socket-level SSRF guard")


_install_socket_ssrf_guard()


def _friendly_ydl_error(e: Exception) -> str:
    """yt-dlp errors are long and Pythonic. Distill them into the
    one line that a visitor on cmvideo.online can act on."""
    msg = str(e).strip().splitlines()[-1] if str(e) else ""
    low = msg.lower()
    yt_blocked = any(needle in low for needle in (
        "sign in to confirm",
        "not a bot",
        "confirm you're not a bot",
        "unexpected_eof_while_reading",
        "eof occurred in violation",
        "unable to download api page",
        "http error 403",
        "http error 429",
        "failed to extract any player response",
    ))
    if yt_blocked:
        return (
            "YouTube is rate-limiting this free server right now. "
            "Most other sites still work (Vimeo, Reddit, Twitter, TikTok, ~1,800 total), "
            "or grab the desktop app below \u2014 it runs from your own connection and isn't affected."
        )
    if "unavailable" in low or "private video" in low or "video unavailable" in low:
        return "That video isn't available (private, region-locked, or removed)."
    if "unsupported url" in low:
        return "That site isn't supported. The full desktop app uses the same yt-dlp under the hood, so it won't help either \u2014 try a direct video URL."
    return msg or "Couldn't fetch that URL."


def _safe_name(stem: str, fmt: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:80] or "cmvideo-mini"
    return f"{cleaned}.{fmt}"


def _media_type(fmt: str) -> str:
    return "video/mp4" if fmt == "mp4" else "audio/mpeg"


# ---- YouTube embed-and-censor ---------------------------------------------
# Legal/safe alternative to downloading: we fetch the transcript via the
# (public, no-auth, no-API-key) timedtext endpoint that YouTube serves
# to its own player, run it through our wordlists, and return the list
# of (start, end) intervals plus the video_id. The frontend then embeds
# the official YouTube iframe player and schedules mute()/unMute()
# calls. We never download the video itself.
YOUTUBE_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|v/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})",
    re.IGNORECASE,
)


def _extract_youtube_id(url: str) -> str | None:
    m = YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None


_TRANSCRIPT_TOKEN_RE = re.compile(r"[^\w']+")


def _normalize_token(tok: str) -> str:
    return _TRANSCRIPT_TOKEN_RE.sub("", tok).lower()


def _transcript_intervals(video_id: str, words: set) -> list[dict]:
    """Fetch the English transcript for `video_id` and return a list
    of {start, end, word} dicts for every token whose normalised form
    is in `words`. Token-level timestamps are interpolated within
    each transcript segment.

    Uses youtube-transcript-api v1.x which returns FetchedTranscript
    objects (iterable of FetchedTranscriptSnippet with .text /
    .start / .duration attrs)."""
    from youtube_transcript_api import YouTubeTranscriptApi
    from youtube_transcript_api._errors import (
        TranscriptsDisabled,
        NoTranscriptFound,
        VideoUnavailable,
        AgeRestricted,
        IpBlocked,
        RequestBlocked,
        PoTokenRequired,
        CouldNotRetrieveTranscript,
        VideoUnplayable,
        YouTubeTranscriptApiException,
    )
    api = YouTubeTranscriptApi()
    try:
        fetched = api.fetch(video_id, languages=["en", "en-US", "en-GB"])
    except (TranscriptsDisabled, NoTranscriptFound):
        raise HTTPException(
            status_code=400,
            detail="This video doesn\u2019t have English captions, so we can\u2019t tell which words to mute. Try a different video, or use the desktop app for full Whisper transcription."
        )
    except (VideoUnavailable, VideoUnplayable):
        raise HTTPException(status_code=400, detail="That video isn\u2019t available (private, region-locked, or removed).")
    except AgeRestricted:
        raise HTTPException(status_code=400, detail="That video is age-restricted, which blocks transcript access. Try a non-restricted upload, or use the desktop app.")
    except (IpBlocked, RequestBlocked, PoTokenRequired):
        raise HTTPException(
            status_code=502,
            detail="YouTube blocked the transcript fetch from this server. Try again in a minute, or use the desktop app \u2014 it runs from your own connection."
        )
    except CouldNotRetrieveTranscript:
        raise HTTPException(
            status_code=502,
            detail="Couldn\u2019t fetch the transcript. Try a different video or use the desktop app."
        )
    except YouTubeTranscriptApiException as e:
        # Cookie / consent / catch-all from the library hierarchy that
        # doesn't inherit from CouldNotRetrieveTranscript and so escapes
        # every clause above. Log the concrete type server-side so we
        # can see in HF Space logs what's actually being thrown.
        log.warning("yt-transcript library exception %s: %s", type(e).__name__, e)
        raise HTTPException(
            status_code=502,
            detail="Couldn\u2019t fetch the transcript right now. Try again in a minute, or use the desktop app \u2014 it runs from your own connection."
        )
    except Exception as e:
        # Network-layer (SSL handshake / connection reset / timeout)
        # or parser errors when YouTube returns a captcha / consent
        # page instead of the expected payload. Log the full traceback
        # in HF Space logs but keep the user-visible message generic.
        log.exception(
            "yt-transcript fetch crashed for video_id=%s: %s",
            video_id, type(e).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail="Couldn\u2019t fetch the transcript right now. Try again in a minute, or use the desktop app \u2014 it runs from your own connection."
        )

    hits = []
    for seg in fetched:
        text = (getattr(seg, "text", "") or "").replace("\n", " ")
        if not text.strip():
            continue
        toks = text.split()
        if not toks:
            continue
        start = float(getattr(seg, "start", 0.0) or 0.0)
        dur = float(getattr(seg, "duration", 0.0) or 0.0) or 0.5
        # Even distribution across the segment, with a 150 ms safety
        # pad on each interval so the browser's mute call lands a hair
        # early and we never leak the first phoneme.
        per = dur / len(toks)
        for idx, tok in enumerate(toks):
            norm = _normalize_token(tok)
            if not norm or norm not in words:
                continue
            t0 = start + idx * per
            t1 = t0 + per
            hits.append({
                "start": round(max(0.0, t0 - 0.15), 3),
                "end":   round(t1 + 0.15, 3),
                "word":  norm,
            })
    return hits


# ---- yt-dlp ---------------------------------------------------------------
# YouTube has been increasingly aggressive about blocking datacenter
# IPs with 'Sign in to confirm you're not a bot'. The free HF Space
# IP is squarely in that bucket. These extractor_args pick the player
# clients that still tend to work without a PO Token / login cookie.
# See yt-dlp issue #10128 and friends for the current state of play.
YDL_EXTRACTOR_ARGS = {
    "youtube": {
        "player_client": ["tv", "tv_embedded", "mediaconnect", "web_creator", "mweb"],
        "player_skip": ["configs"],
    },
}
YDL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# YouTube cookies path: if the operator sets the YT_COOKIES_TXT env
# var (HF Space Secret) to the raw contents of a Netscape cookies.txt
# file we materialize it to a tempfile here at import time and feed
# the path to yt-dlp. yt-dlp matches cookies by domain, so passing
# `cookiefile` for non-YouTube URLs is harmless.
def _init_yt_cookies() -> Optional[str]:
    raw = os.environ.get("YT_COOKIES_TXT", "").strip()
    if not raw:
        return None
    fd, path = tempfile.mkstemp(prefix="cmvm_yt_cookies_", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(raw if raw.endswith("\n") else raw + "\n")
    log.info("YT cookies file initialised at %s (%d chars)", path, len(raw))
    return path


YT_COOKIES_FILE = _init_yt_cookies()

def _ydl_common_opts(tmpdir: Path, max_duration: int, max_filesize: int) -> dict:
    opts = {
        "outtmpl": str(tmpdir / "%(title).80s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "max_filesize": max_filesize,
        "match_filter": yt_dlp.utils.match_filter_func(
            f"duration <? {max_duration}"
        ),
        "socket_timeout": 12,
        "retries": 1,
        "concurrent_fragment_downloads": 1,
        "extractor_args": YDL_EXTRACTOR_ARGS,
        "http_headers": {"User-Agent": YDL_USER_AGENT},
    }
    if YT_COOKIES_FILE:
        opts["cookiefile"] = YT_COOKIES_FILE
    return opts


def _video_format_selector(fps: str = "source") -> str:
    h = MAX_VIDEO_HEIGHT
    # FPS clause: empty for "source", [fps<=30] for 30,
    # [fps>=50] for 60 (real 60fps streams sometimes report 59.94).
    # We also fall through to the un-filtered selector so we still get
    # *something* if no format matches the requested fps.
    fps_clause = {
        "source": "",
        "30": "[fps<=30]",
        "60": "[fps>=50]",
    }.get(fps, "")
    return (
        f"bestvideo[height<={h}]{fps_clause}[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={h}]{fps_clause}[ext=mp4]+bestaudio/"
        f"bestvideo[height<={h}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={h}][ext=mp4]+bestaudio/"
        f"best[height<={h}][ext=mp4]/"
        f"best[height<={h}]/best"
    )


def _do_download(url: str, fmt: str, tmpdir: Path, max_duration: int, max_filesize: int, fps: str = "source") -> Path:
    """Try yt-dlp first; if it fails with a recoverable error, fall
    through to gallery-dl / Cobalt / streamlink. Caps are enforced
    inside the yt-dlp options so the cheap path never even starts a
    download that would obviously bust them."""
    opts = _ydl_common_opts(tmpdir, max_duration, max_filesize)
    if fmt == "mp4":
        opts.update({
            "format": _video_format_selector(fps),
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
            ],
        })
    else:
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": AUDIO_BITRATE_KBPS,
                },
            ],
        })

    try:
        result = _extractors.extract_with_fallbacks(
            url,
            tmpdir,
            fmt=fmt,
            ydl_opts=opts,
            on_attempt=lambda tool, msg: log.info("extractor[%s]: %s", tool, msg[:200]),
        )
    except _extractors.ExtractionError as exc:
        # Surface as a yt-dlp DownloadError-shaped exception so the
        # caller's friendly-error mapping still works for the common
        # cases. Tools that ran are listed in the exception text.
        raise yt_dlp.utils.DownloadError(str(exc)) from exc

    if result.path.suffix.lower() != f".{fmt}":
        # gallery-dl / Cobalt may hand us a different container. The
        # caller's mode-specific finalisation path will remux the
        # censor flow; for `download` mode we leave the suffix alone -
        # surface the real container the source provided rather than
        # lying about the format.
        log.info("extractor returned %s (not .%s) - keeping as-is", result.path.suffix, fmt)
    log.info(
        "extractor win: %s via %s [tries: %s]",
        result.title, result.extractor, " | ".join(result.attempts) or "none",
    )
    return result.path


def _do_info(url: str) -> dict:
    info_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 15,
        "extractor_args": YDL_EXTRACTOR_ARGS,
        "http_headers": {"User-Agent": YDL_USER_AGENT},
    }
    if YT_COOKIES_FILE:
        info_opts["cookiefile"] = YT_COOKIES_FILE
    with yt_dlp.YoutubeDL(info_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info is None:
        raise RuntimeError("Could not read media info from that URL.")
    duration = info.get("duration")
    return {
        "title": (info.get("title") or "Untitled")[:200],
        "uploader": (info.get("uploader") or "")[:120],
        "duration": int(duration) if isinstance(duration, (int, float)) else None,
        "thumbnail": info.get("thumbnail"),
        "extractor": info.get("extractor_key"),
        "over_cap_download": isinstance(duration, (int, float)) and duration > MAX_DOWNLOAD_DURATION_SECONDS,
        "over_cap_censor": isinstance(duration, (int, float)) and duration > MAX_CENSOR_DURATION_SECONDS,
    }


# ---- censor pipeline ------------------------------------------------------
def _do_censor(src: Path, fmt: str, mode: str, tmpdir: Path, fps: str = "source") -> Path:
    duration = mini_censor.probe_duration(src)
    if duration > MAX_CENSOR_DURATION_SECONDS + 5:
        raise RuntimeError(
            f"Censoring is capped at {MAX_CENSOR_DURATION_SECONDS // 60} minutes "
            f"on the mini version (this clip is {int(duration // 60)} min). Use the full app."
        )
    words = _wordlist_tokens()
    intervals = mini_censor.find_intervals(src, words)
    dst = tmpdir / f"censored.{fmt}"
    mini_censor.render(src, dst, intervals, mode=mode, fmt=fmt, fps=fps)
    return dst


# ---- file upload ----------------------------------------------------------
async def _save_upload(upload: UploadFile, tmpdir: Path) -> Path:
    suffix = Path(upload.filename or "upload.bin").suffix.lower() or ".bin"
    safe_suffix = suffix if re.match(r"^\.[A-Za-z0-9]{1,8}$", suffix) else ".bin"
    dst = tmpdir / f"upload{safe_suffix}"
    total = 0
    with dst.open("wb") as out:
        while True:
            chunk = await upload.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=f"Upload over the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap.")
            out.write(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="Empty upload.")
    log.info("Received upload: %s (%d bytes)", upload.filename, total)

    # Cheap ffprobe pre-check. If the duration is over our censor cap
    # we want to know now, before whisper / ffmpeg do any heavy
    # parsing on a potentially malformed input. ffprobe returns 0.0
    # on unreadable input and we let the real pipeline raise a more
    # specific error in that case.
    try:
        duration = mini_censor.probe_duration(dst)
    except Exception:
        log.exception("ffprobe pre-check raised")
        duration = 0.0
    if duration and duration > MAX_CENSOR_DURATION_SECONDS + 5:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Upload is {int(duration // 60)} min long; the mini app caps censoring "
                f"at {MAX_CENSOR_DURATION_SECONDS // 60} min. Use the desktop app for the full version."
            ),
        )
    return dst


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
class InfoRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)


@app.get("/", include_in_schema=False)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "max_dl_min": MAX_DOWNLOAD_DURATION_SECONDS // 60,
            "max_dl_mb": MAX_DOWNLOAD_FILESIZE_BYTES // (1024 * 1024),
            "max_censor_min": MAX_CENSOR_DURATION_SECONDS // 60,
            "max_censor_mb": MAX_CENSOR_FILESIZE_BYTES // (1024 * 1024),
            "max_video_height": MAX_VIDEO_HEIGHT,
            "audio_bitrate_kbps": AUDIO_BITRATE_KBPS,
            "rate_limit": RATE_LIMIT_PER_HOUR,
        },
    )


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True, "words": len(_wordlist_tokens()), "yt_cookies": bool(YT_COOKIES_FILE)}


@app.post("/api/info")
@limiter.limit("120/hour")
async def api_info(request: Request, body: InfoRequest):
    url = _validate_url(body.url)
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_do_info, url), timeout=25)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Source took too long to respond.")
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=_friendly_ydl_error(e))
    except HTTPException:
        raise
    except Exception:
        # Don't leak the raw exception string - it can include paths
        # or internal state. Server logs keep the full traceback.
        log.exception("info failed")
        raise HTTPException(status_code=400, detail="Couldn't read that URL.")
    return JSONResponse(info)


@app.post("/api/process")
@limiter.limit(RATE_LIMIT_PER_HOUR)
async def api_process(
    request: Request,
    format: str = Form(...),
    mode: str = Form(...),
    url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    fps: str = Form("source"),
):
    fmt = (format or "").lower().strip()
    md = (mode or "").lower().strip()
    fps_choice = (fps or "source").lower().strip()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail="Format must be 'mp4' or 'mp3'.")
    if md not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail="Mode must be 'download', 'silence', or 'beep'.")
    if fps_choice not in ALLOWED_FPS:
        raise HTTPException(status_code=400, detail="FPS must be 'source', '30', or '60'.")
    # FPS is video-only; ignore the field for MP3.
    if fmt == "mp3":
        fps_choice = "source"

    have_url = bool(url and url.strip())
    have_file = file is not None and bool(file.filename)
    if have_url == have_file:
        raise HTTPException(status_code=400, detail="Provide either a URL or a file, not both / neither.")

    if md == "download" and have_file:
        raise HTTPException(status_code=400, detail="Download mode is URL-only - you already have the file locally.")

    # Early Content-Length gate for uploads. The streaming check in
    # `_save_upload` is the real enforcer (clients can lie about
    # Content-Length, and chunked uploads have no length), but rejecting
    # obviously-too-big requests here means we never even allocate the
    # tempdir or open a write handle.
    if have_file:
        try:
            cl = int(request.headers.get("content-length", "0"))
        except ValueError:
            cl = 0
        # Allow a little slack for multipart envelope overhead.
        if cl and cl > MAX_UPLOAD_BYTES + (1 << 20):
            raise HTTPException(status_code=413, detail=f"Upload over the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap.")

    tmpdir = Path(tempfile.mkdtemp(prefix="cmvm_"))
    try:
        # 1) Get the source media into tmpdir.
        if have_url:
            clean_url = _validate_url(url)
            max_dur = MAX_DOWNLOAD_DURATION_SECONDS if md == "download" else MAX_CENSOR_DURATION_SECONDS
            max_size = MAX_DOWNLOAD_FILESIZE_BYTES if md == "download" else MAX_CENSOR_FILESIZE_BYTES
            try:
                src = await asyncio.wait_for(
                    asyncio.to_thread(_do_download, clean_url, fmt, tmpdir, max_dur, max_size, fps_choice),
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                raise HTTPException(status_code=504, detail=f"Source download exceeded the {DOWNLOAD_TIMEOUT_SECONDS}s mini-app cap.")
            except yt_dlp.utils.DownloadError as e:
                msg = str(e).splitlines()[-1]
                if "max-filesize" in msg.lower():
                    raise HTTPException(status_code=413, detail=f"Source exceeds the {max_size // (1024 * 1024)} MB mini-app cap.")
                raise HTTPException(status_code=400, detail=_friendly_ydl_error(e))
        else:
            src = await _save_upload(file, tmpdir)
            if src.stat().st_size > MAX_CENSOR_FILESIZE_BYTES:
                raise HTTPException(status_code=413, detail=f"Upload exceeds the {MAX_CENSOR_FILESIZE_BYTES // (1024 * 1024)} MB cap.")

        # 2) Either return it as-is (download) or run the censor pipeline.
        if md == "download":
            out_path = src
        else:
            try:
                out_path = await asyncio.wait_for(
                    asyncio.to_thread(_do_censor, src, fmt, md, tmpdir, fps_choice),
                    timeout=CENSOR_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                raise HTTPException(
                    status_code=504,
                    detail=f"Censoring exceeded the {CENSOR_TIMEOUT_SECONDS}s mini-app cap. Try a shorter clip or use the desktop app.",
                )
            except RuntimeError as e:
                raise HTTPException(status_code=400, detail=str(e))

        # 3) Final filesize gate (catches outputs that grew during re-encode).
        size = out_path.stat().st_size
        active_cap = MAX_DOWNLOAD_FILESIZE_BYTES if md == "download" else MAX_CENSOR_FILESIZE_BYTES
        if size > active_cap:
            raise HTTPException(status_code=413, detail=f"Output exceeds the {active_cap // (1024 * 1024)} MB cap.")

        stem = src.stem if md == "download" else f"{src.stem}-{md}"
        return FileResponse(
            path=str(out_path),
            media_type=_media_type(fmt),
            filename=_safe_name(stem, fmt),
            background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
        )
    except HTTPException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Server logs keep the real traceback; client gets a stable
        # message that doesn't leak paths or library internals.
        log.exception("process failed")
        raise HTTPException(status_code=500, detail="Mini app hit an internal error. Try again or use the desktop app.")


@app.post("/api/download", include_in_schema=False)
@limiter.limit(RATE_LIMIT_PER_HOUR)
async def api_download_compat(
    request: Request,
    format: str = Form(...),
    url: str = Form(...),
):
    """Back-compat shim for old clients that used the JSON /api/download.
    Delegates to /api/process with mode=download."""
    return await api_process(request=request, format=format, mode="download", url=url, file=None)


class YTCensorRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)


@app.post("/api/yt-censor")
@limiter.limit("120/hour")
async def api_yt_censor(request: Request, body: YTCensorRequest):
    """Return mute intervals + video_id for a YouTube URL so the
    frontend can embed the official iframe player and overlay
    client-side mute scheduling. Never touches video bytes."""
    url = _validate_url(body.url)
    vid = _extract_youtube_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="That doesn't look like a YouTube URL.")
    words = _wordlist_tokens()
    try:
        intervals = await asyncio.wait_for(
            asyncio.to_thread(_transcript_intervals, vid, words),
            timeout=20,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Transcript fetch took too long. Try again in a minute.")
    return JSONResponse({
        "video_id": vid,
        "embed_url": f"https://www.youtube.com/embed/{vid}?enablejsapi=1&rel=0&modestbranding=1",
        "intervals": intervals,
        "interval_count": len(intervals),
    })


@app.get("/api/limits", include_in_schema=False)
async def api_limits():
    return {
        "max_download_duration_seconds": MAX_DOWNLOAD_DURATION_SECONDS,
        "max_download_filesize_bytes": MAX_DOWNLOAD_FILESIZE_BYTES,
        "max_censor_duration_seconds": MAX_CENSOR_DURATION_SECONDS,
        "max_censor_filesize_bytes": MAX_CENSOR_FILESIZE_BYTES,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_video_height": MAX_VIDEO_HEIGHT,
        "audio_bitrate_kbps": AUDIO_BITRATE_KBPS,
        "rate_limit": RATE_LIMIT_PER_HOUR,
        "modes": sorted(ALLOWED_MODES),
        "formats": sorted(ALLOWED_FORMATS),
        "yt_cookies_loaded": bool(YT_COOKIES_FILE),
        "yt_censor_enabled": True,
        "full_app_url": "https://cmvideo.online",
        "extractors": _extractors.available_tools(),
        "extractor_versions": _extractors.tool_versions(),
        "memory": _extractors.memory_status(),
    }
