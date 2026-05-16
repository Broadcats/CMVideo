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
MAX_CENSOR_DURATION_SECONDS = 8 * 60             # transcription is slow on free CPU
MAX_CENSOR_FILESIZE_BYTES = 100 * 1024 * 1024    # cap upload + output
MAX_UPLOAD_BYTES = 100 * 1024 * 1024             # multipart body size
AUDIO_BITRATE_KBPS = "320"  # highest standard MP3 bitrate (LAME -V0 / CBR)

# Quality tiers. "standard" stays the default because the speed wins
# from 0.4.11 (single-file progressive MP4, no merge step) only apply
# at 720p - bumping the default to 1080p makes first-impression
# pulls slower for everyone. HD is opt-in.
ALLOWED_QUALITY = {"standard", "hd"}
DEFAULT_QUALITY = "standard"
QUALITY_HEIGHTS = {"standard": 720, "hd": 1080}
# 1080p source can run 1.3-2.7 GB/hr on AVC; bump the per-job size
# cap accordingly. HF Spaces' ephemeral disk is ~50 GB so even three
# concurrent HD pulls (~4.5 GB tmp) is fine.
QUALITY_DOWNLOAD_CAPS = {
    "standard": 800 * 1024 * 1024,
    "hd":      1_500 * 1024 * 1024,
}
# Back-compat: anywhere downstream still references the legacy
# constants we keep them pointing at the standard tier.
MAX_VIDEO_HEIGHT = QUALITY_HEIGHTS[DEFAULT_QUALITY]
MAX_DOWNLOAD_FILESIZE_BYTES = QUALITY_DOWNLOAD_CAPS[DEFAULT_QUALITY]

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

def _ydl_common_opts(
    tmpdir: Path,
    max_duration: int,
    max_filesize: int,
    *,
    progress_hook=None,
) -> dict:
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
        # Pull HLS / DASH segments in parallel. yt-dlp defaults to 1
        # which is the bottleneck on Twitch / IG / TikTok and most
        # of the *.tube tubes (each segment is ~6 s; 4-way parallel
        # turns a 10-min wall time into ~2.5 min on the same link).
        # 4 is the sweet spot for HF Spaces' tiny bandwidth budget -
        # higher counts thrash the limited egress and don't help.
        "concurrent_fragment_downloads": 4,
        # Fragment-level retries: many tubes serve some segments off
        # a CDN that flaps. Two retries per fragment (default 10) is
        # the balance between giving up too soon and burning timeout
        # on a fundamentally bad mirror.
        "fragment_retries": 2,
        # Don't stop the whole job on a single failed fragment. We
        # can usually drop one and still produce a watchable file.
        "skip_unavailable_fragments": True,
        "extractor_args": YDL_EXTRACTOR_ARGS,
        "http_headers": {"User-Agent": YDL_USER_AGENT},
    }
    if YT_COOKIES_FILE:
        opts["cookiefile"] = YT_COOKIES_FILE
    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]
    return opts


def _video_format_selector(fps: str = "source", *, height: int | None = None) -> str:
    """Pick the format yt-dlp should download - QUALITY FIRST.

    History note: in 0.4.11 we ordered this selector to prefer
    progressive MP4 ("best[ext=mp4][acodec!=none]") because a
    single-file download avoids the ~10-30 s ffmpeg merge step at
    the end. That assumption was wrong for YouTube specifically,
    where progressive MP4 only goes up to 360p - everything 720p+
    is served as separate video/audio tracks. So the selector was
    silently capping YouTube downloads at 360p regardless of which
    height the user asked for. Same story on a number of other
    sites that ship progressive at 480p max.

    yt-dlp picks the FIRST matching clause in the chain. So we now
    order clauses by QUALITY, not by I/O cost. The progressive
    no-merge path is still here, but it only fires when the
    progressive variant is actually AT the chosen height (i.e. the
    site really does ship a single-file MP4 at 720p / 1080p, like
    most non-YouTube CDNs). Otherwise we take the split-track path
    and pay the merge cost - which on the free Space is 10-30 s
    for a ~150 MB file, well under the 360 s download cap.

    `height` is the resolution cap. Defaults to 720 (`standard`).
    When `height` is 1080 we still embed a 720 fallback at the
    bottom of the chain so an HD request against a source that
    doesn't expose 1080 produces a file rather than failing.
    """
    h = int(height or MAX_VIDEO_HEIGHT)
    # FPS clause: empty for "source", [fps<=30] for 30,
    # [fps>=50] for 60 (real 60fps streams sometimes report 59.94).
    fps_clause = {
        "source": "",
        "30": "[fps<=30]",
        "60": "[fps>=50]",
    }.get(fps, "")

    def tier(target: int) -> str:
        # bestvideo* (with star) lets yt-dlp pick whichever variant
        # is best at this height, including progressive-with-audio
        # streams. That means on a site that only ships a single
        # 720p MP4 (no separate tracks), bestvideo*[height<=720]
        # naturally picks that progressive file and the
        # +bestaudio is a no-op (yt-dlp drops the redundant fetch).
        # On YouTube where 720p only exists as split tracks, the
        # same expression picks the 720p video-only AVC and merges
        # it with the best m4a. Either way: we get the right height.
        return (
            # 1) Best video at the cap, AVC/m4a where possible (mp4
            #    output without a transcode).
            f"bestvideo*[height<={target}]{fps_clause}[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            f"bestvideo*[height<={target}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
            # 2) Best video at the cap, mp4 video container, any audio.
            f"bestvideo*[height<={target}]{fps_clause}[ext=mp4]+bestaudio/"
            f"bestvideo*[height<={target}][ext=mp4]+bestaudio/"
            # 3) Best video at the cap, ANY container (webm/etc.) +
            #    best audio. yt-dlp will remux to mp4 via the
            #    FFmpegVideoRemuxer postprocessor we set in opts.
            f"bestvideo*[height<={target}]{fps_clause}+bestaudio/"
            f"bestvideo*[height<={target}]+bestaudio/"
            # 4) Single-file at the EXACT cap height. This is the
            #    no-merge fast path for sites that ship progressive
            #    720p / 1080p (most non-YouTube CDNs). It's clause
            #    4, not clause 1, because we only want it when the
            #    progressive resolution actually matches the cap -
            #    not when it's a low-res fallback that happens to
            #    sneak in under <=H.
            f"best[height={target}]{fps_clause}[ext=mp4][acodec!=none]/"
            f"best[height={target}][ext=mp4][acodec!=none]/"
            # 5) Last resort at this tier - any single-file <=H.
            f"best[height<={target}]"
        )

    base = tier(h)
    # If HD was asked for and 1080 isn't available, fall through
    # the entire 720 tier rather than failing. yt-dlp picks the
    # first chain that matches.
    if h > 720:
        base += "/" + tier(720)
    return base + "/best"


# Direct-media-URL fast path: extensions that point straight at a
# downloadable file (no yt-dlp scrape needed). When the URL ends in
# one of these AND the host responds with a matching Content-Type,
# we stream the bytes ourselves and skip the entire extractor chain.
# Cuts ~3-5 s of yt-dlp init + content scrape, and (more importantly)
# avoids needless ffmpeg remuxing on `_do_download` for URLs that
# already point at the exact file the user wants.
_DIRECT_MEDIA_EXTS = (
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv",
    ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac",
)


def _direct_url_fast_path(
    url: str,
    fmt: str,
    tmpdir: Path,
    max_filesize: int,
    *,
    progress_hook=None,
) -> Path | None:
    """If `url` looks like it points straight at a media file and the
    requested `fmt` matches its container, stream it to disk and
    return the saved path. Returns None when the URL doesn't qualify
    or the HEAD probe fails - the caller then falls through to the
    full extractor chain.

    Uses `_validate_url`'s SSRF-checked scheme + host gate already, so
    we just have to enforce the size cap.
    """
    try:
        path_lower = urlparse(url).path.lower()
    except Exception:  # noqa: BLE001
        return None
    if not any(path_lower.endswith(ext) for ext in _DIRECT_MEDIA_EXTS):
        return None
    # Only fast-path when the URL ext matches the requested format.
    # mp4 mode wants a video container; mp3 mode is happy with any
    # audio file (we re-encode anyway in the postproc step).
    src_ext = "." + path_lower.rsplit(".", 1)[-1]
    if fmt == "mp4" and src_ext not in (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv"):
        return None
    if fmt == "mp3" and src_ext not in (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"):
        return None

    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": YDL_USER_AGENT})
    try:
        # HEAD-style probe on GET so we can stream the body if the
        # response looks legit. Some servers don't honour HEAD.
        resp = urllib.request.urlopen(req, timeout=12)
    except Exception:  # noqa: BLE001
        return None

    try:
        ct = (resp.headers.get("Content-Type") or "").lower()
        cl = resp.headers.get("Content-Length") or "0"
        try:
            total = int(cl) if cl.isdigit() else 0
        except ValueError:
            total = 0
    except Exception:  # noqa: BLE001
        resp.close()
        return None

    # Reject obvious mismatches before we burn bandwidth: a URL that
    # ends in .mp4 but returns text/html is a redirect-to-page.
    if not ct.startswith(("video/", "audio/", "application/octet-stream", "binary/octet-stream")):
        resp.close()
        return None

    if total and total > max_filesize:
        resp.close()
        raise yt_dlp.utils.DownloadError(
            f"Source exceeds the {max_filesize // (1024*1024)} MB mini-app cap (max-filesize)."
        )

    # Stream to disk in 1 MB chunks; abort if we cross max_filesize
    # mid-flight (the server lied or didn't send Content-Length).
    out = tmpdir / f"direct{src_ext}"
    written = 0
    chunk_size = 1 << 20
    last_emit = 0.0
    try:
        with out.open("wb") as fh:
            while True:
                buf = resp.read(chunk_size)
                if not buf:
                    break
                fh.write(buf)
                written += len(buf)
                if written > max_filesize:
                    raise yt_dlp.utils.DownloadError(
                        f"Source exceeds the {max_filesize // (1024*1024)} MB mini-app cap (mid-stream)."
                    )
                # Throttle progress emit so we don't hammer the lock.
                now = time.monotonic()
                if progress_hook and (now - last_emit) > 0.25:
                    last_emit = now
                    try:
                        progress_hook({
                            "status": "downloading",
                            "downloaded_bytes": written,
                            "total_bytes": total or written,
                        })
                    except Exception:  # noqa: BLE001
                        pass
        if progress_hook:
            try:
                progress_hook({"status": "finished",
                               "downloaded_bytes": written,
                               "total_bytes": total or written})
            except Exception:  # noqa: BLE001
                pass
    finally:
        resp.close()

    log.info("direct fast-path: %s (%d bytes, ext=%s)", url[:120], written, src_ext)
    return out


def _do_download(
    url: str,
    fmt: str,
    tmpdir: Path,
    max_duration: int,
    max_filesize: int,
    fps: str = "source",
    *,
    height: int | None = None,
    progress_hook=None,
    on_attempt=None,
) -> Path:
    """Try yt-dlp first; if it fails with a recoverable error, fall
    through to gallery-dl / Cobalt / streamlink. Caps are enforced
    inside the yt-dlp options so the cheap path never even starts a
    download that would obviously bust them.

    `progress_hook` is yt-dlp's own callback shape (a dict with
    ``status``, ``downloaded_bytes``, ``total_bytes``, ...). Callers
    use it to drive the job-state progress bar.
    """
    # Tier 0: direct media URL. Skips yt-dlp init + scrape + remux for
    # plain http(s)://.../video.mp4 style links. Returns None if the
    # URL doesn't qualify so we fall through to the chain.
    direct = _direct_url_fast_path(url, fmt, tmpdir, max_filesize, progress_hook=progress_hook)
    if direct is not None:
        log.info("extractor win: direct fast-path -> %s", direct.name)
        return direct
    opts = _ydl_common_opts(tmpdir, max_duration, max_filesize, progress_hook=progress_hook)
    if fmt == "mp4":
        opts.update({
            "format": _video_format_selector(fps, height=height),
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

    def _wrap_attempt(tool, msg):  # type: ignore[no-untyped-def]
        log.info("extractor[%s]: %s", tool, msg[:200])
        if on_attempt is not None:
            try:
                on_attempt(tool, msg)
            except Exception:  # noqa: BLE001
                pass

    try:
        result = _extractors.extract_with_fallbacks(
            url,
            tmpdir,
            fmt=fmt,
            ydl_opts=opts,
            on_attempt=_wrap_attempt,
            target_height=int(height or MAX_VIDEO_HEIGHT),
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
def _do_censor(
    src: Path,
    fmt: str,
    mode: str,
    tmpdir: Path,
    fps: str = "source",
    *,
    on_transcribe_progress=None,
    on_render_progress=None,
) -> Path:
    duration = mini_censor.probe_duration(src)
    if duration > MAX_CENSOR_DURATION_SECONDS + 5:
        raise RuntimeError(
            f"Censoring is capped at {MAX_CENSOR_DURATION_SECONDS // 60} minutes "
            f"on the mini version (this clip is {int(duration // 60)} min). Use the full app."
        )
    words = _wordlist_tokens()
    intervals = mini_censor.find_intervals(
        src, words,
        progress=on_transcribe_progress,
        total_duration=duration,
    )
    dst = tmpdir / f"censored.{fmt}"
    mini_censor.render(
        src, dst, intervals, mode=mode, fmt=fmt, fps=fps,
        progress=on_render_progress,
        total_duration=duration,
    )
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
            "qualities": sorted(ALLOWED_QUALITY),
            "default_quality": DEFAULT_QUALITY,
            "quality_heights": dict(QUALITY_HEIGHTS),
            "audio_bitrate_kbps": AUDIO_BITRATE_KBPS,
            "rate_limit": RATE_LIMIT_PER_HOUR,
        },
    )


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True, "words": len(_wordlist_tokens()), "yt_cookies": bool(YT_COOKIES_FILE)}


# ---------------------------------------------------------------------------
# Async job model
#
# `/api/process` historically ran the whole pipeline inside the HTTP
# request, which meant the client had nothing to poll - hence "Pulling
# MP4... typically 10-60 sec." sitting on screen until the response
# either arrived or timed out. The job model below moves the pipeline
# into a background coroutine and exposes:
#
#   POST /api/process?async=1           -> {"job_id": "..."}
#   GET  /api/jobs/{id}                 -> {"stage", "pct", "ready",
#                                           "error", "filename"}
#   GET  /api/jobs/{id}/file            -> the finished file (then
#                                          cleans the tmpdir up)
#
# State lives in-process (dict + lock). HF Spaces gives us 1 worker so
# this is fine; a multi-worker deployment would need Redis. Jobs are
# garbage-collected after JOB_TTL_SECONDS regardless of fetch state to
# stop a forgotten upload from squatting on disk forever.
# ---------------------------------------------------------------------------
import secrets
import threading
import time
from dataclasses import dataclass, field

JOB_TTL_SECONDS = 30 * 60   # 30 min - longest a finished job sits unfetched
# Concurrency tuned to the HF Spaces free tier (~2 shared vCPUs). 8
# concurrent ffmpeg encodes thrash the CPU; 3 is the steady-state max
# the box can sustain while keeping each pull responsive. Override
# via env when running on bigger boxes.
JOB_MAX_INFLIGHT = int(os.environ.get("CMVIDEO_JOB_MAX_INFLIGHT", "3"))
# One job per IP at a time. They still get RATE_LIMIT_PER_HOUR worth
# of submissions per hour, just sequentially - which is fairer when
# the box is hot AND prevents one client from monopolising the queue.
JOB_MAX_PER_IP = int(os.environ.get("CMVIDEO_JOB_MAX_PER_IP", "1"))

# Per-IP failure window: if an IP racks up this many failed (errored)
# jobs inside FAILURE_WINDOW_S we put the IP in a soft cooldown. Stops
# a client (botnet or otherwise) from burning job slots with attempts
# that 4xx-out instantly. Tunable for ops, but the defaults are sane.
FAILURE_THRESHOLD = int(os.environ.get("CMVIDEO_FAILURE_THRESHOLD", "5"))
FAILURE_WINDOW_S = int(os.environ.get("CMVIDEO_FAILURE_WINDOW_S", "600"))
FAILURE_COOLDOWN_S = int(os.environ.get("CMVIDEO_FAILURE_COOLDOWN_S", "300"))


@dataclass
class JobState:
    job_id: str
    created_at: float = field(default_factory=lambda: time.monotonic())
    stage: str = "queued"           # queued / fetching / transcribing / rendering / ready / error
    pct: int = 0                    # 0..100 within the current stage
    ready: bool = False
    error: Optional[str] = None     # client-friendly message; full traceback stays in logs
    filename: str = ""              # suggested download name, populated when ready
    # Live download telemetry. Surfaced through /api/jobs/{id} so the
    # frontend can render `2.4 MB/s · 14s left` next to the progress
    # bar - much more reassuring than a percentage in isolation.
    bytes_done: int = 0
    bytes_total: int = 0
    speed_bps: float = 0.0
    eta_s: Optional[int] = None
    _tmpdir: Optional[Path] = None
    _out_path: Optional[Path] = None
    _media_type: str = "application/octet-stream"
    _format: str = "mp4"
    _client_ip: str = ""


_jobs_lock = threading.Lock()
_jobs: dict[str, JobState] = {}


def _gc_jobs() -> None:
    """Drop jobs older than the TTL. Best-effort - holds the lock for
    a couple of microseconds."""
    cutoff = time.monotonic() - JOB_TTL_SECONDS
    expired: list[JobState] = []
    with _jobs_lock:
        for jid, j in list(_jobs.items()):
            if j.created_at < cutoff:
                expired.append(j)
                _jobs.pop(jid, None)
    for j in expired:
        if j._tmpdir and j._tmpdir.exists():
            shutil.rmtree(j._tmpdir, ignore_errors=True)


def _create_job(client_ip: str, fmt: str, tmpdir: Path) -> JobState:
    _gc_jobs()
    with _jobs_lock:
        if len(_jobs) >= JOB_MAX_INFLIGHT:
            raise HTTPException(status_code=503, detail="Mini service is busy - try again in a minute.")
        # Per-IP cap: prevents one client from filling all 8 inflight
        # slots and locking everyone else out.
        live_for_ip = sum(
            1 for j in _jobs.values()
            if j._client_ip == client_ip and not j.ready and j.error is None
        )
        if live_for_ip >= JOB_MAX_PER_IP:
            raise HTTPException(
                status_code=429,
                detail=f"You already have {JOB_MAX_PER_IP} jobs in flight. Wait for one to finish.",
            )
        # token_urlsafe(24) is 32 bytes of entropy - the same shape as
        # uuid.uuid4().hex but pulled from os.urandom so the value is
        # deliberately unpredictable. Job IDs double as a bearer token
        # for fetching the file (in addition to the IP scope below).
        job = JobState(
            job_id=secrets.token_urlsafe(24),
            _client_ip=client_ip,
            _format=fmt,
            _tmpdir=tmpdir,
            _media_type=_media_type(fmt),
        )
        _jobs[job.job_id] = job
        return job


def _get_job(job_id: str, *, client_ip: str | None = None) -> JobState:
    """Look a job up by id, optionally enforcing the IP scope.

    Two layers of access control:
      1. The job_id itself is a 32-byte unpredictable token (see
         _create_job) - effectively unguessable.
      2. When `client_ip` is supplied we require it to match the
         creator's. Belt-and-braces: even if a job_id leaks through
         a referer or shoulder-surf, the file is still locked to the
         original requester's network identity.
    """
    with _jobs_lock:
        j = _jobs.get(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="That job has expired or never existed.")
    if client_ip is not None and j._client_ip and j._client_ip != client_ip:
        # Lie about the reason - same response as a missing job_id so
        # an attacker can't tell whether a given id was ever valid.
        raise HTTPException(status_code=404, detail="That job has expired or never existed.")
    return j


def _set_stage(job: JobState, stage: str, pct: int = 0) -> None:
    job.stage = stage
    job.pct = max(0, min(100, int(pct)))


async def _run_pipeline_async(
    job: JobState,
    *,
    md: str,
    fmt: str,
    fps_choice: str,
    quality_choice: str,
    have_url: bool,
    clean_url: Optional[str],
    saved_upload: Optional[Path],
) -> None:
    """Background pipeline that drives the JobState. Called via
    `asyncio.create_task` from `/api/process?async=1`.

    Mirrors the synchronous code path in `api_process` but tags each
    stage so the client can render a progress bar."""
    tmpdir = job._tmpdir
    assert tmpdir is not None
    src: Optional[Path] = None
    try:
        if have_url:
            assert clean_url is not None
            max_dur = MAX_DOWNLOAD_DURATION_SECONDS if md == "download" else MAX_CENSOR_DURATION_SECONDS
            # HD download mode lifts the per-job size cap to fit
            # 1080p clips. Censor mode is unaffected because the
            # transcript step is the bottleneck, not the bytes.
            quality_cap = QUALITY_DOWNLOAD_CAPS.get(quality_choice, MAX_DOWNLOAD_FILESIZE_BYTES)
            max_size = quality_cap if md == "download" else MAX_CENSOR_FILESIZE_BYTES
            target_height = QUALITY_HEIGHTS.get(quality_choice, MAX_VIDEO_HEIGHT)

            # Track download speed across hook invocations so the
            # frontend can render `2.4 MB/s · 14s left` instead of a
            # bare percentage. yt-dlp emits `speed` and `eta` itself
            # but only for some extractors; we compute our own as a
            # fallback.
            _yt_state = {"last_t": 0.0, "last_bytes": 0, "ema_bps": 0.0}

            def _yt_progress(d):  # type: ignore[no-untyped-def]
                if not isinstance(d, dict):
                    return
                status = d.get("status")
                if status == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    done = d.get("downloaded_bytes") or 0
                    # Speed: prefer yt-dlp's own value, else compute
                    # an EMA of bytes/sec so the displayed rate
                    # doesn't jitter wildly between segments.
                    speed = d.get("speed") or 0.0
                    if not speed:
                        now = time.monotonic()
                        if _yt_state["last_t"]:
                            dt = max(1e-3, now - _yt_state["last_t"])
                            db = max(0, done - _yt_state["last_bytes"])
                            inst = db / dt
                            ema = _yt_state["ema_bps"] or inst
                            _yt_state["ema_bps"] = ema * 0.6 + inst * 0.4
                            speed = _yt_state["ema_bps"]
                        _yt_state["last_t"] = now
                        _yt_state["last_bytes"] = done
                    eta = d.get("eta")
                    if not eta and speed and total and total > done:
                        eta = int((total - done) / speed)
                    job.bytes_done = int(done)
                    job.bytes_total = int(total or 0)
                    job.speed_bps = float(speed or 0.0)
                    job.eta_s = int(eta) if eta else None
                    if total and total > 0:
                        pct = int(done * 100 / total)
                        _set_stage(job, "fetching", min(90, pct))
                    else:
                        _set_stage(job, "fetching", min(85, max(job.pct, 5) + 1))
                elif status == "finished":
                    _set_stage(job, "fetching", 95)

            def _on_attempt(tool, msg):  # type: ignore[no-untyped-def]
                # When yt-dlp gives up and we fall through to other
                # extractors, reset to a coarse-grained fetching pct
                # so the bar starts climbing again instead of looking
                # stuck at whatever yt-dlp got to.
                _set_stage(job, "fetching", max(5, job.pct // 2))

            _set_stage(job, "fetching", 1)
            try:
                src = await asyncio.wait_for(
                    asyncio.to_thread(
                        _do_download,
                        clean_url, fmt, tmpdir, max_dur, max_size, fps_choice,
                        height=target_height,
                        progress_hook=_yt_progress,
                        on_attempt=_on_attempt,
                    ),
                    timeout=DOWNLOAD_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                job.error = f"Source download exceeded the {DOWNLOAD_TIMEOUT_SECONDS}s mini-app cap."
                job.stage = "error"
                return
            except yt_dlp.utils.DownloadError as e:
                msg = str(e).splitlines()[-1]
                if "max-filesize" in msg.lower():
                    job.error = f"Source exceeds the {max_size // (1024 * 1024)} MB mini-app cap."
                else:
                    job.error = _friendly_ydl_error(e)
                job.stage = "error"
                return
            _set_stage(job, "fetching", 100)
        else:
            # Upload was already saved to disk synchronously by the
            # request handler before the job kicked off (uploads can
            # be huge and we want backpressure to flow through the
            # HTTP layer, not into a background queue).
            assert saved_upload is not None
            src = saved_upload

        if md == "download":
            out_path = src
        else:
            def _on_transcribe(pct):  # type: ignore[no-untyped-def]
                _set_stage(job, "transcribing", pct)

            def _on_render(pct):  # type: ignore[no-untyped-def]
                _set_stage(job, "rendering", pct)

            _set_stage(job, "transcribing", 0)
            try:
                out_path = await asyncio.wait_for(
                    asyncio.to_thread(
                        _do_censor, src, fmt, md, tmpdir, fps_choice,
                        on_transcribe_progress=_on_transcribe,
                        on_render_progress=_on_render,
                    ),
                    timeout=CENSOR_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                job.error = (
                    f"Censoring exceeded the {CENSOR_TIMEOUT_SECONDS}s mini-app cap. "
                    "Try a shorter clip or use the desktop app."
                )
                job.stage = "error"
                return
            except RuntimeError as e:
                job.error = str(e)
                job.stage = "error"
                return
            _set_stage(job, "rendering", 100)

        # Final filesize gate (catches outputs that grew during re-encode).
        size = out_path.stat().st_size
        active_cap = MAX_DOWNLOAD_FILESIZE_BYTES if md == "download" else MAX_CENSOR_FILESIZE_BYTES
        if size > active_cap:
            job.error = f"Output exceeds the {active_cap // (1024 * 1024)} MB cap."
            job.stage = "error"
            return

        stem = src.stem if md == "download" else f"{src.stem}-{md}"
        job.filename = _safe_name(stem, fmt)
        job._out_path = out_path
        _set_stage(job, "ready", 100)
        job.ready = True
    except HTTPException as exc:
        job.error = exc.detail if isinstance(exc.detail, str) else "Job failed."
        job.stage = "error"
    except Exception:  # noqa: BLE001
        log.exception("background job failed: %s", job.job_id)
        job.error = "Mini app hit an internal error. Try again or use the desktop app."
        job.stage = "error"
    finally:
        # Whatever happened, if we ended in `error` count a failure
        # against the creator IP. Stops a client from quietly burning
        # job slots with attempts that 4xx-out.
        if job.stage == "error":
            _record_failure(job._client_ip)


# ---------------------------------------------------------------------------
# Overload protection
#
# Threats we actually face on HF Spaces:
#   * Application-layer floods: bots starting many cheap jobs to
#     thrash ffmpeg / Playwright / Chromium.
#   * "Smart" abusers cycling IPs to evade /hour limits.
#   * Repeated previews of the same URL (e.g. when a thread links the
#     widget) hammering yt-dlp scrapes for one source.
#   * Operator-side overload (the box catches fire for any reason and
#     we need to flip a kill switch fast).
#
# What we DON'T try to defend against here:
#   * Volumetric L3/L4 DDoS - HF / Cloudflare's edge handles it.
#   * Determined bot operators with rotating IPv6 /64s - that needs
#     a CDN-level WAF (Cloudflare, AWS Shield, etc.), out of scope
#     for a free Hugging Face Space.
#
# The defenses below are designed to be cheap, in-process, and safe
# to keep on permanently. They should never block a legitimate user
# in normal traffic.
# ---------------------------------------------------------------------------

# Kill-switch: when CMVIDEO_MINI_DISABLED is set to anything truthy,
# all submission endpoints return 503 immediately. /healthz and
# /api/limits stay alive so the operator can confirm the gate is up.
def _killswitch_active() -> bool:
    val = (os.environ.get("CMVIDEO_MINI_DISABLED") or "").strip().lower()
    return val in {"1", "true", "yes", "on"}


_KILL_DETAIL = (
    "Mini service is paused right now under heavy load. The desktop app "
    "at cmvideo.online has no caps and runs locally - grab it instead."
)


def _enforce_killswitch() -> None:
    if _killswitch_active():
        raise HTTPException(status_code=503, detail=_KILL_DETAIL,
                            headers={"Retry-After": "300"})


# User-Agent gate: drop submissions from clients that don't even bother
# to pretend to be a browser or a script. Real curl / wget / Python
# clients always send something; the empty-UA case is exclusively
# bottom-tier scraper / botnet traffic.
_BAD_UA_SUBSTRINGS = (
    "ahrefs", "semrush", "mj12bot", "petalbot", "dotbot", "bytespider",
    "amazonbot", "scrapy", "censys", "shodan",
)


def _enforce_ua(request: Request) -> None:
    ua = (request.headers.get("user-agent") or "").strip().lower()
    if not ua:
        raise HTTPException(status_code=400, detail="Set a User-Agent.")
    for bad in _BAD_UA_SUBSTRINGS:
        if bad in ua:
            raise HTTPException(status_code=403, detail="Crawlers aren't welcome here.")


# Sliding-window failure tracker. Every time a submission is rejected
# (4xx) or a job goes to `error` stage, we drop a timestamp in the
# IP's deque. If the IP racks up FAILURE_THRESHOLD failures inside
# FAILURE_WINDOW_S, every new submission is refused for
# FAILURE_COOLDOWN_S with a 429 + Retry-After.
import collections

_failures: dict[str, collections.deque] = {}
_failures_lock = threading.Lock()


def _record_failure(ip: str) -> None:
    if not ip:
        return
    now = time.monotonic()
    with _failures_lock:
        dq = _failures.setdefault(ip, collections.deque(maxlen=FAILURE_THRESHOLD * 4))
        dq.append(now)
        # Compact: drop entries older than the window so the deque
        # stays small even for chronic offenders.
        cutoff = now - FAILURE_WINDOW_S
        while dq and dq[0] < cutoff:
            dq.popleft()


def _check_cooldown(ip: str) -> None:
    if not ip:
        return
    now = time.monotonic()
    with _failures_lock:
        dq = _failures.get(ip)
        if not dq:
            return
        cutoff = now - FAILURE_WINDOW_S
        while dq and dq[0] < cutoff:
            dq.popleft()
        recent = len(dq)
    if recent >= FAILURE_THRESHOLD:
        # Cooldown lasts FAILURE_COOLDOWN_S past the most recent
        # failure. The Retry-After header tells well-behaved clients
        # exactly how long to wait.
        last = dq[-1] if recent else now
        retry_after = max(1, int(last + FAILURE_COOLDOWN_S - now))
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many failed submissions from your IP - cooling down for "
                f"{retry_after}s. The desktop app at cmvideo.online has no caps."
            ),
            headers={"Retry-After": str(retry_after)},
        )


# /api/info cache. Same-URL preview bursts (e.g. the widget gets
# embedded in a popular Reddit thread) shouldn't re-scrape yt-dlp
# every time. Tiny TTL because video metadata can shift (live
# streams etc.) and we don't want stale duration caps.
_INFO_CACHE_TTL_S = 5 * 60
_INFO_CACHE_MAX = 256
_info_cache: "collections.OrderedDict[str, tuple[float, dict]]" = collections.OrderedDict()
_info_cache_lock = threading.Lock()


def _info_cache_get(url: str) -> dict | None:
    now = time.monotonic()
    with _info_cache_lock:
        item = _info_cache.get(url)
        if item is None:
            return None
        ts, data = item
        if (now - ts) > _INFO_CACHE_TTL_S:
            _info_cache.pop(url, None)
            return None
        # Move to end (LRU): recently-accessed URLs survive evictions.
        _info_cache.move_to_end(url)
        return data


def _info_cache_put(url: str, data: dict) -> None:
    with _info_cache_lock:
        _info_cache[url] = (time.monotonic(), data)
        _info_cache.move_to_end(url)
        while len(_info_cache) > _INFO_CACHE_MAX:
            _info_cache.popitem(last=False)


@app.post("/api/info")
@limiter.limit("120/hour;10/minute")
async def api_info(request: Request, body: InfoRequest):
    _enforce_killswitch()
    _enforce_ua(request)
    url = _validate_url(body.url)
    cached = _info_cache_get(url)
    if cached is not None:
        return JSONResponse(cached)
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_do_info, url), timeout=25)
    except asyncio.TimeoutError:
        _record_failure(_client_ip(request))
        raise HTTPException(status_code=504, detail="Source took too long to respond.")
    except yt_dlp.utils.DownloadError as e:
        _record_failure(_client_ip(request))
        raise HTTPException(status_code=400, detail=_friendly_ydl_error(e))
    except HTTPException:
        raise
    except Exception:
        # Don't leak the raw exception string - it can include paths
        # or internal state. Server logs keep the full traceback.
        log.exception("info failed")
        _record_failure(_client_ip(request))
        raise HTTPException(status_code=400, detail="Couldn't read that URL.")
    _info_cache_put(url, info)
    return JSONResponse(info)


@app.post("/api/process")
@limiter.limit(f"{RATE_LIMIT_PER_HOUR};2/minute")
async def api_process(
    request: Request,
    format: str = Form(...),
    mode: str = Form(...),
    url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    fps: str = Form("source"),
    quality: str = Form(DEFAULT_QUALITY),
):
    # Hardening gates run BEFORE we touch any of the request body, so
    # an attacker can't make us allocate tmpdirs / stream uploads
    # while they're already on the cooldown list.
    _enforce_killswitch()
    _enforce_ua(request)
    _check_cooldown(_client_ip(request))

    fmt = (format or "").lower().strip()
    md = (mode or "").lower().strip()
    fps_choice = (fps or "source").lower().strip()
    quality_choice = (quality or DEFAULT_QUALITY).lower().strip()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail="Format must be 'mp4' or 'mp3'.")
    if md not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail="Mode must be 'download', 'silence', or 'beep'.")
    if fps_choice not in ALLOWED_FPS:
        raise HTTPException(status_code=400, detail="FPS must be 'source', '30', or '60'.")
    if quality_choice not in ALLOWED_QUALITY:
        raise HTTPException(status_code=400, detail="Quality must be 'standard' or 'hd'.")
    # FPS is video-only; ignore the field for MP3.
    if fmt == "mp3":
        fps_choice = "source"
        quality_choice = DEFAULT_QUALITY  # height is meaningless for audio
    # 1080p + fps override = libx264 ultrafast at 1080p on 2 shared
    # vCPUs, which runs at ~5x slower than realtime. An 8-min censor
    # job would hit the ffmpeg timeout. Refuse the combo with a clear
    # message instead of silently coercing - users who specifically
    # picked 60fps deserve to know why we're not honoring it.
    if quality_choice == "hd" and fps_choice in {"30", "60"}:
        raise HTTPException(
            status_code=400,
            detail=(
                "1080p + fps override is too slow on the mini's shared CPU. "
                "Pick 720p (Standard) for 30/60 fps, or 1080p with Source fps. "
                "The desktop app handles both at any combination."
            ),
        )

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

    # Async branch: when the client sends `?async=1`, we run the
    # pipeline in a background task and return a job id immediately
    # so the frontend can poll for progress. The synchronous path
    # below is preserved for backwards-compat with anyone still
    # POST'ing /api/process and waiting for the file in the response.
    want_async = (request.query_params.get("async") or "").strip() in {"1", "true", "yes"}
    if want_async:
        tmpdir = Path(tempfile.mkdtemp(prefix="cmvm_"))
        clean_url: Optional[str] = None
        saved_upload: Optional[Path] = None
        try:
            if have_url:
                clean_url = _validate_url(url)
            else:
                saved_upload = await _save_upload(file, tmpdir)
                if saved_upload.stat().st_size > MAX_CENSOR_FILESIZE_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds the {MAX_CENSOR_FILESIZE_BYTES // (1024 * 1024)} MB cap.",
                    )
            job = _create_job(_client_ip(request), fmt, tmpdir)
            asyncio.create_task(_run_pipeline_async(
                job,
                md=md, fmt=fmt, fps_choice=fps_choice,
                quality_choice=quality_choice,
                have_url=have_url, clean_url=clean_url,
                saved_upload=saved_upload,
            ))
            return JSONResponse({"job_id": job.job_id})
        except HTTPException:
            shutil.rmtree(tmpdir, ignore_errors=True)
            _record_failure(_client_ip(request))
            raise
        except Exception:
            shutil.rmtree(tmpdir, ignore_errors=True)
            log.exception("async dispatch failed")
            _record_failure(_client_ip(request))
            raise HTTPException(status_code=500, detail="Mini app hit an internal error. Try again or use the desktop app.")

    tmpdir = Path(tempfile.mkdtemp(prefix="cmvm_"))
    try:
        # 1) Get the source media into tmpdir.
        if have_url:
            clean_url = _validate_url(url)
            max_dur = MAX_DOWNLOAD_DURATION_SECONDS if md == "download" else MAX_CENSOR_DURATION_SECONDS
            quality_cap = QUALITY_DOWNLOAD_CAPS.get(quality_choice, MAX_DOWNLOAD_FILESIZE_BYTES)
            max_size = quality_cap if md == "download" else MAX_CENSOR_FILESIZE_BYTES
            target_height = QUALITY_HEIGHTS.get(quality_choice, MAX_VIDEO_HEIGHT)
            try:
                src = await asyncio.wait_for(
                    asyncio.to_thread(
                        _do_download, clean_url, fmt, tmpdir, max_dur, max_size, fps_choice,
                        height=target_height,
                    ),
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
        _record_failure(_client_ip(request))
        raise
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Server logs keep the real traceback; client gets a stable
        # message that doesn't leak paths or library internals.
        log.exception("process failed")
        _record_failure(_client_ip(request))
        raise HTTPException(status_code=500, detail="Mini app hit an internal error. Try again or use the desktop app.")


# ---- async job polling ---------------------------------------------------

# Human-friendly stage labels for the frontend to render. Kept here
# (next to the routes that surface them) so the wording is easy to
# tweak without going hunting for it.
_STAGE_LABELS = {
    "queued":       "Queued...",
    "fetching":     "Pulling source...",
    "transcribing": "Transcribing...",
    "rendering":    "Rendering...",
    "ready":        "Ready",
    "error":        "Failed",
}


@app.get("/api/jobs/{job_id}")
@limiter.limit("180/minute")          # 700ms polls = ~85/min, this leaves headroom but blocks runaway clients
async def api_job_state(request: Request, job_id: str):
    """Return the current state of a background job. The client polls
    this every ~700 ms while a job is in-flight so it can render a
    progress bar.

    Access control: the job_id is a 32-byte unpredictable token AND
    we require the request's client IP to match the creator's. This
    is a polling endpoint so the rate limit is loose by design (the
    UI polls about 85x/min); the limiter is just there to stop a
    runaway / hostile client from hammering us at HTTP-flood rates.
    """
    j = _get_job(job_id, client_ip=_client_ip(request))
    return {
        "job_id": j.job_id,
        "stage": j.stage,
        "stage_label": _STAGE_LABELS.get(j.stage, j.stage),
        "pct": int(j.pct),
        "ready": bool(j.ready),
        "error": j.error,
        "filename": j.filename,
        # Live telemetry: bytes done / total, current speed in bytes/s,
        # and ETA seconds. All optional - a frontend can ignore them
        # and still get a useful percentage.
        "bytes_done": int(j.bytes_done),
        "bytes_total": int(j.bytes_total),
        "speed_bps": float(j.speed_bps),
        "eta_s": j.eta_s,
    }


@app.get("/api/jobs/{job_id}/file")
@limiter.limit("30/minute")
async def api_job_file(request: Request, job_id: str):
    """Stream the finished file. Cleaning up happens in a background
    task so the client doesn't sit on a closed socket while shutil
    walks tmpdir.

    Same IP-scope check as the state endpoint so a leaked job_id
    can't be used to siphon someone else's file.
    """
    j = _get_job(job_id, client_ip=_client_ip(request))
    if j.error:
        raise HTTPException(status_code=400, detail=j.error)
    if not j.ready or j._out_path is None or not j._out_path.exists():
        raise HTTPException(status_code=409, detail="Job is not ready yet.")
    out = j._out_path
    tmpdir = j._tmpdir
    media_type = j._media_type
    filename = j.filename or _safe_name("cmvideo-mini", j._format)

    def _cleanup() -> None:
        with _jobs_lock:
            _jobs.pop(job_id, None)
        if tmpdir and tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)

    return FileResponse(
        path=str(out),
        media_type=media_type,
        filename=filename,
        background=BackgroundTask(_cleanup),
    )


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


def _llm_status_safe():
    """Return llm_extract.llm_status() if the optional Tier-8 module
    is importable, else a minimal placeholder. The mini ships fine
    without llm_extract.py if the operator chose not to wire up an
    LLM provider."""
    try:
        import llm_extract  # type: ignore[import-not-found]
    except ImportError:
        return {"enabled": False, "reason": "module not installed"}
    try:
        return llm_extract.llm_status()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": False, "error": str(exc)[:200]}


@app.get("/api/limits", include_in_schema=False)
async def api_limits():
    with _jobs_lock:
        live_jobs = sum(1 for j in _jobs.values() if not j.ready and j.error is None)
    with _failures_lock:
        cooled_ips = sum(1 for dq in _failures.values() if len(dq) >= FAILURE_THRESHOLD)
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
        "qualities": sorted(ALLOWED_QUALITY),
        "default_quality": DEFAULT_QUALITY,
        "quality_heights": dict(QUALITY_HEIGHTS),
        "quality_download_caps_mb": {k: v // (1024 * 1024) for k, v in QUALITY_DOWNLOAD_CAPS.items()},
        "yt_cookies_loaded": bool(YT_COOKIES_FILE),
        "hardening": {
            "killswitch_active": _killswitch_active(),
            "job_max_inflight": JOB_MAX_INFLIGHT,
            "job_max_per_ip": JOB_MAX_PER_IP,
            "live_jobs_now": live_jobs,
            "failure_threshold": FAILURE_THRESHOLD,
            "failure_window_s": FAILURE_WINDOW_S,
            "failure_cooldown_s": FAILURE_COOLDOWN_S,
            "ips_in_cooldown_now": cooled_ips,
            "info_cache_size": len(_info_cache),
            "info_cache_ttl_s": _INFO_CACHE_TTL_S,
        },
        "yt_censor_enabled": True,
        "full_app_url": "https://cmvideo.online",
        "extractors": _extractors.available_tools(),
        "extractor_versions": _extractors.tool_versions(),
        "memory": _extractors.memory_status(),
        "llm": _llm_status_safe(),
    }
