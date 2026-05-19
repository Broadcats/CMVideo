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
import json
import logging
import os
import re
import secrets
import shutil
import socket
import tempfile
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
import yt_dlp
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.background import BackgroundTask

import mini_censor
import extractors as _extractors  # multi-tool fallback chain (yt-dlp -> gallery-dl -> Cobalt -> streamlink)
import proxy_router as _proxy_router  # per-domain residential-proxy routing (Meta / TT / X / tube sites)
from version import MINI_VERSION  # CalVer for the mini-app, separate from desktop's APP_VERSION


# ---------------------------------------------------------------------------
# Caps. The "mini" pitch only holds if these stay tight.
# ---------------------------------------------------------------------------
MAX_DOWNLOAD_DURATION_SECONDS = 60 * 60          # URL download only (1 hour)
MAX_CENSOR_DURATION_SECONDS = 8 * 60             # transcription is slow on free CPU
MAX_CENSOR_FILESIZE_BYTES = 100 * 1024 * 1024    # cap upload + output
MAX_UPLOAD_BYTES = 100 * 1024 * 1024             # multipart body size
AUDIO_BITRATE_KBPS = "320"  # highest standard MP3 bitrate (LAME -V0 / CBR)

# Quality tiers. "720p" is the default because the speed wins
# from 0.4.11 (single-file progressive MP4, no merge step) only apply
# at 720p - bumping the default to 1080p makes first-impression
# pulls slower for everyone. Higher tiers are opt-in.
#
# Naming: height-suffixed primary keys (the y2down convention,
# adopted in mini-2026.05.18.0-alpha). Legacy "standard" / "hd"
# values from the 0.4.13.0-alpha API are kept as aliases so old
# integrations don't break.
QUALITY_HEIGHTS = {
    "144p":   144,
    "240p":   240,
    "360p":   360,
    "480p":   480,
    "720p":   720,
    "1080p":  1080,
    "1440p":  1440,
    "2160p":  2160,
}
QUALITY_ALIASES = {"standard": "720p", "hd": "1080p"}
ALLOWED_QUALITY = set(QUALITY_HEIGHTS.keys()) | set(QUALITY_ALIASES.keys())
DEFAULT_QUALITY = "720p"
# Per-tier filesize caps. Lower tiers get tighter budgets - if the
# source is shipping a 4 GB master we don't need the full blob to
# serve a 360p output. Higher tiers get more headroom because AVC
# bitrate scales roughly with pixel count: 1080p ~2x 720p, 1440p
# ~2x 1080p, 4K ~2x 1440p.
QUALITY_DOWNLOAD_CAPS = {
    "144p":   200 * 1024 * 1024,
    "240p":   300 * 1024 * 1024,
    "360p":   400 * 1024 * 1024,
    "480p":   600 * 1024 * 1024,
    "720p":   800 * 1024 * 1024,
    "1080p": 1500 * 1024 * 1024,
    "1440p": 2500 * 1024 * 1024,
    "2160p": 4000 * 1024 * 1024,
}
# Back-compat: anywhere downstream still references the legacy
# constants we keep them pointing at the standard tier.
MAX_VIDEO_HEIGHT = QUALITY_HEIGHTS[DEFAULT_QUALITY]
MAX_DOWNLOAD_FILESIZE_BYTES = QUALITY_DOWNLOAD_CAPS[DEFAULT_QUALITY]


def _normalize_quality(quality: str) -> str:
    """Map a user-supplied quality string to a canonical
    height-named tier. Accepts both the new ('720p', '1080p', ...)
    and legacy ('standard', 'hd') values; returns one of
    QUALITY_HEIGHTS' keys or raises HTTPException(400)."""
    q = (quality or DEFAULT_QUALITY).lower().strip()
    if q in QUALITY_HEIGHTS:
        return q
    if q in QUALITY_ALIASES:
        return QUALITY_ALIASES[q]
    raise HTTPException(
        status_code=400,
        detail=f"Quality must be one of: {', '.join(sorted(QUALITY_HEIGHTS))}",
    )

# 1-hour 720p AVC files land around 600-800 MB. From the HF Space's
# datacenter peering that pulls in roughly 60-180s depending on
# origin throttling, so we budget 6 minutes - tight enough that a
# stuck connection still gives up promptly, generous enough to
# actually finish the new 1-hour cap on a slow source.
DOWNLOAD_TIMEOUT_SECONDS = 360
CENSOR_TIMEOUT_SECONDS = 240

# Per-IP submit cap on /api/process and /api/download. The
# `;2/minute` burst guard is layered on at the route decorators
# so the per-hour budget can't be spent in a single 30-second
# window.
#
# History: started at 5/hour, which turned out to be too tight
# for power users (a single full-length playlist would burn the
# budget). Bumped to 20/hour with v0.4.16.4-alpha when the
# owner-IP allowlist was retired - the public default now has
# to actually be usable, not just defensive.
#
# Override with `CMVIDEO_RATE_LIMIT_PER_HOUR=NN/hour` env var
# (e.g. `30/hour`). The format is whatever slowapi's
# Limiter.limit() accepts; usually `<int>/<unit>` where unit is
# hour|minute|second|day. Validated implicitly at decorator
# parse time - a malformed value will raise on app boot.
RATE_LIMIT_PER_HOUR = os.environ.get("CMVIDEO_RATE_LIMIT_PER_HOUR", "20/hour")

# Audio formats. yt-dlp's FFmpegExtractAudio supports all of these
# via its `preferredcodec` arg. Lossless formats (wav, flac) skip
# the bitrate parameter entirely.
ALLOWED_VIDEO_FORMATS = {"mp4", "webm", "mov", "avi", "mkv"}
ALLOWED_AUDIO_FORMATS = {"mp3", "m4a", "aac", "ogg", "opus", "wav", "flac"}
DEFAULT_AUDIO_FORMAT = "mp3"
LOSSLESS_AUDIO_FORMATS = {"wav", "flac"}
# yt-dlp's `preferredcodec` value for each format. Most align with
# the format name; "ogg" maps to "vorbis" because that's the codec
# inside an Ogg container yt-dlp can produce by default.
AUDIO_CODEC_MAP = {
    "mp3":  "mp3",
    "m4a":  "m4a",
    "aac":  "aac",
    "ogg":  "vorbis",
    "opus": "opus",
    "wav":  "wav",
    "flac": "flac",
}
# Per-format filesize caps. WAV is the worst case at ~600 MB/hr
# for 16-bit/44.1kHz/stereo; FLAC about half that. Lossy formats
# at 320 kbps land around 150 MB/hr regardless of source quality,
# so 200 MB is plenty.
AUDIO_DOWNLOAD_CAPS = {
    "mp3":  200 * 1024 * 1024,
    "m4a":  200 * 1024 * 1024,
    "aac":  200 * 1024 * 1024,
    "ogg":  200 * 1024 * 1024,
    "opus": 200 * 1024 * 1024,
    "wav":  800 * 1024 * 1024,
    "flac": 600 * 1024 * 1024,
}

# Top-level format slot. ALLOWED_VIDEO_FORMATS => video path;
# ALLOWED_AUDIO_FORMATS => audio path with FFmpegExtractAudio.
ALLOWED_FORMATS = ALLOWED_VIDEO_FORMATS | ALLOWED_AUDIO_FORMATS
ALLOWED_MODES = {"download", "silence", "beep"}
ALLOWED_FPS = {"source", "30", "60"}

WORDLISTS_DIR = Path(os.environ.get("CMVIDEO_WORDLISTS_DIR", "wordlists")).resolve()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cmvideo-mini")


# Matches the auth segment of a URL-shaped substring:
#   <scheme>://<user>:<pass>@<host>...
# Used to scrub residential-proxy credentials out of log lines
# before they reach HF Space logs. yt-dlp / requests sometimes
# echo the proxy URL back in error messages ("HTTP 407 from
# http://user:pass@host:port"), and our `CMVIDEO_RESIDENTIAL_PROXY`
# env var IS the user:pass@host:port form, so anything we log
# that came from those libraries needs sanitising.
_PROXY_CRED_RE = re.compile(r'(://)[^:@/\s]+:[^@/\s]+@')


def _safe_log_msg(exc: BaseException) -> str:
    """Single-line, credential-safe, length-capped representation
    of `exc` for INFO/WARNING logging.

    Why this exists:
      * `str(exc).splitlines()[-1]` is the obvious one-liner but
        IndexErrors when the stringified exception is empty -
        which DOES happen for some socket-level errors. An
        IndexError raised inside an `except` block escapes
        whatever clean error response we were about to return,
        turning a 422 into a 500.
      * yt-dlp / requests sometimes embed the upstream URL
        (including the residential-proxy URL with creds) in
        their error messages. We don't want creds in HF logs.
      * Long messages with embedded HTML / tracebacks blow up
        log volume; cap at 200 chars.

    Returns the type name as a fallback if the message is empty,
    so log lines always carry SOMETHING actionable."""
    raw = str(exc) if str(exc) else type(exc).__name__
    redacted = _PROXY_CRED_RE.sub(r'\1***:***@', raw)
    lines = [ln for ln in redacted.splitlines() if ln.strip()]
    last = lines[-1] if lines else type(exc).__name__
    return last[:200]


# ---------------------------------------------------------------------------
# App boilerplate
# ---------------------------------------------------------------------------
# Trusted-proxy depth. `X-Forwarded-For` is the running history of
# every reverse proxy a request has passed through, with each
# proxy *appending* its immediate peer's IP to the right end. So
# for a request that took the path:
#     client -> Cloudflare -> HF Spaces -> our backend
# the backend sees `X-Forwarded-For: client_ip, cloudflare_ip`
# (Cloudflare appended the real client; HF appended Cloudflare).
# The TCP peer at the backend is HF's edge.
#
# `_TRUSTED_PROXY_HOPS` = how many trusted proxies sit in front
# of the FastAPI process. The real client IP is the
# `_TRUSTED_PROXY_HOPS`-th entry from the RIGHT of the chain
# (1-indexed). For HF Spaces alone (default 1) that's the
# rightmost entry. For Cloudflare + HF (=2) it's the second-from-
# right. Taking the leftmost is wrong on the public internet:
# anyone can send `X-Forwarded-For: <anything>` and that string
# becomes chain[0]. Combined with the owner-IP allowlist (which
# grants unlimited jobs to listed IPs), trusting chain[0] turns
# a header into an unauthenticated "skip all rate limits" key.
_TRUSTED_PROXY_HOPS = max(1, int(os.environ.get("CMVIDEO_TRUSTED_PROXY_HOPS", "1") or "1"))


def _client_ip(request: Request) -> str:
    """Resolve the real client IP behind HF Spaces' reverse proxy
    in a way that resists header forgery THROUGH the public edge.

    `slowapi.util.get_remote_address` reads `request.client.host`,
    which on HF Spaces is always the platform's edge proxy - so
    every visitor on Earth shared the same rate-limit bucket. We
    parse `X-Forwarded-For` and pick the
    `_TRUSTED_PROXY_HOPS`-th-from-rightmost entry (the immediate
    peer of our outermost trusted proxy). Anything an attacker
    pre-pends on the left is ignored.

    Worked example with `_TRUSTED_PROXY_HOPS=1` (HF only):
    * Legitimate: chain `[client_ip]` (HF appended) -> chain[-1]
      = `client_ip`. Right answer.
    * Forged: attacker sends `X-Forwarded-For: <victim>`.
      HF appends attacker_ip. Chain `[<victim>, attacker_ip]`
      -> chain[-1] = `attacker_ip` (the real client). Forgery
      ignored.
    * Forged with padding: `[a, b, c, attacker_ip]` -> chain[-1]
      = `attacker_ip`. Still right.

    LIMITATION: this trusts that the chain is `_TRUSTED_PROXY_HOPS`
    entries long because the trusted edge ALWAYS appends its peer.
    On HF Spaces' Caddy edge that's true. If you ever expose this
    process directly to the public internet (no proxy in front),
    a single-entry forged XFF will still pass through - the
    correct fix in that scenario is socket-peer-CIDR trust, which
    requires knowing the platform's internal peer IPs. Ship that
    only after measuring `request.client.host` on the actual
    target deploy. On HF Spaces this gap is unreachable: the
    FastAPI container is bound on the internal Docker network and
    not routable from the public internet.

    If `len(chain) < _TRUSTED_PROXY_HOPS` (chain shorter than
    expected, which on HF means missing/empty XFF), we fall
    through to the socket peer rather than `chain[0]`. That
    matters for synthetic / direct test traffic and doesn't
    affect production because HF's chain is always >= 1 entry."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        chain = [c.strip() for c in xff.split(",")]
        chain = [c for c in chain if c]
        # Take the Nth-from-rightmost entry, but ONLY if the chain
        # is at least that long. If `len(chain) < _TRUSTED_PROXY_HOPS`
        # the request didn't actually pass through the expected
        # number of trusted proxies, which means EITHER the
        # platform misconfigured XFF, OR an attacker is hitting
        # the FastAPI process directly with no edge in front and
        # forging the header. In both cases trusting any chain
        # entry hands the attacker a free spoof. Fall through to
        # the socket peer instead - it's the actual TCP source
        # and can't be forged over HTTP.
        if chain and len(chain) >= _TRUSTED_PROXY_HOPS:
            candidate = chain[len(chain) - _TRUSTED_PROXY_HOPS]
            # Tolerate IPv6 with brackets / zone IDs by feeding
            # ipaddress only the bit it'll accept; if parsing fails
            # we still return the raw string so the limiter buckets
            # on something.
            if candidate:
                try:
                    ipaddress.ip_address(candidate.split("%", 1)[0])
                except ValueError:
                    pass
                return candidate
    return get_remote_address(request) or "0.0.0.0"


# ---------------------------------------------------------------------------
# Owner-IP allowlist
# ---------------------------------------------------------------------------
# IPs that should bypass abuse-shaped gates: hourly + burst rate limits,
# the per-IP job concurrency cap, and the per-IP failure cooldown.
#
# Owner IPs DO still respect:
#   * JOB_MAX_INFLIGHT (global) - protects the box from melting even
#     under maintainer testing.
#   * The killswitch          - so a panic shutdown still applies.
#   * The User-Agent gate     - no reason to ship empty UAs.
#   * Duration / size caps    - same: protects the box, and protects
#     the maintainer from accidentally queueing a 10 GB pull.
#
# Configure via the CMVIDEO_OWNER_IPS env var (CSV of IPs and/or CIDR
# ranges, mixed v4 + v6 fine). Empty / unset = nobody is privileged.
#
# Example:
#   CMVIDEO_OWNER_IPS="203.0.113.42, 2001:db8::/64, 198.51.100.0/24"
_OWNER_IPS_RAW = os.environ.get("CMVIDEO_OWNER_IPS", "").strip()
_OWNER_NETWORKS: list[ipaddress._BaseNetwork] = []
for _entry in _OWNER_IPS_RAW.split(","):
    _entry = _entry.strip()
    if not _entry:
        continue
    try:
        # `strict=False` lets you write `203.0.113.42` as well as
        # `203.0.113.42/32` - both come out as a /32 network.
        _OWNER_NETWORKS.append(ipaddress.ip_network(_entry, strict=False))
    except ValueError:
        log.warning("Ignoring invalid CMVIDEO_OWNER_IPS entry: %r", _entry)
if _OWNER_NETWORKS:
    log.info("Owner-IP allowlist active: %d entries", len(_OWNER_NETWORKS))


def _is_owner_ip(ip_str: str) -> bool:
    """True if `ip_str` matches one of the configured owner networks."""
    if not _OWNER_NETWORKS or not ip_str:
        return False
    try:
        ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
    except ValueError:
        return False
    return any(ip in net for net in _OWNER_NETWORKS)


def _rate_limit_key(request: Request) -> str:
    """slowapi key_func that returns a per-request unique key for
    owner IPs (so they never share a bucket with anyone, including
    themselves) and the normal client IP for everyone else.

    The trick: slowapi rate-limits per key. By handing each owner
    request a fresh random key, we ensure the lookup always finds an
    empty bucket - effectively a no-op rate limit. The cost is one
    `secrets.token_hex(8)` call per request (~microseconds)."""
    ip = _client_ip(request)
    if _is_owner_ip(ip):
        return f"owner-bypass-{secrets.token_hex(8)}"
    return ip


limiter = Limiter(key_func=_rate_limit_key, default_limits=[])

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
# Space cross-origin. The Space's own / endpoint is fallback for
# direct visitors and is same-origin from there.
#
# Production allowlist is just the two cmvideo.online hostnames.
# Localhost dev origins are gated behind CMVIDEO_DEV_CORS=1 so the
# production HF Space doesn't volunteer them - they're not a real
# vector (no creds in the request, browser still enforces SOP) but
# it's neat to keep the live origin list tight, and one less footgun
# for anyone running a malicious local server that wanted to read
# cross-origin responses they could already trigger themselves.
_CORS_ORIGINS = [
    "https://cmvideo.online",
    "https://www.cmvideo.online",
]
if os.environ.get("CMVIDEO_DEV_CORS", "").strip() == "1":
    _CORS_ORIGINS += [
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ]
    log.info("CORS dev mode: localhost origins are allowed")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
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


# ---------------------------------------------------------------------------
# Embed URL canonicalization
# ---------------------------------------------------------------------------
# Some sites publish two URL shapes for the same video:
#   - Canonical "watch" page: yt-dlp's primary extractor matches.
#   - Embed / iframe player URL: a separate extractor that often
#     lags behind on parser fixes (or doesn't exist at all).
#
# When the user pastes the embed URL we silently rewrite to the
# canonical form before extraction. This is purely a heuristic
# - if pattern doesn't match we leave the URL alone and let
# yt-dlp do its thing.
#
# Stress test (May 2026) showed `youtube.com/embed/<id>` 422'ing
# from datacenter while `youtube.com/watch?v=<id>` succeeded with
# the same auth state, and `thisvid.com/embed/<id>/` 422'ing while
# `thisvid.com/videos/<slug>/` succeeded. Same trick fixes both.

# Each entry: (compiled regex, replacement_template_for_id_only).
# We rebuild the URL from scratch so `?start=10` style query
# strings on the embed get merged correctly with `?v=<id>`
# instead of producing `watch?v=<id>?start=10`. Capture group 1
# is always the video ID.
_EMBED_REWRITES: tuple[tuple[re.Pattern, str], ...] = (
    # YouTube /embed/<id>?<extra> -> /watch?v=<id>&<extra>
    (
        re.compile(
            r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/embed/([A-Za-z0-9_-]{11})(\?.*)?$",
            re.I,
        ),
        "youtube_watch",
    ),
    # youtu.be/<id>?<extra> -> youtube.com/watch?v=<id>&<extra>
    (
        re.compile(
            r"^(?:https?://)?youtu\.be/([A-Za-z0-9_-]{11})(\?.*)?$",
            re.I,
        ),
        "youtube_watch",
    ),
    # YouTube /shorts/<id> -> /watch?v=<id>. Shorts work in
    # yt-dlp but via a separate extractor path; canonical /watch
    # is the more battle-tested code path.
    (
        re.compile(
            r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/shorts/([A-Za-z0-9_-]{11})(\?.*)?$",
            re.I,
        ),
        "youtube_watch",
    ),
    # YouTube /live/<id> -> /watch?v=<id>. These are permanent video
    # URLs that look like live channels but are really archived streams.
    (
        re.compile(
            r"^(?:https?://)?(?:www\.|m\.)?youtube\.com/live/([A-Za-z0-9_-]{11})(\?.*)?$",
            re.I,
        ),
        "youtube_watch",
    ),
    # Vimeo player.vimeo.com/video/<id> -> vimeo.com/<id>
    (
        re.compile(
            r"^(?:https?://)?player\.vimeo\.com/video/(\d+)(\?.*)?$",
            re.I,
        ),
        "vimeo_watch",
    ),
)


def _canonicalize_embed_url(url: str) -> str:
    """If `url` matches a known embed-shape pattern, rewrite it to
    the canonical "watch page" form. Returns `url` unchanged if no
    rewrite applies. Idempotent."""
    if not url:
        return url
    s = url.strip()
    for pattern, kind in _EMBED_REWRITES:
        m = pattern.match(s)
        if not m:
            continue
        vid = m.group(1)
        extra = m.group(2) or ""
        # Merge `?start=10` style extras as `&start=10` since we're
        # already adding our own `?v=` (or vimeo path).
        if extra.startswith("?"):
            extra = "&" + extra[1:]
        if kind == "youtube_watch":
            new = f"https://www.youtube.com/watch?v={vid}{extra}"
        elif kind == "vimeo_watch":
            new = f"https://vimeo.com/{vid}{extra.replace('&', '?', 1) if extra else ''}"
        else:  # pragma: no cover
            continue
        log.info("canonicalize: %s -> %s", s[:80], new[:80])
        return new
    return s


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
    # Embed -> canonical rewrite happens BEFORE host validation so
    # the validation runs against the rewritten host (which is
    # what yt-dlp will actually fetch). For e.g. player.vimeo.com
    # -> vimeo.com that's a different hostname; we want SSRF
    # checks to apply to the destination.
    raw = _canonicalize_embed_url(raw)
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


# Loopback exemptions for our own internal services. The SSRF guard
# blocks ALL private/loopback IPs by default, but the bgutil PoToken
# sidecar runs on 127.0.0.1:CMVIDEO_POTOKEN_PORT and the bgutil
# yt-dlp plugin needs to reach it - blocking that breaks YouTube
# extraction in a way that's invisible (the plugin fails open with
# "no provider available", yt-dlp falls back to no-PoToken, YT bot-
# walls us). We exempt that ONE specific (host, port) pair.
#
# The exemption is intentionally narrow: only loopback host + the
# bgutil port. An attacker controlling a request URL still cannot
# reach `127.0.0.1:22` or `127.0.0.1:80` etc.
def _bgutil_port() -> int:
    try:
        return int(os.environ.get("CMVIDEO_POTOKEN_PORT", "4416"))
    except (TypeError, ValueError):
        return 4416


def _is_loopback_exempt(ip: "ipaddress._BaseAddress", port: int) -> bool:
    """True if (ip, port) is one of our own internal services that
    must be reachable despite the SSRF guard."""
    if not ip.is_loopback:
        return False
    return port == _bgutil_port()


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    infos = _REAL_GETADDRINFO(host, port, *args, **kwargs)
    # If `host` itself is literally one of our blocked metadata names,
    # bail before returning results. (Belt-and-suspenders; the URL
    # validator already catches these by name.)
    if isinstance(host, str) and host.lower() in _BLOCKED_HOSTS_LITERAL:
        raise _SSRFBlocked(f"blocked host: {host}")
    # `port` arg can be int, str, or None depending on caller. Coerce
    # to int for the exemption check; non-numeric ports never match
    # our bgutil port so the check correctly returns False.
    try:
        port_int = int(port) if port is not None else 0
    except (TypeError, ValueError):
        port_int = 0
    for family, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
        except ValueError:
            continue
        if _is_loopback_exempt(ip, port_int):
            # Internal service that we explicitly allow ourselves to
            # reach. Don't log every plugin call - that'd spam the
            # logs; just let it through.
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


def _log_potoken_diagnostics() -> None:
    """Run once at startup. Tells us at a glance whether the PoToken
    plugin is engaged. The deployed mini's been getting "Sign in to
    confirm you're not a bot" YouTube errors despite the bgutil Node
    sidecar starting cleanly per the entrypoint logs - this prints
    enough to tell us which layer is silently broken on next deploy.

    Logs:
      * yt-dlp version (some PoToken plugin versions require recent yt-dlp)
      * Whether the `bgutil_ytdlp_pot_provider` package imports cleanly
      * Whether the local bgutil sidecar responds on its ping endpoint
      * The list of PoTokenProvider classes yt-dlp has actually
        registered. Empty list -> plugin discovery is broken.
    """
    try:
        import yt_dlp as _ytdlp
        # yt-dlp moved __version__ between modules across releases; check
        # version.py if the module attribute is absent.
        ver = getattr(_ytdlp, "__version__", None)
        if not ver:
            try:
                from yt_dlp.version import __version__ as ver
            except Exception:
                ver = "?"
    except Exception as exc:  # noqa: BLE001
        log.warning("[potoken-diag] yt-dlp import failed: %s", exc)
        return
    log.info("[potoken-diag] yt-dlp version: %s", ver)

    # NOTE: Don't manually __import__ the plugin modules here. Each
    # bgutil plugin module decorates its provider class with
    # @register_provider, which calls
    # yt_dlp.extractor.youtube.pot._provider.register_provider_generic().
    # That helper has an internal assertion that the same provider
    # name isn't registered twice. If we import the modules now AND
    # then yt-dlp's auto-discovery imports them again later (via
    # YoutubeDL() init), the second import raises AssertionError
    # mid-decoration and yt-dlp throws away the concrete provider
    # classes (BgUtilHTTP, BgUtilScriptNode) - leaving only the
    # PoTokenProvider base class registered, which is useless. Diagnosed
    # this by seeing the registered-subclasses list contain only
    # `BgUtilPTPBase`. Let yt-dlp own discovery; we just confirm the
    # `yt_dlp_plugins` package is on sys.path.
    try:
        import importlib.util as _imputil
        spec = _imputil.find_spec("yt_dlp_plugins")
        if spec is not None:
            log.info("[potoken-diag] yt_dlp_plugins package found at %s", spec.origin or spec.submodule_search_locations)
        else:
            log.warning("[potoken-diag] yt_dlp_plugins package NOT FOUND on sys.path")
    except Exception as exc:  # noqa: BLE001
        log.warning("[potoken-diag] package locate FAILED: %s", exc)

    try:
        import requests as _r
        port = os.environ.get("CMVIDEO_POTOKEN_PORT", "4416")
        r = _r.get(f"http://127.0.0.1:{port}/ping", timeout=2)
        log.info("[potoken-diag] bgutil sidecar ping -> %s, body=%s",
                 r.status_code, r.text[:120])
    except Exception as exc:  # noqa: BLE001
        log.warning("[potoken-diag] bgutil sidecar ping FAILED: %s: %s",
                    type(exc).__name__, str(exc)[:200])

    # Force yt-dlp to run its plugin discovery now (it normally happens
    # lazily on first extractor use). Then list registered providers.
    try:
        from yt_dlp.plugins import directories as _plug_dirs  # noqa: F401
    except Exception:
        pass
    try:
        # Triggering YoutubeDL() init runs plugin loading. Cheap; we
        # destroy it immediately.
        import yt_dlp as _ytdlp2
        with _ytdlp2.YoutubeDL({"quiet": True, "no_warnings": True}) as _y:
            pass
    except Exception as exc:  # noqa: BLE001
        log.info("[potoken-diag] YoutubeDL probe init: %s", exc)

    try:
        from yt_dlp.extractor.youtube.pot.provider import PoTokenProvider
        subs = PoTokenProvider.__subclasses__()
        log.info("[potoken-diag] Registered PoTokenProvider subclasses: %s",
                 [c.__module__ + "." + c.__name__ for c in subs] or "<none>")
    except Exception as exc:  # noqa: BLE001
        log.info("[potoken-diag] PoTokenProvider listing unsupported on this yt-dlp build: %s",
                 type(exc).__name__)


_log_potoken_diagnostics()


def _friendly_ydl_error(e: Exception, url: str | None = None) -> str:
    """yt-dlp errors are long and Pythonic. Distill them into the
    one line that a visitor on cmvideo.online can act on.

    `url` is optional but strongly recommended: the YouTube-specific
    "rate-limited" message is only emitted if the URL's hostname is
    on a YT-family domain. Generic-looking errors like "HTTP error
    429" or TLS EOFs come up for plenty of non-YouTube sites
    (Rumble, Bloomberg, Cloudflare-fronted tubes) and we were
    misclassifying those as YT failures, which confused users."""
    raw = str(e or "").strip()
    # `splitlines()[-1]` blows up on empty strings - guard so the
    # error pipeline can't crash itself trying to format another
    # error.
    lines = raw.splitlines() if raw else []
    msg = lines[-1] if lines else ""
    low = msg.lower()
    # YT-specific signals that ONLY map to "YT is bot-walling us":
    # "sign in to confirm" and the "not a bot" variants. These
    # phrases don't appear on other sites' error paths, so we can
    # match them URL-agnostic.
    yt_specific = any(needle in low for needle in (
        "sign in to confirm",
        "not a bot",
        "confirm you're not a bot",
        "failed to extract any player response",
        # yt-dlp's bare `Please sign in` from the YT player API when
        # all configured player_clients return playabilityStatus =
        # LOGIN_REQUIRED. Same root cause as "sign in to confirm" -
        # cookies are the fix, not waiting it out.
        "please sign in",
        "login_required",
    ))
    # Generic transport-layer signals that COULD mean YT bot wall
    # but only when we're actually talking to YT. For non-YT URLs
    # they usually mean something else (Rumble's own rate limiter,
    # Cloudflare 1015, an upstream TLS hiccup) and the user
    # deserves the actual error string, not a misleading
    # YT-flavoured message.
    yt_generic_signals = any(needle in low for needle in (
        "unexpected_eof_while_reading",
        "eof occurred in violation",
        "unable to download api page",
        "http error 403",
        "http error 429",
    ))
    if yt_specific:
        return (
            "YouTube is blocking this server’s IP. "
            "Fix: open mini.cmvideo.online, expand ‘YouTube not working?’ "
            "and paste your browser cookies — bypasses the block for 30 min. "
            "Or use the desktop app (runs on your own connection)."
        )
    if yt_generic_signals and _is_youtube_host(url or ""):
        return (
            "YouTube download failed — the server’s CDN connection was cut. "
            "Try MP4 format instead of WebM (VP9 DASH streams are more "
            "fragile from datacenter IPs). If it keeps failing, use "
            "mini.cmvideo.online for cookie auth or the desktop app."
        )
    # When every extractor in the fallback chain fails, the raw dump
    # ("All extractors failed. Last error: ... (tried: yt-dlp: ..., llm: ...)")
    # leaks through. Catch it and show a clean message instead.
    if "all extractors failed" in low or ("tried:" in low and "last error:" in low):
        if _is_youtube_host(url or ""):
            return (
                "YouTube couldn’t be downloaded from this server. "
                "Try MP4 format, or visit mini.cmvideo.online to upload "
                "your browser cookies (bypasses the block for 30 min). "
                "Desktop app works on your own connection."
            )
        return "Download failed — all extraction methods exhausted. Try the desktop app."
    if "unavailable" in low or "private video" in low or "video unavailable" in low:
        return "That video isn't available (private, region-locked, or removed)."
    # Eporner: known yt-dlp extractor regression — "unable to extract hash"
    # (upstream issue #16277). Let the Playwright tier handle it instead of
    # showing a confusing parser-error message.
    if "eporner" in (url or "").lower() and "unable to extract" in low and "hash" in low:
        return "Eporner's yt-dlp extractor has a known issue — trying the browser fallback instead."
    # Mixcloud: yt-dlp recognises the URL but fails at the CDN/auth
    # stage with messages that contain "unsupported" or similar generic
    # text. The generic "unsupported url" catch below hides the real
    # error and tells the user "that site isn't supported" \u2014 wrong.
    if url and "mixcloud.com" in url.lower():
        safe_msg = _proxy_router.redact_secrets(msg) if msg else ""
        return safe_msg or "Mixcloud download failed. The track may require a Mixcloud account or the CDN link expired."
    if "unsupported url" in low:
        return "That site isn't supported. The full desktop app uses the same yt-dlp under the hood, so it won't help either \u2014 try a direct video URL."
    # Parser regressions: yt-dlp's extractor matched the URL but
    # the site's HTML/JSON shape changed and the parser blew up.
    # These are normally fixed within a day or two of being
    # reported upstream; the right move for the user is to wait
    # OR file an issue. We surface a concrete "report this URL"
    # link so motivated users have a clear next step instead of
    # a stack trace.
    parser_regression_hints = (
        "no video formats",
        "no formats found",
        "unable to extract",
        "unable to parse",
        "keyerror",
        "list index out of range",
        "list indices must be integers",
        "expected string or bytes-like",
        "extractor crash",
        "json parse error",
        "cannot parse data",
        "report this issue",
        "please report",
    )
    if any(h in low for h in parser_regression_hints):
        return (
            "This site\u2019s extractor is currently broken upstream "
            "(yt-dlp parser bug). It usually gets patched within a day or two. "
            "If it\u2019s urgent, file the URL at https://github.com/yt-dlp/yt-dlp/issues "
            "or try the desktop app, which auto-updates yt-dlp on launch."
        )
    return msg or "Couldn't fetch that URL."


def _safe_name(stem: str, fmt: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)[:80] or "cmvideo-mini"
    return f"{cleaned}.{fmt}"


_AUDIO_MEDIA_TYPES: dict[str, str] = {
    "mp3":  "audio/mpeg",
    "m4a":  "audio/mp4",
    "aac":  "audio/aac",
    "ogg":  "audio/ogg",
    "opus": "audio/ogg",   # ffmpeg ships opus inside an Ogg container
    "wav":  "audio/wav",
    "flac": "audio/flac",
}


_VIDEO_MEDIA_TYPES: dict[str, str] = {
    "mp4":  "video/mp4",
    "webm": "video/webm",
    "mov":  "video/quicktime",
    "avi":  "video/x-msvideo",
    "mkv":  "video/x-matroska",
}


def _media_type(fmt: str) -> str:
    if fmt in _VIDEO_MEDIA_TYPES:
        return _VIDEO_MEDIA_TYPES[fmt]
    return _AUDIO_MEDIA_TYPES.get(fmt, "application/octet-stream")


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
        # Player client probe order. Each client is tried in sequence;
        # yt-dlp stops at the first one that returns a playable format.
        #   * `android_vr`  - VR surface, bypasses bot-wall on most
        #                     datacenter IPs without PoToken or cookies.
        #                     Listed first so it short-circuits before
        #                     the heavier web clients.
        #   * `tv`          - TV-app surface, no PoToken needed.
        #   * `ios`         - iOS app client; requires GVS PO Token which
        #                     bgutil mints. Independent code path from the
        #                     web clients — covers videos those miss.
        #   * `web_creator` - Desktop web variant; bgutil HTTP plugin
        #                     mints a PoToken for it (verified live).
        #   * `mweb`        - Mobile web variant, ditto.
        # When YT bot-walls our datacenter IP even android_vr fails and
        # all web clients return LOGIN_REQUIRED despite valid PoTokens.
        # The real remediation is cookies via /api/yt-cookies or the
        # YT_COOKIES_TXT operator secret.
        "player_client": ["android_vr", "tv", "ios", "web_creator", "mweb"],
        "player_skip": ["configs"],
    },
}
YDL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)


# ---- curl_cffi impersonation ----------------------------------------------
# A growing list of sites front their pages with Cloudflare /
# DataDome / PerimeterX fingerprint checks that look at the JA3 /
# JA4 TLS handshake and HTTP/2 frame ordering, NOT just headers.
# The default Python `requests` / `urllib3` stack has a deterministic
# fingerprint that those services flag as "bot". `curl_cffi` ships
# real browser TLS handshakes (Chrome/Safari/Firefox) and lets
# yt-dlp ride on them.
#
# We don't enable impersonation universally because:
#   1. Some extractors break under it (they assume the urllib3
#      backend's exact retry semantics).
#   2. It costs ~5x the per-request CPU vs. urllib3.
#   3. yt-dlp's own internal retries can pick the right backend
#      automatically when the URL's domain is known.
#
# Instead: for hostnames that historically 403 from datacenter +
# urllib3 (Cloudflare-fronted sites that don't take residential
# proxy alone), we ALSO request `impersonate=chrome` so the TLS
# fingerprint passes muster. The actual ImpersonateTarget value
# is left as a string ("chrome") and yt-dlp picks the freshest
# installed sub-version at request time.

_IMPERSONATE_DOMAINS: tuple[str, ...] = (
    # Cloudflare-fronted with strict TLS fingerprinting.
    "bloomberg.com", "bwbx.io",
    "newgrounds.com", "ngfiles.com",
    "9gag.com", "9cache.com",
    "coub.com",
    # MindGeek + several tubes use DataDome / Cloudflare bot
    # heuristics on top of geo restrictions. Impersonation on top
    # of residential proxy clears both.
    "xhamster.com", "xhcdn.com",
    "spankbang.com",
    "eporner.com", "epornercdn.com",
    "redtube.com", "rdtcdn.com",
    "thisvid.com", "ttcache.com",
    # Discord CDN media URLs sometimes fingerprint-check.
    "cdn.discordapp.com",
    # Rumble is Cloudflare-fronted and blocks datacenter TLS
    # handshakes without a recognised browser fingerprint.
    "rumble.com", "rumblecdn.com",
    # Odysee / LBRY: Cloudflare fronted; impersonation clears the bot check.
    "odysee.com", "lbry.tv",
    # YouTube actively drops our TLS handshake from datacenter IPs
    # without a recognised browser fingerprint - we observed
    # `[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation
    # of protocol` on every yt-dlp probe. Adding google's tube +
    # CDN domains to the impersonate list lets curl_cffi's chrome
    # fingerprint clear the gate. The bgutil PoToken sidecar then
    # handles the second-line "are you a bot" challenge.
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "googlevideo.com", "ytimg.com",
)

# Domain policy table. This is the single tuning surface for the mini's
# extractor behavior. Keeps "how aggressive should we be for host X?"
# decisions in one place instead of scattering timeout/retry magic
# numbers throughout the request paths.
#
# Keys:
#   socket_timeout_s   -> yt-dlp socket_timeout
#   info_budget_s      -> outer asyncio.wait_for budget on /api/info and
#                         streamable resolver
#   ydl_retries        -> yt-dlp request retries
#   fragment_retries   -> yt-dlp fragment retries (HLS/DASH)
#
# The first matching suffix wins.
_DOMAIN_POLICY: tuple[tuple[tuple[str, ...], dict[str, int]], ...] = (
    # YouTube: PoToken + player_client probing means extract_info can
    # legitimately run long. Give it more wall time before we cut it.
    (("youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com", "ytimg.com"), {
        "socket_timeout_s": 20,
        "info_budget_s": 65,
        "ydl_retries": 2,
        "fragment_retries": 3,
    }),
    # Social + Meta stack: slower first-byte through proxy and more
    # frequent transient throttles.
    (("instagram.com", "cdninstagram.com", "facebook.com", "fbcdn.net", "threads.net",
      "x.com", "twitter.com", "twimg.com", "reddit.com", "redd.it", "v.redd.it"), {
        "socket_timeout_s": 30,
        "info_budget_s": 45,
        "ydl_retries": 2,
        "fragment_retries": 3,
    }),
    # MindGeek tubes: PornHub CDN signs URLs per-IP, so yt-dlp
    # needs a consistent proxy session (handled by ytdlp-pipe tier).
    # Give a full budget so the pipe finishes before the outer timer fires.
    (("pornhub.com", "phncdn.com", "rt.pornhub.com", "youporn.com", "ypncdn.com",
      "tube8.com", "tube8cdn.com", "redtube.com", "rdtcdn.com"), {
        "socket_timeout_s": 25,
        "info_budget_s": 45,
        "ydl_retries": 2,
        "fragment_retries": 3,
    }),
    # xHamster: DataDome bot-check + frequent decipher regressions.
    # More retries help on transient 429s; budget lets yt-dlp try the
    # full impersonation handshake before falling through to Playwright.
    (("xhamster.com", "xhcdn.com"), {
        "socket_timeout_s": 25,
        "info_budget_s": 40,
        "ydl_retries": 2,
        "fragment_retries": 3,
    }),
    # SpankBang: moderate datacenter hostility; proxy + impersonation
    # usually sufficient. Shorter budget so Playwright fires quickly.
    (("spankbang.com",), {
        "socket_timeout_s": 20,
        "info_budget_s": 30,
        "ydl_retries": 1,
        "fragment_retries": 2,
    }),
    # Eporner: known yt-dlp hash-extraction regression (issue #16277).
    # Short budget so we fall through to Playwright fast when yt-dlp fails.
    (("eporner.com", "epornercdn.com"), {
        "socket_timeout_s": 20,
        "info_budget_s": 30,
        "ydl_retries": 1,
        "fragment_retries": 2,
    }),
    # xVideos / xNXX: generally stable yt-dlp extractors. Moderate settings.
    (("xvideos.com", "xvideos-cdn.com", "xnxx.com", "xnxx-cdn.com"), {
        "socket_timeout_s": 20,
        "info_budget_s": 30,
        "ydl_retries": 1,
        "fragment_retries": 2,
    }),
    # ThisVid (KVS): embed links removed May 2026. yt-dlp may fail
    # on embed-shaped URLs; keep budget short so Playwright catches it.
    (("thisvid.com", "ttcache.com"), {
        "socket_timeout_s": 20,
        "info_budget_s": 30,
        "ydl_retries": 1,
        "fragment_retries": 2,
    }),
    # Rumble: Cloudflare bot-check + dedicated CDN. Impersonation helps.
    (("rumble.com", "rumblecdn.com"), {
        "socket_timeout_s": 20,
        "info_budget_s": 35,
        "ydl_retries": 1,
        "fragment_retries": 2,
    }),
)


def _hostname(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _domain_policy_for(url: str) -> dict[str, int]:
    host = _hostname(url)
    # Global defaults - intentionally conservative to keep dead-URL
    # feedback quick for the long tail.
    out = {
        "socket_timeout_s": 15,
        "info_budget_s": 25,
        "ydl_retries": 1,
        "fragment_retries": 2,
    }
    if not host:
        return out
    for suffixes, policy in _DOMAIN_POLICY:
        if any(host == s or host.endswith("." + s) for s in suffixes):
            out.update(policy)
            break
    return out


def _is_slow_resolve_host(url: str) -> bool:
    return _domain_policy_for(url).get("socket_timeout_s", 15) >= 30


def _socket_timeout_for(url: str, default: int = 15) -> int:
    """Return the per-host socket_timeout to use for yt-dlp."""
    policy = _domain_policy_for(url)
    return int(policy.get("socket_timeout_s", default))


def _info_resolve_budget_s(url: str) -> int:
    """Outer asyncio.wait_for budget for /api/info and stream resolver."""
    return int(_domain_policy_for(url).get("info_budget_s", 25))


def _is_youtube_host(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(
        host == s or host.endswith("." + s)
        for s in ("youtube.com", "youtu.be", "youtube-nocookie.com",
                  "googlevideo.com", "ytimg.com")
    )


def _should_impersonate(url: str) -> bool:
    """True if this URL's hostname is on the impersonation allowlist.
    Mirrors `proxy_router.should_proxy()` semantics: hostname-suffix
    match at a label boundary so foo.bar.bloomberg.com matches but
    notbloomberg.com.evil.tld doesn't."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for suffix in _IMPERSONATE_DOMAINS:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def _maybe_impersonate(opts: dict, url: str) -> dict:
    """Mutate `opts` in place to add yt-dlp's `impersonate` option
    if the URL's hostname is on the allowlist. Returns `opts`
    unchanged so callers can chain it. No-op if curl_cffi isn't
    importable - we don't want to fail the request just because
    impersonation isn't available."""
    if not _should_impersonate(url):
        return opts
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        # `client='chrome'` with no version pin = yt-dlp picks the
        # newest installed Chrome target. Same for other clients;
        # we deliberately don't pin a version to avoid stale-pin
        # failures when curl_cffi ships a new browser version and
        # drops an old one.
        opts["impersonate"] = ImpersonateTarget(client="chrome")
    except ImportError:
        # curl_cffi extras not installed; silently fall through to
        # the urllib3 backend.
        pass
    return opts

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


# ---------------------------------------------------------------------------
# Per-session client-supplied YouTube cookies
# ---------------------------------------------------------------------------
# YouTube has been increasingly aggressive about blocking datacenter
# IPs without an authenticated session. The mini app's HF Space
# outbound IP doesn't have any cookies, which is why ~70% of YT
# URLs fail with "Sign in to confirm you're not a bot".
#
# Users CAN bring their own browser cookies. The flow:
#   1. Browser POSTs the user's exported `cookies.txt` content
#      to /api/yt-cookies. Server validates it parses as Netscape
#      format, generates a random 24-byte session token, materializes
#      the cookies into a tempfile, and returns the token.
#   2. Browser includes `yt_session=<token>` on subsequent
#      /api/info, /api/process, and /api/stream-download requests.
#   3. Server looks up the session, hands the cookiefile path to
#      yt-dlp for that single request.
#
# Security model:
#   * Cookies live in process memory only (path is in /tmp, written
#     0600). NEVER persisted to a database, log line, or response
#     body.
#   * Session token is 24 bytes from `secrets.token_urlsafe()`,
#     which is the same primitive we use for stream tokens.
#     Probability of collision/guess is negligible.
#   * Token is short-lived: 30 min from upload, then the entry +
#     tempfile is purged. No "remember me" flag, no extension.
#   * Token is bound to the uploading client IP. Requests with the
#     correct token from a different IP are rejected to prevent a
#     leaked token (e.g. via screenshot, browser history) being
#     replayable across the internet.
#   * Tempfile is unlinked on TTL expiry AND on process exit.
#   * Cookie content is NEVER echoed back. Only `{ok: true, expires_in}`.
#   * Number of active sessions is capped (env-tunable) to bound
#     memory under abuse.
#
# Threat scenarios this DOESN'T defend against:
#   * The HF Space operator is a compromised insider and can read
#     /tmp. (True for the existing YT_COOKIES_TXT secret too;
#     the operator is part of the trust boundary.)
#   * The client uploads cookies for an account they don't own.
#     (User's problem. We just route bytes; we don't verify
#     account ownership.)
#
# Why we don't encrypt at rest in the tempfile:
#   The encryption key would have to live in the same process,
#   which gains us nothing against the threat scenarios we DO
#   defend against (token guessing, accidental log echo). The
#   tempfile is 0600 + lives in tmpfs on HF Spaces.

YT_SESSION_TTL_S = int(os.environ.get("CMVIDEO_YT_SESSION_TTL_S", "1800"))
YT_SESSION_MAX_ACTIVE = int(os.environ.get("CMVIDEO_YT_SESSION_MAX_ACTIVE", "50"))
# Max cookies.txt body size: typical YT export is ~2-8 KB. 256 KB
# is generous and bounds memory under abuse.
YT_COOKIES_MAX_BYTES = 256 * 1024

# Session table:
#   token -> (cookiefile_path, client_ip, expires_at)
import threading as _threading_yt  # threading is also imported lower for jobs
_yt_sessions: dict[str, tuple[str, str, float]] = {}
_yt_sessions_lock = _threading_yt.Lock()


# Netscape cookies.txt: each non-comment line is exactly 7
# tab-separated fields. We match liberally - just need to know
# the body parses without exploding.
_NETSCAPE_COOKIE_LINE = re.compile(r"^[^#\s][^\t]*\t[^\t]*\t[^\t]*\t[^\t]*\t[^\t]*\t[^\t]*\t.*$")


def _validate_netscape_cookies(raw: str) -> tuple[bool, int]:
    """Return (is_valid, n_yt_cookies). Doesn't try to be a full
    parser - just checks the file has at least one well-formed
    cookie line whose domain looks YouTube-shaped."""
    if not raw or len(raw) > YT_COOKIES_MAX_BYTES:
        return False, 0
    yt_count = 0
    has_any_cookie = False
    for line in raw.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if not _NETSCAPE_COOKIE_LINE.match(line):
            continue
        has_any_cookie = True
        domain = line.split("\t", 1)[0].lstrip(".")
        if domain.endswith("youtube.com") or domain.endswith("google.com") or domain.endswith("youtu.be"):
            yt_count += 1
    return has_any_cookie, yt_count


def _purge_expired_yt_sessions() -> None:
    """Drop entries whose TTL has elapsed, unlinking the
    associated tempfile. Called lazily before every session
    create/lookup so we don't need a separate sweeper thread.

    We hold the lock the whole time; the table is small (~tens
    of entries max) so this is microseconds."""
    now = time.time()
    with _yt_sessions_lock:
        dead = [tok for tok, (_, _, exp) in _yt_sessions.items() if exp < now]
        for tok in dead:
            path, _, _ = _yt_sessions.pop(tok)
            try:
                os.unlink(path)
            except OSError:
                pass


def _yt_session_create(raw: str, client_ip: str) -> tuple[str, int]:
    """Persist `raw` cookies.txt content to a tempfile, register
    a new session for `client_ip`, return (token, n_yt_cookies).
    Raises ValueError if validation fails or the table is full."""
    ok, yt_count = _validate_netscape_cookies(raw)
    if not ok:
        raise ValueError("That doesn't parse as a Netscape cookies.txt file.")
    if yt_count == 0:
        raise ValueError(
            "Cookies file uploaded but no YouTube cookies found. "
            "Make sure you exported cookies for youtube.com, not just google.com."
        )
    _purge_expired_yt_sessions()
    with _yt_sessions_lock:
        if len(_yt_sessions) >= YT_SESSION_MAX_ACTIVE:
            raise ValueError("Too many active YouTube cookie sessions; try again later.")
        # Materialize. fdopen+write under 0600.
        fd, path = tempfile.mkstemp(prefix="cmvm_ytuser_", suffix=".txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(raw if raw.endswith("\n") else raw + "\n")
            os.chmod(path, 0o600)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        token = secrets.token_urlsafe(24)
        _yt_sessions[token] = (path, client_ip, time.time() + YT_SESSION_TTL_S)
        return token, yt_count


def _yt_session_get(token: Optional[str], client_ip: str) -> Optional[str]:
    """Return the cookiefile path for `token` if it's valid and
    matches `client_ip`. Returns None for any failure mode -
    callers fall through to the normal no-cookies path. We
    deliberately don't differentiate "wrong IP" / "expired" /
    "not found" in the return value to avoid giving a probe
    oracle for guessing tokens."""
    if not token:
        return None
    _purge_expired_yt_sessions()
    with _yt_sessions_lock:
        entry = _yt_sessions.get(token)
    if entry is None:
        return None
    path, owner_ip, expires_at = entry
    if expires_at < time.time():
        return None
    # Strict per-IP binding: token only works from the IP it was
    # created on. We accept the operator-allowlist owner IPs as
    # an exception so a session created on a phone keeps working
    # if the user switches networks (CGNAT) - but only if the
    # allowlist is actually configured.
    if owner_ip != client_ip and not _is_owner_ip(client_ip):
        return None
    return path


def _yt_session_status() -> dict:
    """Diagnostic snapshot for /api/limits."""
    _purge_expired_yt_sessions()
    with _yt_sessions_lock:
        return {
            "active_sessions": len(_yt_sessions),
            "ttl_s": YT_SESSION_TTL_S,
            "max_active": YT_SESSION_MAX_ACTIVE,
        }


def _resolve_cookiefile(yt_session_token: Optional[str], client_ip: str) -> Optional[str]:
    """Pick the cookiefile to feed yt-dlp for a given request.

    Priority:
      1. User-supplied session cookies (per-IP, 30-min TTL) when
         the request carries a valid `yt_session` token.
      2. Operator-supplied env-var cookies (`YT_COOKIES_TXT`).
      3. None - yt-dlp runs without cookies (anonymous).

    Returns the path or None. NEVER raises - on any error we
    fall back to None so the request still goes through (just
    without cookies)."""
    try:
        path = _yt_session_get(yt_session_token, client_ip)
        if path:
            return path
    except Exception:
        log.exception("yt session lookup failed; falling back to no-cookie path")
    return YT_COOKIES_FILE


# Per-request cookiefile context. Set in the FastAPI handler
# before kicking off the yt-dlp call (which runs in a thread
# pool via `asyncio.to_thread`). ContextVars are propagated
# across `asyncio.to_thread` boundaries by stdlib, so the
# thread-pool worker reads the same value the handler set.
#
# Why ContextVar instead of an explicit parameter on every
# downstream helper:
#   * `_do_info`, `_resolve_streamable_format`, `_do_download`
#     are called from MANY sites (info endpoint, stream init
#     endpoint, async job pipeline). Threading `cookiefile`
#     through every signature would touch ~20 call sites and
#     a dozen Pydantic models. ContextVar is a one-line set
#     at the request boundary that everyone reads
#     transparently.
#   * Default of None falls back cleanly to `YT_COOKIES_FILE`
#     so existing call paths that don't set the context behave
#     exactly as before.
import contextvars
_request_cookiefile: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_request_cookiefile", default=None,
)


def _request_cookiefile_for_url(url: str) -> Optional[str]:
    """Return the cookiefile path that the current request's
    yt-dlp call should use, if any. Reads the per-request
    ContextVar set by the API handler; falls back to the
    operator's env-var cookies if no per-request override.

    The `url` argument is reserved for future per-domain
    routing (e.g. don't apply Instagram cookies to a
    YouTube URL) but is currently informational only -
    yt-dlp itself filters cookies by domain at request time."""
    override = _request_cookiefile.get()
    if override:
        return override
    return YT_COOKIES_FILE


def _ydl_common_opts(
    tmpdir: Path,
    max_duration: int,
    max_filesize: int,
    *,
    url: str = "",
    progress_hook=None,
) -> dict:
    policy = _domain_policy_for(url) if url else {}
    retries = int(policy.get("ydl_retries", 1))
    frag_retries = int(policy.get("fragment_retries", 2))
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
        "retries": retries,
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
        "fragment_retries": frag_retries,
        # Don't stop the whole job on a single failed fragment. We
        # can usually drop one and still produce a watchable file.
        "skip_unavailable_fragments": True,
        "extractor_args": YDL_EXTRACTOR_ARGS,
        "http_headers": {"User-Agent": YDL_USER_AGENT},
    }
    # Cookiefile resolution: per-request override (user uploaded
    # cookies via /api/yt-cookies) wins over the operator's
    # env-var pile. `_request_cookiefile_for_url` reads a
    # ContextVar set in the request handler; default is None
    # which falls back to the env-var.
    cf = _request_cookiefile_for_url("")
    if cf:
        opts["cookiefile"] = cf
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
        #
        # `!*preview` / `!*trailer` / `!*sample`: yt-dlp "does not
        # contain" operator. Filters out preview-labelled format IDs
        # that some tube sites expose alongside the full video. Sites
        # that use numeric format IDs (RedTube: "240"/"720") are
        # unaffected; the filter only fires when a format_id literally
        # contains the word "preview", "trailer", or "sample".
        no_preview = "[format_id!*preview][format_id!*trailer][format_id!*sample]"
        return (
            # 1) Best video at the cap, AVC/m4a where possible (mp4
            #    output without a transcode).
            f"bestvideo*[height<={target}]{fps_clause}[ext=mp4][vcodec^=avc1]{no_preview}+bestaudio[ext=m4a]/"
            f"bestvideo*[height<={target}][ext=mp4][vcodec^=avc1]{no_preview}+bestaudio[ext=m4a]/"
            # 2) Best video at the cap, mp4 video container, any audio.
            f"bestvideo*[height<={target}]{fps_clause}[ext=mp4]{no_preview}+bestaudio/"
            f"bestvideo*[height<={target}][ext=mp4]{no_preview}+bestaudio/"
            # 3) Best video at the cap, ANY container (webm/etc.) +
            #    best audio. yt-dlp will remux to mp4 via the
            #    FFmpegVideoRemuxer postprocessor we set in opts.
            f"bestvideo*[height<={target}]{fps_clause}{no_preview}+bestaudio/"
            f"bestvideo*[height<={target}]{no_preview}+bestaudio/"
            # 4) Single-file at the EXACT cap height. This is the
            #    no-merge fast path for sites that ship progressive
            #    720p / 1080p (most non-YouTube CDNs). It's clause
            #    4, not clause 1, because we only want it when the
            #    progressive resolution actually matches the cap -
            #    not when it's a low-res fallback that happens to
            #    sneak in under <=H.
            f"best[height={target}]{fps_clause}[ext=mp4][acodec!=none]{no_preview}/"
            f"best[height={target}][ext=mp4][acodec!=none]{no_preview}/"
            # 5) Last resort at this tier — any single-file <=H,
            #    still avoiding preview-labelled IDs where possible.
            f"best[height<={target}]{no_preview}/"
            f"best[height<={target}]"
        )

    base = tier(h)
    # If HD was asked for and 1080 isn't available, fall through
    # the entire 720 tier rather than failing. yt-dlp picks the
    # first chain that matches.
    if h > 720:
        base += "/" + tier(720)
    return base + "/best"


def _video_format_fallback_ladder(fps: str = "source", *, height: int | None = None) -> str:
    """Build a quality fallback ladder for yt-dlp selectors.

    Example for 1080p request:
      1080p selector -> 720p -> 480p -> 360p -> best

    This maximizes success on sites whose top variant is flaky,
    geo-filtered, or transiently unavailable while still preferring the
    requested height first.
    """
    requested = int(height or MAX_VIDEO_HEIGHT)
    # Conservative lower tiers that are widely available across
    # extractor families and CDNs.
    candidates = [requested, 1440, 1080, 720, 480, 360, 240]
    seen: set[int] = set()
    parts: list[str] = []
    for h in candidates:
        if h > requested and h != requested:
            continue
        if h in seen:
            continue
        seen.add(h)
        sel = _video_format_selector(fps, height=h)
        if sel.endswith("/best"):
            sel = sel[:-5]
        parts.append(sel)
    if not parts:
        return _video_format_selector(fps, height=requested)
    return "/".join(parts) + "/best"


def _webm_format_selector(fps: str, *, height: int) -> str:
    """yt-dlp format selector that targets native VP9/VP8+Opus/Vorbis WebM
    streams first, avoiding H.264/AAC sources that can't be muxed into a
    valid WebM container without an expensive transcode."""
    no_preview = "[format_id!*preview][format_id!*trailer][format_id!*sample]"
    fps_clause = {"source": "", "30": "[fps<=30]", "60": "[fps>=50]"}.get(fps, "")
    return (
        # 1) VP9 video + Opus audio in WebM (YouTube native, no transcode)
        f"bestvideo[height<={height}]{fps_clause}[ext=webm][vcodec^=vp]{no_preview}+bestaudio[ext=webm][acodec=opus]/"
        f"bestvideo[height<={height}][ext=webm][vcodec^=vp]{no_preview}+bestaudio[ext=webm]/"
        # 2) Any WebM video + best WebM-compatible audio
        f"bestvideo[height<={height}]{fps_clause}[ext=webm]{no_preview}+bestaudio[ext=webm]/"
        f"bestvideo[height<={height}][ext=webm]{no_preview}+bestaudio[ext=webm]/"
        # 3) Single-file WebM at the cap height
        f"best[height={height}]{fps_clause}[ext=webm][acodec!=none]{no_preview}/"
        f"best[height={height}][ext=webm][acodec!=none]{no_preview}/"
        # 4) Any single-file WebM ≤ cap
        f"best[height<={height}]{fps_clause}[ext=webm]{no_preview}/"
        f"best[height<={height}][ext=webm]{no_preview}/"
        # 5) Absolute last resort: best webm-or-any (yt-dlp tries webm mux)
        f"bestvideo[height<={height}]{no_preview}+bestaudio/"
        f"best[height<={height}]"
    )


def _webm_format_fallback_ladder(fps: str = "source", *, height: int | None = None) -> str:
    """Same multi-tier height fallback as `_video_format_fallback_ladder` but
    using the webm-native selector at each tier."""
    requested = int(height or MAX_VIDEO_HEIGHT)
    candidates = [requested, 1440, 1080, 720, 480, 360, 240]
    seen: set[int] = set()
    parts: list[str] = []
    for h in candidates:
        if h > requested and h != requested:
            continue
        if h in seen:
            continue
        seen.add(h)
        sel = _webm_format_selector(fps, height=h)
        if sel.endswith("/best"):
            sel = sel[:-5]
        parts.append(sel)
    if not parts:
        return _webm_format_selector(fps, height=requested)
    return "/".join(parts) + "/best"


def _mkv_format_selector(fps: str, *, height: int) -> str:
    """yt-dlp format selector for MKV output. MKV accepts any codec so we
    go straight for best quality without codec restrictions — VP9, AVC,
    AV1, Opus, AAC, all mux cleanly into MKV without a transcode pass."""
    no_preview = "[format_id!*preview][format_id!*trailer][format_id!*sample]"
    fps_clause = {"source": "", "30": "[fps<=30]", "60": "[fps>=50]"}.get(fps, "")
    return (
        f"bestvideo[height<={height}]{fps_clause}{no_preview}+bestaudio/"
        f"bestvideo[height<={height}]{no_preview}+bestaudio/"
        f"best[height={height}]{fps_clause}[acodec!=none]{no_preview}/"
        f"best[height={height}][acodec!=none]{no_preview}/"
        f"best[height<={height}]{no_preview}/"
        f"best[height<={height}]"
    )


def _mkv_format_fallback_ladder(fps: str = "source", *, height: int | None = None) -> str:
    requested = int(height or MAX_VIDEO_HEIGHT)
    candidates = [requested, 1440, 1080, 720, 480, 360, 240]
    seen: set[int] = set()
    parts: list[str] = []
    for h in candidates:
        if h > requested and h != requested:
            continue
        if h in seen:
            continue
        seen.add(h)
        sel = _mkv_format_selector(fps, height=h)
        if sel.endswith("/best"):
            sel = sel[:-5]
        parts.append(sel)
    if not parts:
        return _mkv_format_selector(fps, height=requested)
    return "/".join(parts) + "/best"


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
    # mp4 mode wants a video container; an audio request only gets
    # the fast-path when the source IS already that exact audio
    # format (e.g. `format=flac` + `.flac` source). Otherwise we
    # fall through to the yt-dlp path so the FFmpegExtractAudio
    # postprocessor can transcode into the requested codec - the
    # earlier behaviour of returning ANY audio file for ANY audio
    # request meant `format=flac` + an mp4 source served back the
    # mp4 unmodified, breaking caller expectations.
    src_ext = "." + path_lower.rsplit(".", 1)[-1]
    # For video formats, only fast-path when the source IS already in
    # an acceptable container for the requested format — no remux here.
    _VIDEO_FAST_PATH_EXTS: dict[str, set[str]] = {
        "mp4":  {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv"},
        "webm": {".webm"},
        "mov":  {".mov", ".mp4", ".m4v"},  # MOV and MP4 are ISOBMFF siblings
        "mkv":  {".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v"},
        "avi":  {".avi"},
    }
    if fmt in _VIDEO_FAST_PATH_EXTS and src_ext not in _VIDEO_FAST_PATH_EXTS[fmt]:
        return None
    if fmt not in ALLOWED_VIDEO_FORMATS:
        # Audio formats: src_ext must equal `.{fmt}` exactly.
        # `.m4a` is treated as a synonym of `.aac` because both
        # contain raw AAC and don't need a re-encode pass.
        ok_exts = {f".{fmt}"}
        if fmt in ("m4a", "aac"):
            ok_exts |= {".m4a", ".aac"}
        if src_ext not in ok_exts:
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
    opts = _ydl_common_opts(
        tmpdir, max_duration, max_filesize, url=url, progress_hook=progress_hook
    )
    # Per-host socket_timeout override - sites on _SLOW_RESOLVE_DOMAINS
    # (Instagram and friends) are reliably too slow for the default
    # 12s and time out before they finish loading their JSON page.
    opts["socket_timeout"] = _socket_timeout_for(url, default=opts.get("socket_timeout", 12))
    # Per-domain residential-proxy routing. yt-dlp's `proxy` opt
    # routes ALL fetches in this run (page + segments) through the
    # proxy, which is what we want - the segment downloads are
    # where IG / TT / etc actually rate-limit hardest. Direct
    # connection (proxy=None) for everything else so we don't burn
    # paid GB on YouTube / Reddit / Vimeo etc.
    proxy_for_this = _proxy_router.proxy_for_url(url)
    if proxy_for_this:
        opts["proxy"] = proxy_for_this
        log.info("yt-dlp: routing %s through residential proxy", _proxy_router._hostname_of(url))
    # Per-domain TLS impersonation for Cloudflare-fronted sites.
    # No-op when the URL isn't on the impersonate allowlist or
    # curl_cffi is missing.
    _maybe_impersonate(opts, url)
    # YouTube's VP9/WebM DASH streams are reliably 403'd from datacenter
    # IPs — the CDN restricts VP9 segment delivery to browser clients.
    # Silently fall back to MP4 (H.264) which uses less-restricted DASH
    # or progressive streams. MKV/MOV/AVI all use the MP4 ladder anyway.
    if fmt == "webm" and _is_youtube_host(url):
        fmt = "mp4"
    if fmt == "mp4":
        opts.update({
            "format": _video_format_fallback_ladder(fps, height=height),
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
            ],
        })
    elif fmt == "webm":
        opts.update({
            "format": _webm_format_fallback_ladder(fps, height=height),
            "merge_output_format": "webm",
        })
    elif fmt == "mkv":
        opts.update({
            "format": _mkv_format_fallback_ladder(fps, height=height),
            "merge_output_format": "mkv",
        })
    elif fmt in ("mov", "avi"):
        # MOV is ISOBMFF (same as MP4, Apple-compatible container).
        # AVI is legacy — prefer AVC+AAC to avoid VP9→H.264 transcode.
        # Both reuse the MP4 format ladder (AVC+AAC streams preferred).
        opts.update({
            "format": _video_format_fallback_ladder(fps, height=height),
            "merge_output_format": fmt,
        })
    else:
        # Audio path. `fmt` is the requested audio container/codec
        # name (mp3 / m4a / aac / ogg / opus / wav / flac). We pick
        # the matching FFmpegExtractAudio codec and skip the bitrate
        # arg for lossless formats (yt-dlp passes it to ffmpeg `-ab`
        # which is meaningless for wav/flac).
        codec = AUDIO_CODEC_MAP.get(fmt, "mp3")
        pp: dict = {
            "key": "FFmpegExtractAudio",
            "preferredcodec": codec,
        }
        if fmt not in LOSSLESS_AUDIO_FORMATS:
            pp["preferredquality"] = AUDIO_BITRATE_KBPS
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [pp],
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
            max_filesize=max_filesize,
        )
    except _extractors.ExtractionError as exc:
        # Surface as a yt-dlp DownloadError-shaped exception so the
        # caller's friendly-error mapping still works for the common
        # cases. Tools that ran are listed in the exception text.
        # Defense-in-depth pass through `redact_secrets` even though
        # `ExtractionError.__str__` already redacts - this catches
        # the case where someone constructs the DownloadError text
        # from `exc.message` or `exc.attempts` directly without
        # going through `__str__`.
        raise yt_dlp.utils.DownloadError(_proxy_router.redact_secrets(str(exc))) from exc

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


_RETRYABLE_EXTRACT_ERR_SIGNALS = (
    "timed out",
    "timeout",
    "connection reset",
    "connection aborted",
    "temporarily unavailable",
    "temporary failure",
    "http error 429",
    "http error 5",
    "tls",
    "ssl",
    "unexpected eof",
    "unable to download webpage",
    "unable to download api page",
    "failed to perform, curl",
    "proxyerror",
)


def _is_retryable_extract_error(exc: Exception) -> bool:
    low = str(exc or "").lower()
    return any(sig in low for sig in _RETRYABLE_EXTRACT_ERR_SIGNALS)


def _extract_info_with_retry_matrix(
    url: str,
    base_opts: dict,
    *,
    purpose: str,
) -> dict:
    """Run yt-dlp extract_info with a bounded retry matrix.

    Matrix dimensions:
      - proxy route: configured proxy (if any) -> direct
      - impersonation: on -> off (only for impersonate-eligible hosts)

    This catches two common production failures:
      1) proxy pool issues (timeouts / TLS flap) where direct succeeds
      2) occasional curl_cffi/impersonation regressions where plain
         urllib backend succeeds.
    """
    host = _proxy_router._hostname_of(url)
    proxy_candidates: list[Optional[str]] = [None]
    try:
        p = _proxy_router.proxy_for_url(url)
        if p:
            proxy_candidates = [p, None]
    except Exception:
        log.exception("%s retry-matrix: proxy_for_url failed; using direct only", purpose)
        proxy_candidates = [None]

    use_impersonate = _should_impersonate(url)
    imp_candidates = [True, False] if use_impersonate else [False]

    attempts: list[tuple[Optional[str], bool]] = []
    for px in proxy_candidates:
        for imp in imp_candidates:
            attempts.append((px, imp))

    last_exc: Exception | None = None
    total = len(attempts)
    for idx, (px, imp) in enumerate(attempts, start=1):
        opts = dict(base_opts)
        if px:
            opts["proxy"] = px
        else:
            opts.pop("proxy", None)
        if imp:
            _maybe_impersonate(opts, url)
        else:
            opts.pop("impersonate", None)

        # Later attempts get a slightly bigger timeout/retry budget.
        if idx > 1:
            opts["socket_timeout"] = min(int(opts.get("socket_timeout", 15)) + 8, 45)
            opts["retries"] = max(int(opts.get("retries", 1)), 2)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info is None:
                raise RuntimeError("extract_info returned None")
            return info
        except Exception as exc:
            last_exc = exc
            log.info(
                "%s retry-matrix %d/%d host=%s proxy=%s impersonate=%s timeout=%s retries=%s err=%s: %s",
                purpose, idx, total, host, bool(px), imp,
                opts.get("socket_timeout"), opts.get("retries"),
                type(exc).__name__, _safe_log_msg(exc),
            )
            if not _is_retryable_extract_error(exc):
                raise
            # Otherwise continue to next matrix cell.
            continue

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("extract_info failed without exception detail")


def _cobalt_info_for_url(url: str) -> Optional[dict]:
    """Get minimal video metadata from Cobalt API (title from filename).
    Returns None if Cobalt is not configured or the request fails."""
    try:
        if not _extractors.cobalt_available():
            return None
        base = _extractors.COBALT_API_BASE.rstrip("/")
        key = _extractors.COBALT_API_KEY or ""
        body = json.dumps({
            "url": url,
            "videoQuality": "720",
            "downloadMode": "auto",
        }).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "CMVideo-Mini/1.0",
        }
        if key:
            headers["Authorization"] = f"Api-Key {key}"
        req = urllib.request.Request(base, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        filename = payload.get("filename") or ""
        # Strip YouTube video ID suffix and file extension from Cobalt filename.
        title = re.sub(r"\s*\[[A-Za-z0-9_-]{11}\]\s*", " ", filename).strip()
        title = re.sub(r"\.[a-z0-9]{2,5}$", "", title, flags=re.I).strip()
        title = title or "YouTube Video"
        return {
            "title": title[:200],
            "uploader": "",
            "duration": None,
            "thumbnail": None,
            "extractor": "cobalt",
            "over_cap_download": False,
            "over_cap_censor": False,
        }
    except Exception:  # noqa: BLE001
        return None


def _do_info(url: str) -> dict:
    yt_debug = _is_youtube_host(url)
    info_opts = {
        "quiet": not yt_debug,
        "no_warnings": not yt_debug,
        "verbose": yt_debug,
        "noplaylist": True,
        "skip_download": True,
        # Per-host: 30s for IG / cdninstagram, 15s elsewhere.
        # Instagram's web frontend often takes 18-25s to first byte
        # through the residential proxy and the default 15s causes
        # spurious "Source took too long to respond" 504s.
        "socket_timeout": _socket_timeout_for(url, default=15),
        "retries": int(_domain_policy_for(url).get("ydl_retries", 1)),
        "extractor_args": YDL_EXTRACTOR_ARGS,
        "http_headers": {"User-Agent": YDL_USER_AGENT},
    }
    cf = _request_cookiefile_for_url(url)
    if cf:
        info_opts["cookiefile"] = cf

    if _is_youtube_host(url):
        # For YouTube info extraction, use direct (no proxy) to avoid the
        # residential proxy's known YouTube TLS hang. Cookies + bgutil handle
        # authentication without needing a residential IP for metadata lookup.
        # The proxy is still used for actual CDN segment downloads via
        # extract_with_fallbacks. If yt-dlp fails, Cobalt gives us the title.
        try:
            direct_opts = dict(info_opts)
            direct_opts.pop("proxy", None)
            direct_opts["socket_timeout"] = 15
            direct_opts["retries"] = 1
            with yt_dlp.YoutubeDL(direct_opts) as ydl:
                yt_info = ydl.extract_info(url, download=False)
            if yt_info is not None:
                duration = yt_info.get("duration")
                return {
                    "title": (yt_info.get("title") or "Untitled")[:200],
                    "uploader": (yt_info.get("uploader") or "")[:120],
                    "duration": int(duration) if isinstance(duration, (int, float)) else None,
                    "thumbnail": yt_info.get("thumbnail"),
                    "extractor": yt_info.get("extractor_key"),
                    "over_cap_download": isinstance(duration, (int, float)) and duration > MAX_DOWNLOAD_DURATION_SECONDS,
                    "over_cap_censor": isinstance(duration, (int, float)) and duration > MAX_CENSOR_DURATION_SECONDS,
                }
        except Exception as yt_exc:
            log.info("_do_info youtube yt-dlp failed (%s): %s", type(yt_exc).__name__, str(yt_exc)[:200])
        cobalt_info = _cobalt_info_for_url(url)
        if cobalt_info is not None:
            return cobalt_info
        raise yt_dlp.utils.DownloadError("YouTube info unavailable from this server. The download may still work — try submitting directly.")

    info = _extract_info_with_retry_matrix(url, info_opts, purpose="info")
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


# Tier 0 handles all common video containers directly.
_DIRECT_VIDEO_EXTS = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi")
_DIRECT_OK_CONTENT_TYPES = (
    "video/mp4", "video/x-m4v", "video/quicktime",
    "video/webm",
    "video/x-matroska", "video/x-msvideo",
    "application/mp4", "application/octet-stream", "binary/octet-stream",
)
_DIRECT_MP4_MIMES = ("video/mp4", "video/x-m4v", "application/mp4", "video/quicktime")

# Placeholder/holding clips some hosts return while the "real" video is
# still being prepared (e.g. `processing.mp4`). Serving these as success
# is worse than a clear failure because users think the download worked.
_PLACEHOLDER_MEDIA_HINTS = (
    "processing", "transcoding", "encoding", "rendering",
    "pending", "not_ready", "please_wait", "coming_soon",
    "placeholder",
)


def _looks_placeholder_media_url(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _PLACEHOLDER_MEDIA_HINTS)


# Some hosts return tiny "processing" teaser MP4s through their embed/CDN
# endpoints. Fast-path streaming should be disabled there so the client
# falls back to /api/process, which resolves the real media asset.
_STREAM_FASTPATH_BLOCKED_DOMAINS = (
    "thisvid.com",
    "ttcache.com",
)

# These domains sign their CDN delivery URLs to a specific exit IP.
# Tier-1 direct streaming resolves the URL via the proxy but then
# GETs the bytes potentially through a different pool exit node,
# breaking the IP-bound signature (HTTP 474 / 403 on the CDN).
# Force these to Tier-2 (yt-dlp subprocess) so yt-dlp handles
# both metadata resolution AND byte-fetching with the same --proxy.
_YTDLP_PIPE_REQUIRED_DOMAINS = frozenset({
    "pornhub.com",
    "phncdn.com",
    "rt.pornhub.com",
})


def _stream_fastpath_blocked(url: str) -> bool:
    host = _proxy_router._hostname_of(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in _STREAM_FASTPATH_BLOCKED_DOMAINS)


def _resolve_streamable_direct_url(url: str) -> Optional[dict]:
    """Tier 0 of the fast path: plain direct media URL, no yt-dlp.

    Probes `url` with a small ranged GET (1 byte) - servers that
    don't honour HEAD usually still answer Range, and a 1-byte read
    leaves no measurable bandwidth footprint. Returns a `method:
    direct` dict if the response looks like a streamable MP4, else
    None so the caller falls through to yt-dlp.

    Why ranged GET instead of HEAD:
      * Some CDNs return 405 / 403 / different headers on HEAD.
      * `Range: bytes=0-0` always returns the same Content-Type
        as a full GET would, with `Content-Range` confirming the
        total size, even on servers with broken HEAD.
      * Closes the connection immediately after the 1-byte body.

    Returns the same `method: direct` shape that tier 1 uses so
    `api_stream_serve` doesn't need a separate dispatch branch.
    """
    try:
        path_lower = urlparse(url).path.lower()
    except Exception:
        return None
    if not any(path_lower.endswith(ext) for ext in _DIRECT_VIDEO_EXTS):
        return None
    if _looks_placeholder_media_url(url):
        return None

    headers = {
        "User-Agent": YDL_USER_AGENT,
        "Range": "bytes=0-0",
        "Accept": "*/*",
    }
    # Per-domain residential proxy for consistency with the rest
    # of the pipeline. Direct URLs are usually CDN-hosted and not
    # IP-gated, but if they ARE on the proxy allowlist (e.g. a
    # tube CDN) we want to hit them through the same egress IP
    # the eventual stream will use, otherwise we'd probe-and-
    # serve from two different IPs and might trip session-binding.
    proxies = None
    try:
        p = _proxy_router.proxy_for_url(url)
        if p:
            proxies = {"http": p, "https": p}
    except Exception:
        log.exception("streamable tier-0: proxy_for_url() failed; falling back to direct")

    try:
        resp = requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=(STREAM_CONNECT_TIMEOUT_S, 12),
            allow_redirects=True,
            proxies=proxies,
        )
    except requests.RequestException as exc:
        log.info("streamable tier-0: probe failed for %s: %s: %s",
                 _proxy_router._hostname_of(url),
                 type(exc).__name__,
                 _safe_log_msg(exc))
        return None

    try:
        # 200 OK or 206 Partial Content both mean we got bytes.
        # Anything else (3xx redirect chains beyond `allow_redirects`,
        # 4xx access denied, 5xx server error) means this isn't a
        # plain media URL we can stream.
        if resp.status_code not in (200, 206):
            return None
        ct_raw = (resp.headers.get("Content-Type") or "").lower()
        ct = ct_raw.split(";", 1)[0].strip()
        if not ct.startswith(_DIRECT_OK_CONTENT_TYPES):
            return None
        # We pass the ORIGINAL URL through to the stream, NOT
        # `resp.url` (the post-redirect URL). Some CDNs 302 to
        # short-lived signed URLs whose tokens expire between the
        # probe and the actual stream GET; re-walking the redirect
        # chain at stream time always produces a fresh token. The
        # extra ~50ms round trip is invisible compared to the
        # streaming throughput.
        # Filesize: prefer Content-Range total (under Range request)
        # over Content-Length (which would be the partial size).
        cr = resp.headers.get("Content-Range") or ""
        filesize = None
        if "/" in cr:
            tail = cr.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                filesize = int(tail)
        if filesize is None:
            cl = resp.headers.get("Content-Length") or ""
            if cl.isdigit():
                filesize = int(cl)
    finally:
        try:
            resp.close()
        except Exception:
            pass

    # Reject tiny placeholder clips ("processing.mp4", etc.) that are
    # technically valid MP4s but not the requested content.
    if filesize is not None and filesize < 64 * 1024 and _looks_placeholder_media_url(url):
        return None

    # Filename from URL path. We don't have a real title here -
    # `_safe_name` in the caller normalises and adds the extension.
    title = path_lower.rsplit("/", 1)[-1].rsplit(".", 1)[0] or "video"
    _EXT_TO_FMT = {
        ".webm": ("webm", "video/webm"),
        ".mkv":  ("mkv",  "video/x-matroska"),
        ".avi":  ("avi",  "video/x-msvideo"),
        ".mov":  ("mov",  "video/quicktime"),
        ".m4v":  ("mp4",  "video/mp4"),
    }
    src_ext_key = "." + path_lower.rsplit(".", 1)[-1] if "." in path_lower else ""
    if src_ext_key in _EXT_TO_FMT:
        out_ext, mime_type = _EXT_TO_FMT[src_ext_key]
    else:
        out_ext = "mp4"
        mime_type = "video/mp4" if ct in _DIRECT_MP4_MIMES else (ct or "video/mp4")

    return {
        "method": "direct",
        "url": url,
        "headers": {"User-Agent": YDL_USER_AGENT},
        "ext": out_ext,
        "filesize": filesize,
        "title": title,
        "extractor": "direct-url",
        "mime_type": mime_type,
        "height": 0,
    }


# ---------------------------------------------------------------------------
# Tier 3: generic <video> tag scraper for sites yt-dlp doesn't recognise.
# Some news / blog / wiki / CMS pages just embed an MP4 with
# `<video src="...">` or `<source src="...">` and yt-dlp's Generic
# extractor either misses it or returns garbage. This scraper
# downloads the page HTML once, pulls out:
#   * `<video src=...>` / `<source src=...>` (HTML5 native player)
#   * `og:video` / `og:video:url` / `og:video:secure_url` meta
#   * `twitter:player:stream` meta
#   * JSON-LD `VideoObject.contentUrl` / `embedUrl`
#
# Then probes each candidate URL through the existing tier-0
# direct-URL fast path. First one that comes back as a streamable
# MP4 wins; we return its tier-0 dict shape.
#
# Cost discipline: this fires ONLY after yt-dlp has already failed
# (extract_info raised or returned no formats). Page-fetch is
# capped at ~256 KB so we don't yank a whole hostile site into
# memory.

_PAGE_SCRAPER_MAX_BYTES = 256 * 1024
_PAGE_SCRAPER_TIMEOUT_S = 8

# Patterns ordered most-reliable first. Each is anchored on the
# attribute name so we don't accidentally match comments.
_VIDEO_URL_PATTERNS: tuple[re.Pattern, ...] = (
    # OpenGraph - most reliable when present.
    re.compile(
        r'<meta\b[^>]*\bproperty\s*=\s*["\']og:video(?::secure_url|:url)?["\'][^>]*\bcontent\s*=\s*["\']([^"\']+\.mp4[^"\']*)',
        re.I,
    ),
    # OpenGraph with reversed attribute order.
    re.compile(
        r'<meta\b[^>]*\bcontent\s*=\s*["\']([^"\']+\.mp4[^"\']*)["\'][^>]*\bproperty\s*=\s*["\']og:video(?::secure_url|:url)?["\']',
        re.I,
    ),
    # Twitter player stream meta.
    re.compile(
        r'<meta\b[^>]*\bname\s*=\s*["\']twitter:player:stream["\'][^>]*\bcontent\s*=\s*["\']([^"\']+\.mp4[^"\']*)',
        re.I,
    ),
    # HTML5 <video src="...">. Some sites use src on <video>, others
    # on a child <source>; we match both.
    re.compile(r'<video\b[^>]*\bsrc\s*=\s*["\']([^"\']+\.mp4[^"\']*)', re.I),
    re.compile(r'<source\b[^>]*\bsrc\s*=\s*["\']([^"\']+\.mp4[^"\']*)', re.I),
    # JSON-LD `"contentUrl": "...mp4"` (loose match - we don't
    # parse the surrounding JSON, just the field).
    re.compile(r'"contentUrl"\s*:\s*"([^"]+\.mp4[^"]*)"', re.I),
    re.compile(r'"embedUrl"\s*:\s*"([^"]+\.mp4[^"]*)"', re.I),
)


def _resolve_streamable_page_scrape(url: str) -> Optional[dict]:
    """Tier 3 fast path: fetch the HTML, look for embedded MP4
    URLs in well-known meta / element shapes, and probe the
    first one that looks like a real video.

    Returns the same `method: direct` dict shape as tier 0/1
    so the caller doesn't need a separate dispatch branch.
    Returns None if the URL doesn't appear to be HTML, the page
    has no recognised video URL, or every candidate fails the
    tier-0 probe.
    """
    headers = {
        "User-Agent": YDL_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        # Cap server-side: ask for a partial body so a huge page
        # doesn't tank our memory budget. Most servers honour the
        # Range request and just send the first N bytes; the
        # ones that don't will get cut off below at the read loop.
        "Range": f"bytes=0-{_PAGE_SCRAPER_MAX_BYTES - 1}",
    }
    proxies = None
    try:
        p = _proxy_router.proxy_for_url(url)
        if p:
            proxies = {"http": p, "https": p}
    except Exception:
        log.exception("page-scrape: proxy_for_url() failed; falling back to direct")

    try:
        resp = requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=(STREAM_CONNECT_TIMEOUT_S, _PAGE_SCRAPER_TIMEOUT_S),
            allow_redirects=True,
            proxies=proxies,
        )
    except requests.RequestException as exc:
        log.info("page-scrape: fetch failed for %s: %s: %s",
                 _proxy_router._hostname_of(url),
                 type(exc).__name__,
                 _safe_log_msg(exc))
        return None

    try:
        if resp.status_code not in (200, 206):
            return None
        ct = (resp.headers.get("Content-Type") or "").lower()
        if "html" not in ct and "xml" not in ct:
            # Not a webpage - tier 0 already handled the direct
            # MP4 case, so anything that's not HTML here isn't
            # something we can scrape.
            return None
        # Read up to the cap. iter_content + manual byte counting
        # is more robust than .text on partial responses (which
        # can blow up on encoding detection of truncated UTF-8).
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=16 * 1024, decode_unicode=False):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= _PAGE_SCRAPER_MAX_BYTES:
                break
        body = b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        try:
            resp.close()
        except Exception:
            pass

    # Try each pattern; the first hit that resolves through the
    # tier-0 probe wins.
    seen: set[str] = set()
    for pat in _VIDEO_URL_PATTERNS:
        for m in pat.finditer(body):
            cand = (m.group(1) or "").strip()
            if not cand or cand in seen:
                continue
            seen.add(cand)
            # Resolve relative URLs against the page URL.
            try:
                from urllib.parse import urljoin
                resolved = urljoin(url, cand)
            except Exception:
                resolved = cand
            # Decode common HTML entities that sneak into JSON-LD.
            resolved = resolved.replace("&amp;", "&")
            if _looks_placeholder_media_url(resolved):
                continue
            # Probe via tier 0 to confirm it's actually a streamable
            # MP4 (and to fill in filesize / mime).
            tier0 = _resolve_streamable_direct_url(resolved)
            if tier0 is not None:
                tier0["extractor"] = "page-scrape"
                log.info("page-scrape: hit on %s -> %s",
                         _proxy_router._hostname_of(url),
                         resolved[:80])
                return tier0

    return None


def _resolve_streamable_format(url: str, fmt: str, quality: str) -> Optional[dict]:
    """Resolve `url` to a streamable format. Two tiers, fastest-first.

    Returns one of:
      Tier 1 - direct HTTP passthrough (lowest overhead):
        {
          "method": "direct",
          "url": str, "headers": dict, "ext": "mp4",
          "filesize": Optional[int], "title": str,
          "extractor": str, "mime_type": "video/mp4", "height": int,
        }
      Tier 2 - yt-dlp subprocess pipe (handles HLS / DASH / segmented /
      separated streams, anything yt-dlp can download):
        {
          "method": "ytdlp-pipe",
          "source_url": str, "target_height": int, "title": str,
          "extractor": str, "mime_type": "video/mp4",
          "filesize": None, "height": int,
        }
      None  (URL not streamable - caller falls back to /api/process slow path)

    Tier 1 eligibility (single-file HTTP MP4):
      * `fmt` must be `mp4`.
      * Chosen format must contain BOTH video and audio tracks
        (no separated DASH streams).
      * Protocol must be plain `http` / `https` (no HLS / DASH).
      * Container must be `mp4`.
      * Height must not exceed the requested quality cap.

    Tier 2 eligibility (yt-dlp pipe):
      * `fmt` must be `mp4`.
      * `extract_info` must succeed AND return at least one video
        format. Anything yt-dlp knows how to download qualifies -
        the actual pipe-stream uses `yt-dlp --output -` which
        handles HLS, DASH, separated-stream merging, fragment
        assembly, cookies, and per-domain proxy via the same code
        paths as the slow `/api/process` pipeline.

    Audio formats always return None (need ffmpeg post-encode -> slow path).
    """
    if fmt not in ALLOWED_VIDEO_FORMATS:
        return None
    if _stream_fastpath_blocked(url):
        log.info("streamable resolve: fast-path disabled for %s", _proxy_router._hostname_of(url))
        return None
    target_height = QUALITY_HEIGHTS.get(quality, MAX_VIDEO_HEIGHT)

    # ----- Tier 0: plain direct media URL (no yt-dlp at all) -----
    # If the URL path ends with a known video extension AND a
    # quick HEAD-style GET confirms `Content-Type: video/*`, we
    # don't need yt-dlp to extract anything - the URL itself IS
    # the streamable resource. yt-dlp's `Generic` extractor often
    # fills `vcodec`/`acodec` as `'none'` for direct URLs because
    # it doesn't probe file contents, which used to fail both
    # tier-1 and tier-2's codec checks and bounce to 422 even
    # for trivially-streamable links like a sample MP4 hosted on
    # a static CDN. Mirrors the slow path's `_direct_url_fast_path`
    # which has been doing this since 0.4.x.
    direct = _resolve_streamable_direct_url(url)
    if direct is not None:
        return direct

    info_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        # Per-host: 30s for slow-frontend domains (IG), 15s elsewhere.
        "socket_timeout": _socket_timeout_for(url, default=15),
        "retries": int(_domain_policy_for(url).get("ydl_retries", 1)),
        "extractor_args": YDL_EXTRACTOR_ARGS,
        "http_headers": {"User-Agent": YDL_USER_AGENT},
    }
    cf = _request_cookiefile_for_url(url)
    if cf:
        info_opts["cookiefile"] = cf
    try:
        info = _extract_info_with_retry_matrix(url, info_opts, purpose="streamable-resolve")
    except Exception as exc:
        # Logged at INFO (not exception) - extractor failure for
        # an unsupported / dead URL is a normal outcome on the
        # public mini. Don't spam tracebacks for those.
        log.info(
            "streamable resolve: extract_info failed for %s: %s: %s",
            _proxy_router._hostname_of(url),
            type(exc).__name__,
            _safe_log_msg(exc),
        )
        # Tier 3: page scrape. Last shot before falling through
        # to the slow path. If the page literally embeds an MP4
        # in `<video>` / `og:video` / JSON-LD we can still serve
        # it without yt-dlp recognising the site at all.
        scraped = _resolve_streamable_page_scrape(url)
        if scraped is not None:
            return scraped
        return None
    if info is None:
        log.info("streamable resolve: extract_info returned None for %s",
                 _proxy_router._hostname_of(url))
        scraped = _resolve_streamable_page_scrape(url)
        if scraped is not None:
            return scraped
        return None

    title = (info.get("title") or "Untitled")[:200]
    extractor = info.get("extractor_key")
    formats = info.get("formats") or []

    # ----- Tier 1: single-file HTTP MP4 (fastest path) -----
    # Codec-field semantics in yt-dlp:
    #   vcodec=h264, acodec=aac : muxed video+audio (what we want)
    #   vcodec=h264, acodec=none: video-only, needs separate
    #                             audio merge -> reject (tier 2)
    #   vcodec=none, acodec=aac : audio-only -> reject (mp4 mode
    #                             needs video; tier 2 won't help)
    #   vcodec=none, acodec=none: unknown - usually means yt-dlp's
    #                             Generic extractor returned the
    #                             URL without probing file content.
    #                             For an .mp4 ext it's almost
    #                             always actually muxed, so we
    #                             accept. The previous logic
    #                             rejected this case and dropped
    #                             a whole class of streamable URLs
    #                             into the slow-path 422 fallback.
    direct_candidates = []
    for f in formats:
        if not isinstance(f, dict):
            continue
        if not f.get("url"):
            continue
        if f.get("fragments"):
            continue
        v = f.get("vcodec") or "none"
        a = f.get("acodec") or "none"
        if v == "none" and a != "none":
            continue  # audio-only
        if v != "none" and a == "none":
            continue  # video-only (needs audio merge -> tier 2)
        proto = (f.get("protocol") or "").lower()
        if proto not in ("https", "http"):
            continue
        # Only direct-passthrough when source container matches request.
        # MOV and MP4 are ISOBMFF siblings; MKV accepts any single-file
        # source since it just re-wraps the streams.
        _TIER1_EXTS: dict[str, set[str]] = {
            "mp4":  {"mp4"},
            "webm": {"webm"},
            "mov":  {"mp4", "m4v", "mov"},
            "mkv":  {"mp4", "webm", "mkv", "m4v"},
            "avi":  {"avi"},
        }
        if f.get("ext") not in _TIER1_EXTS.get(fmt, {"mp4"}):
            continue
        height = f.get("height") or 0
        if height > target_height:
            continue
        direct_candidates.append(f)

    _host = _proxy_router._hostname_of(url)
    _pipe_required = any(
        _host == d or _host.endswith("." + d)
        for d in _YTDLP_PIPE_REQUIRED_DOMAINS
    )
    if direct_candidates and not _pipe_required:
        direct_candidates.sort(
            key=lambda f: (
                f.get("height") or 0,
                f.get("tbr") or 0,
                f.get("filesize") or f.get("filesize_approx") or 0,
            ),
            reverse=True,
        )
        best = direct_candidates[0]
        headers = dict(best.get("http_headers") or {})
        headers.setdefault("User-Agent", YDL_USER_AGENT)
        return {
            "method": "direct",
            "url": best["url"],
            "headers": headers,
            "ext": fmt if fmt in ALLOWED_VIDEO_FORMATS else "mp4",
            "filesize": best.get("filesize") or best.get("filesize_approx"),
            "title": title,
            "extractor": extractor,
            "mime_type": _VIDEO_MEDIA_TYPES.get(fmt, "video/mp4"),
            "height": best.get("height") or 0,
        }

    # ----- Tier 2: yt-dlp subprocess pipe -----
    # Anything yt-dlp can download qualifies. We don't pick a
    # specific format here - yt-dlp's own format selector in the
    # subprocess invocation handles that. The check is "did
    # extract_info return any format with a URL?" - if yes, the
    # subprocess will be able to produce SOMETHING.
    #
    # We deliberately DON'T filter on `vcodec != 'none'` here.
    # yt-dlp's `Generic` extractor (which a lot of url_transparent
    # extractors delegate to - ThisVid, RedTube, etc.) often
    # returns `vcodec='none'`/`acodec='none'` simply because it
    # didn't probe the file content for codec info, NOT because
    # there's no video. Filtering on those fields silently dropped
    # entire classes of valid streamable URLs to 422-fallback,
    # which is exactly the symptom that prompted this commit.
    has_format = any(
        isinstance(f, dict) and f.get("url")
        for f in formats
    )
    if not has_format:
        log.info(
            "streamable resolve: extract_info returned %d formats but none with URLs for %s",
            len(formats),
            _proxy_router._hostname_of(url),
        )
        # yt-dlp recognised the site but couldn't pull a usable
        # format (auth-walled / DRM / parser regression). Try
        # tier 3 (HTML page scrape) as a last shot before letting
        # the caller fall through to the slow path.
        scraped = _resolve_streamable_page_scrape(url)
        if scraped is not None:
            return scraped
        return None

    return {
        "method": "ytdlp-pipe",
        "source_url": url,
        "target_height": target_height,
        "target_fmt": fmt,
        "title": title,
        "extractor": extractor,
        "mime_type": _VIDEO_MEDIA_TYPES.get(fmt, "video/mp4"),
        "filesize": None,
        "height": target_height,  # cap; actual could be lower
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
    # Optional client-supplied YouTube cookie session token,
    # minted earlier via POST /api/yt-cookies. Lets the user
    # bring their own browser cookies to bypass YouTube's
    # datacenter-IP bot challenge. Server-side validated for
    # length/charset before lookup. Empty/None = anonymous.
    yt_session: Optional[str] = Field(default=None, max_length=64)


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
            "max_filesize_mb": MAX_DOWNLOAD_FILESIZE_BYTES // (1024 * 1024),
            "max_duration_min": MAX_DOWNLOAD_DURATION_SECONDS // 60,
            "qualities": sorted(QUALITY_HEIGHTS.keys()),
            "default_quality": DEFAULT_QUALITY,
            "quality_heights": dict(QUALITY_HEIGHTS),
            "audio_formats": sorted(ALLOWED_AUDIO_FORMATS),
            "default_audio_format": DEFAULT_AUDIO_FORMAT,
            "lossless_audio_formats": sorted(LOSSLESS_AUDIO_FORMATS),
            "audio_bitrate_kbps": AUDIO_BITRATE_KBPS,
            "rate_limit": RATE_LIMIT_PER_HOUR,
        },
    )


@app.get("/healthz", include_in_schema=False)
@limiter.limit("60/minute")
async def healthz(request: Request):
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

# ---------------------------------------------------------------------------
# Streaming fast-path knobs. The direct-stream endpoints
# (`/api/stream-download` + `/api/stream/{token}`) bypass the server-pull
# pipeline entirely: yt-dlp resolves the upstream URL, the server opens
# a streaming HTTP request, and bytes flow straight through to the
# browser. No disk usage, no filesize cap (server isn't holding the
# file), no total-time timeout (only an upstream-stall liveness check).
#
# Tokens are one-shot and short-lived: the POST endpoint mints a
# token containing the resolved URL + headers, the matching GET
# endpoint pops the token and starts streaming. Tokens stop being
# valid 5 min after issue OR after first GET, whichever's first.
# ---------------------------------------------------------------------------
STREAM_TOKEN_TTL_S = int(os.environ.get("CMVIDEO_STREAM_TOKEN_TTL_S", "300"))
# Per-chunk read timeout. If upstream stops sending bytes for this
# long the stream is killed. NOT a total-time cap - a multi-hour
# download is fine as long as bytes keep flowing.
STREAM_READ_TIMEOUT_S = int(os.environ.get("CMVIDEO_STREAM_READ_TIMEOUT_S", "60"))
STREAM_CONNECT_TIMEOUT_S = int(os.environ.get("CMVIDEO_STREAM_CONNECT_TIMEOUT_S", "30"))
# Concurrent streams per IP. Streams are cheap (no CPU - just
# byte-shuffling) so this can be more generous than JOB_MAX_PER_IP.
# Bumped from 3 -> 5 in mini-2026.05.16.4-alpha to let one client
# parallelise multiple downloads.
STREAM_MAX_PER_IP = int(os.environ.get("CMVIDEO_STREAM_MAX_PER_IP", "5"))
# Chunk size for the iter_content / yield loop. 512 KB strikes a
# good balance: fewer Python-level yields per second (lower CPU
# overhead at saturation), still small enough that the per-chunk
# read timeout catches stalls quickly. Bumped from 128 KB ->
# 512 KB in mini-2026.05.16.4-alpha. Env-tunable so the operator
# can dial up/down without a redeploy if upstream behaviour
# changes (huge chunks improve raw throughput on fast pipes,
# small chunks reduce stall-detection latency on flaky ones).
STREAM_CHUNK_BYTES = int(os.environ.get("CMVIDEO_STREAM_CHUNK_KB", "512")) * 1024


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
        # slots and locking everyone else out. Owner-IP allowlist
        # skips this check (still subject to the global cap above).
        if not _is_owner_ip(client_ip):
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
    """Look a job up by id.

    Access control: the job_id itself is a 32-byte unpredictable
    token (see _create_job). At ~10**77 possibilities even an
    adversary checking 10**9 ids/second would need 10**60 years
    of guessing - the token IS the security boundary.

    Earlier versions of this function ALSO required `client_ip`
    to match the creator's, returning a deceptive 404 on
    mismatch. That looked like belt-and-braces in code review
    but in practice was load-bearing for nothing while breaking
    every legitimate mobile user whose CGNAT egress shifts
    between requests:

      * Mobile carrier CGNAT pools rotate egress IPs across
        successive HTTP requests (LTE/5G handoff, NAT timeout,
        carrier load-balancing) - sometimes within seconds of
        each other.
      * The frontend polls `/api/jobs/{id}` every 700ms. With
        IP-scope ON, ONE shifted egress mid-poll = 404 to the
        client, mapped to "mini service is offline" by
        site/app.js mapHttpError.
      * Reproduced live on the user's iPhone: thisvid URL
        submitted, polling failed, offline message shown,
        even though /healthz was 200 and the submit had
        returned a valid job_id.

    `client_ip` is now accepted for logging/forensics only and
    never blocks the request. If you ever need a real ownership
    boundary on top of the token (e.g. cookies / signed URLs /
    HMAC nonces), add it here as an explicit second factor -
    don't bring back IP-equality, it's broken by design on
    mobile and CGNAT networks."""
    with _jobs_lock:
        j = _jobs.get(job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="That job has expired or never existed.")
    if client_ip is not None and j._client_ip and j._client_ip != client_ip:
        # Soft signal only: log the IP shift but DO NOT block.
        # Useful as a forensics breadcrumb if a leaked-token
        # incident ever materialises in the wild.
        log.info(
            "job %s: client IP shifted %s -> %s (allowed; CGNAT/handoff is normal on mobile)",
            job_id[:8], j._client_ip, client_ip,
        )
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
                # When one extractor gives up and we fall through to
                # the next, *step the bar forward* by a few points
                # rather than resetting backwards. The user shouldn't
                # see "5%" parked motionless while we cycle through 3
                # backup tools - they should see steady climb that
                # signals "the system is doing things". Cap at 60 so
                # there's still room for the actual download progress
                # hook to take over once a tool starts succeeding.
                step = 5 if tool == "yt-dlp" else 8
                _set_stage(job, "fetching", min(60, max(job.pct, 5) + step))

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
                    job.error = _friendly_ydl_error(e, clean_url)
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
                log.error("pipeline RuntimeError: %s", e)
                job.error = "Processing failed. Try a different clip or use the desktop app."
                job.stage = "error"
                return
            _set_stage(job, "rendering", 100)

        # Final filesize gate (catches outputs that grew during re-encode).
        # `active_cap` MUST mirror the per-quality cap that was passed
        # in to `_do_download` (see ~1320). Using the legacy
        # `MAX_DOWNLOAD_FILESIZE_BYTES` constant - which always points
        # at the *standard* tier (800 MB) - bites HD jobs: yt-dlp is
        # told it can pull up to 1500 MB, succeeds with a legitimate
        # ~1.2 GB 1080p file, then we reject it here with an
        # "exceeds the 800 MB cap" message that's wrong for the
        # quality tier the user picked.
        size = out_path.stat().st_size
        active_cap = (
            QUALITY_DOWNLOAD_CAPS.get(quality_choice, MAX_DOWNLOAD_FILESIZE_BYTES)
            if md == "download"
            else MAX_CENSOR_FILESIZE_BYTES
        )
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
    # Owner IPs don't accrue against the cooldown counter so the
    # maintainer's own failed test pulls don't gradually lock them
    # out. _check_cooldown also short-circuits on owner anyway, but
    # this keeps the deque lean.
    if _is_owner_ip(ip):
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
    # Owner-IP allowlist: don't ever cool the maintainer down for
    # their own testing failures. Doesn't bypass JOB_MAX_INFLIGHT or
    # the killswitch, just the per-IP failure window.
    if _is_owner_ip(ip):
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
    # User cookies bypass cache - the per-cookie response could
    # differ (auth-walled videos vs. public). Anonymous lookups
    # still hit the shared cache.
    cf = _resolve_cookiefile(body.yt_session, _client_ip(request))
    if not body.yt_session:
        cached = _info_cache_get(url)
        if cached is not None:
            return JSONResponse(cached)
    try:
        # Set the per-request cookiefile context BEFORE the
        # threadpool call. ContextVars propagate across
        # `asyncio.to_thread` so `_do_info` reads it.
        token = _request_cookiefile.set(cf)
        try:
            info = await asyncio.wait_for(
                asyncio.to_thread(_do_info, url),
                timeout=_info_resolve_budget_s(url),
            )
        finally:
            _request_cookiefile.reset(token)
    except asyncio.TimeoutError:
        _record_failure(_client_ip(request))
        raise HTTPException(status_code=504, detail="Source took too long to respond.")
    except yt_dlp.utils.DownloadError as e:
        _record_failure(_client_ip(request))
        raise HTTPException(status_code=400, detail=_friendly_ydl_error(e, url))
    except HTTPException:
        raise
    except Exception:
        # Don't leak the raw exception string - it can include paths
        # or internal state. Server logs keep the full traceback.
        log.exception("info failed")
        _record_failure(_client_ip(request))
        raise HTTPException(status_code=400, detail="Couldn't read that URL.")
    # Only cache anonymous requests - per-user-cookie responses
    # can include private / unlisted videos that we MUST NOT
    # leak to the next anonymous visitor. Same key would
    # otherwise serve the wrong content.
    if not body.yt_session:
        _info_cache_put(url, info)
    return JSONResponse(info)


# ---------------------------------------------------------------------------
# YouTube cookie upload endpoint
# ---------------------------------------------------------------------------
class YTCookiesRequest(BaseModel):
    cookies_txt: str = Field(..., min_length=10, max_length=YT_COOKIES_MAX_BYTES)


@app.post("/api/yt-cookies")
@limiter.limit("10/hour;3/minute")
async def api_yt_cookies(request: Request, body: YTCookiesRequest):
    """Accept a Netscape cookies.txt blob from the client, materialize
    it server-side under a random session token, return the token.

    The token can then be passed in the `yt_session` field on
    /api/info, /api/process, /api/stream-download to apply those
    cookies to that single user's yt-dlp calls. Cookies live in
    process memory (tempfile in /tmp, 0600) and are automatically
    purged after `YT_SESSION_TTL_S` (default 30 minutes).

    Security:
      * The token is bound to the uploading client IP. Replay
        from a different IP is rejected (allowlisted owner IPs
        excepted).
      * Cookie content is never echoed in any response. Only the
        opaque token is returned.
      * Token is `secrets.token_urlsafe(24)` - 192 bits of
        entropy, not guessable.
      * No persistence: tempfile is unlinked on TTL expiry. We
        do NOT write anything to a database or log.
      * Rate-limited at 10/hour to bound abuse + memory growth.
    """
    _enforce_killswitch()
    _enforce_ua(request)
    _check_cooldown(_client_ip(request))

    client_ip = _client_ip(request)
    try:
        token, n_yt_cookies = _yt_session_create(body.cookies_txt, client_ip)
    except ValueError as exc:
        # Validation error - raw message is safe (no cookie
        # bytes echoed back, our validator never includes user
        # data in its error strings).
        _record_failure(client_ip)
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "ok": True,
        # n_yt_cookies is the count of YT-domain cookies the user
        # uploaded; useful for the UI to confirm "we saw 8 cookies".
        # NEVER includes the cookie names/values themselves.
        "n_yt_cookies": n_yt_cookies,
        "yt_session": token,
        "expires_in": YT_SESSION_TTL_S,
    }


@app.delete("/api/yt-cookies/{token}")
@limiter.limit("20/hour;5/minute")
async def api_yt_cookies_delete(token: str, request: Request):
    """Manually revoke a cookie session before its TTL expires.

    Token-IP binding still applies: a stranger who somehow has
    the token cannot use it to delete the session unless they're
    on the same IP."""
    _enforce_killswitch()
    if not token or len(token) > 64:
        raise HTTPException(status_code=400, detail="Bad token.")
    client_ip = _client_ip(request)
    with _yt_sessions_lock:
        entry = _yt_sessions.get(token)
        if entry is None:
            return {"ok": True, "revoked": False}
        path, owner_ip, _ = entry
        if owner_ip != client_ip and not _is_owner_ip(client_ip):
            # Don't reveal whether the token exists for a different IP.
            return {"ok": True, "revoked": False}
        _yt_sessions.pop(token, None)
    try:
        os.unlink(path)
    except OSError:
        pass
    return {"ok": True, "revoked": True}


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
    yt_session: Optional[str] = Form(None),
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
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Format must be one of: {', '.join(sorted(ALLOWED_FORMATS))}",
        )
    if md not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail="Mode must be 'download', 'silence', or 'beep'.")
    if fps_choice not in ALLOWED_FPS:
        raise HTTPException(status_code=400, detail="FPS must be 'source', '30', or '60'.")
    quality_choice = _normalize_quality(quality)
    # FPS / video-quality are video-only; coerce sane defaults for
    # audio formats so the pipeline doesn't try to set fps on an
    # mp3.
    if fmt not in ALLOWED_VIDEO_FORMATS:
        fps_choice = "source"
        quality_choice = DEFAULT_QUALITY  # height is meaningless for audio
    # >=1080p + fps override = libx264 ultrafast at 1080p+ on 2 shared
    # vCPUs, which runs at ~5x slower than realtime. An 8-min censor
    # job would hit the ffmpeg timeout. Refuse the combo with a clear
    # message instead of silently coercing - users who specifically
    # picked 60fps deserve to know why we're not honoring it.
    if QUALITY_HEIGHTS[quality_choice] >= 1080 and fps_choice in {"30", "60"}:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{quality_choice} + fps override is too slow on the mini's shared CPU. "
                "Pick 720p or smaller for 30/60 fps, or higher quality with Source fps. "
                "The desktop app handles both at any combination."
            ),
        )
    # Censoring (silence/beep) is CPU-heavy enough that picking
    # >1080p makes the ffmpeg encode pass blow the 240s censor
    # timeout. Reject up front instead of failing mid-job.
    if md != "download" and QUALITY_HEIGHTS[quality_choice] > 1080:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Censoring at {quality_choice} is too slow on the mini's shared CPU. "
                "Pick 1080p or smaller for silence/beep modes, or use the desktop app."
            ),
        )

    have_url = bool(url and url.strip())
    have_file = file is not None and bool(file.filename)
    if have_url == have_file:
        raise HTTPException(status_code=400, detail="Provide either a URL or a file, not both / neither.")

    if md == "download" and have_file:
        raise HTTPException(status_code=400, detail="Download mode is URL-only - you already have the file locally.")

    # Resolve the cookiefile for this request once and stash it
    # on a ContextVar so all downstream yt-dlp calls pick it up.
    # ContextVars are propagated:
    #   * Across `asyncio.to_thread` calls (used by the
    #     synchronous slow path below).
    #   * Into `asyncio.create_task` children at task-creation
    #     time (the async branch). The reset() that fires on the
    #     parent's return doesn't affect the already-snapshotted
    #     child context, so the background pipeline keeps the
    #     cookiefile until it's done.
    process_cf = _resolve_cookiefile(yt_session, _client_ip(request))
    process_cf_token = _request_cookiefile.set(process_cf)

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
            # Pick the right cap by output type: video uses
            # quality-tier caps, audio uses per-format caps
            # (lossless can be much larger than 320 kbps mp3).
            if fmt == "mp4":
                quality_cap = QUALITY_DOWNLOAD_CAPS.get(quality_choice, MAX_DOWNLOAD_FILESIZE_BYTES)
            else:
                quality_cap = AUDIO_DOWNLOAD_CAPS.get(fmt, MAX_DOWNLOAD_FILESIZE_BYTES)
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
                raise HTTPException(status_code=400, detail=_friendly_ydl_error(e, clean_url))
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
                log.error("pipeline RuntimeError (sync path): %s", e)
                raise HTTPException(
                    status_code=400,
                    detail="Processing failed. Try a different clip or use the desktop app.",
                )

        # 3) Final filesize gate (catches outputs that grew during re-encode).
        # See _run_pipeline_async for why active_cap is per-quality:
        # the legacy MAX_DOWNLOAD_FILESIZE_BYTES is the standard
        # (800 MB) tier and rejects legitimate HD pulls.
        size = out_path.stat().st_size
        active_cap = (
            QUALITY_DOWNLOAD_CAPS.get(quality_choice, MAX_DOWNLOAD_FILESIZE_BYTES)
            if md == "download"
            else MAX_CENSOR_FILESIZE_BYTES
        )
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


class DownloadCompatRequest(BaseModel):
    """Body for the JSON-shape /api/download shim used by the
    bundled HF Space page's static/app.js."""

    url: str = Field(..., min_length=8, max_length=2048)
    format: str = Field(..., min_length=1, max_length=8)
    quality: str = Field(default=DEFAULT_QUALITY, min_length=1, max_length=16)
    yt_session: Optional[str] = Field(default=None, max_length=64)


@app.post("/api/download", include_in_schema=False)
@limiter.limit(RATE_LIMIT_PER_HOUR)
async def api_download_compat(request: Request, body: DownloadCompatRequest):
    """Back-compat shim for clients that POST JSON to /api/download
    (notably the mini-app's own bundled `static/app.js`). Delegates
    to /api/process with mode=download.

    Two bugs lived here as of v0.4.16-alpha and earlier:
      1. The signature used `format: str = Form(...)`,
         `url: str = Form(...)` while the docstring (and the only
         known in-tree caller, static/app.js) sent JSON. So every
         request from the bundled HF Space page got 422 from
         FastAPI's body validator before reaching any handler code.
      2. When that 422 was bypassed, the delegating call below
         passed positional args but left `fps` and `quality`
         defaulted. FastAPI's `Form(...)` defaults are sentinel
         objects (not strings) when the function is called directly
         (i.e. not through the request parser), so api_process'
         very first line `(fps or "source").lower()` crashed with
         AttributeError.
    Fixed both: pydantic body for the on-wire shape, and explicit
    string defaults for fps/quality so the direct call doesn't
    inherit Form sentinels."""
    return await api_process(
        request=request,
        format=body.format,
        mode="download",
        url=body.url,
        file=None,
        fps="source",
        quality=body.quality or DEFAULT_QUALITY,
        yt_session=body.yt_session,
    )


# ---------------------------------------------------------------------------
# Streaming fast-path: server-as-passthrough-proxy.
#
# Why this exists:
#   The server-pull pipeline (/api/process) downloads the entire
#   source to disk, then serves the file. That works for censoring
#   (where we have to muxer-touch every byte anyway), but for raw
#   downloads it's pure waste: the server pays disk I/O, the
#   DOWNLOAD_TIMEOUT_SECONDS=360 cap kicks in on long videos before
#   any byte reaches the user, and total bandwidth gets spent twice
#   (once upstream -> server, once server -> client).
#
#   The fast-path skips the disk: yt-dlp resolves the upstream URL,
#   the server opens a streaming HTTP connection, and bytes flow
#   straight through to the browser. No filesize cap. No total-time
#   timeout. Just a per-chunk read-timeout liveness check so a hung
#   upstream doesn't pin the slot forever.
#
# Two-step flow (init + serve) instead of a single GET because:
#   * The init endpoint runs the full request validation /
#     killswitch / cooldown / rate-limit gate. The user's URL never
#     reaches the actual stream endpoint as a query param.
#   * Tokens are short-lived AND one-shot: even if a token leaks
#     it's worthless after first use or 5 min, whichever comes first.
#   * Frontend can detect a "needs_processing" 422 and gracefully
#     fall back to /api/process - whereas a direct GET that 422'd
#     would just navigate the user to an error page.
# ---------------------------------------------------------------------------
_stream_tokens: dict = {}              # token -> (resolved, ip, expires_at)
_stream_tokens_lock = threading.Lock()
_active_streams: dict = {}             # ip -> count of in-flight streams
_active_streams_lock = threading.Lock()


def _purge_expired_stream_tokens() -> None:
    """Drop tokens whose TTL has elapsed. Called opportunistically
    inside the issue + redeem paths so we never need a background
    sweeper. With STREAM_TOKEN_TTL_S=300 and one-shot redemption,
    the dict effectively self-trims."""
    now = time.time()
    with _stream_tokens_lock:
        dead = [t for t, v in _stream_tokens.items() if v[2] <= now]
        for t in dead:
            _stream_tokens.pop(t, None)


class StreamDownloadRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    format: str = Field(..., min_length=1, max_length=8)
    quality: str = Field(default=DEFAULT_QUALITY, min_length=1, max_length=16)
    yt_session: Optional[str] = Field(default=None, max_length=64)


@app.post("/api/stream-download")
@limiter.limit(f"{RATE_LIMIT_PER_HOUR};5/minute")
async def api_stream_download_init(request: Request, body: StreamDownloadRequest):
    """Probe whether `body.url` can be served via the streaming
    fast-path; if yes, mint a one-shot token the browser can GET.

    Returns:
      200 `{stream_url, filename, filesize, height}` if eligible.
        The browser should navigate to `stream_url` (a relative
        path on this same Space) to start the download.
      422 `{reason: "needs_processing", fallback: "/api/process"}`
        if the URL needs server-side merging / re-encoding /
        fragment assembly. The frontend should fall back to the
        regular async pipeline.
      Standard 4xx/5xx for validation + abuse gates.
    """
    _enforce_killswitch()
    _enforce_ua(request)
    _check_cooldown(_client_ip(request))

    fmt = (body.format or "").lower().strip()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Format must be one of: {', '.join(sorted(ALLOWED_FORMATS))}",
        )
    quality_choice = _normalize_quality(body.quality)
    # Fast-path handles video containers (mp4, webm). Audio formats
    # always need the FFmpegExtractAudio postprocessor → slow path.
    if fmt not in ALLOWED_VIDEO_FORMATS:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "needs_processing",
                "fallback": "/api/process",
                "message": "Audio formats need server-side encoding. Use /api/process.",
            },
        )

    clean_url = _validate_url(body.url)

    cf = _resolve_cookiefile(body.yt_session, _client_ip(request))
    try:
        token_ctx = _request_cookiefile.set(cf)
        try:
            resolved = await asyncio.wait_for(
                asyncio.to_thread(_resolve_streamable_format, clean_url, fmt, quality_choice),
                timeout=_info_resolve_budget_s(clean_url),
            )
        finally:
            _request_cookiefile.reset(token_ctx)
    except asyncio.TimeoutError:
        _record_failure(_client_ip(request))
        raise HTTPException(status_code=504, detail="Source took too long to respond.")
    except Exception:
        # Any extractor exception -> not eligible. Don't leak details.
        log.exception("stream-download resolve failed")
        resolved = None

    if not resolved:
        # Frontend's cue to retry against /api/process. NOT a
        # failure - this is the expected branch for HLS / DASH /
        # MP3 / sites with separated streams. We deliberately use
        # 422 (Unprocessable Entity) rather than 200 with a flag
        # so curl-style clients without the fallback logic still
        # see a non-success status.
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "needs_processing",
                "fallback": "/api/process",
                "message": "This URL needs server-side merging or re-encoding. Use /api/process.",
            },
        )

    token = secrets.token_urlsafe(24)
    expires_at = time.time() + STREAM_TOKEN_TTL_S
    filename = _safe_name(resolved.get("title") or "cmvideo", fmt)
    resolved["filename"] = filename
    resolved["client_ip"] = _client_ip(request)
    # Carry the cookiefile path through to the eventual GET
    # request that actually fires the yt-dlp subprocess pipe.
    # The GET arrives in a different request context (separate
    # ContextVar), so we stash it on the resolved dict and the
    # pipe code re-installs it when it builds its own context.
    if cf:
        resolved["_cookiefile"] = cf

    _purge_expired_stream_tokens()
    with _stream_tokens_lock:
        _stream_tokens[token] = (resolved, _client_ip(request), expires_at)

    return {
        "stream_url": f"/api/stream/{token}",
        "filename": filename,
        "filesize": resolved.get("filesize"),
        "height": resolved.get("height", 0),
        "expires_in": STREAM_TOKEN_TTL_S,
    }


@app.get("/api/stream/{token}", include_in_schema=False)
@limiter.limit("60/minute")
async def api_stream_serve(token: str, request: Request):
    """Pop the token, dispatch to the streaming method recorded at
    issue time. NO filesize cap, NO total-time timeout in either
    branch - only liveness checks (per-chunk read-timeout for
    direct, subprocess-alive for ytdlp-pipe).

    The browser's native download bar handles progress; we pass
    `Content-Length` through for the direct branch when upstream
    advertises one (the pipe branch can't know in advance)."""
    _purge_expired_stream_tokens()
    with _stream_tokens_lock:
        # One-shot: pop. Even if the user retries the same URL,
        # they'll get a fresh token and the leaked one is dead.
        data = _stream_tokens.pop(token, None)
    if data is None:
        raise HTTPException(status_code=404, detail="Download link expired or already used. Click Download again.")
    resolved, owner_ip, expires_at = data
    if time.time() > expires_at:
        raise HTTPException(status_code=410, detail="Download link expired. Click Download again to retry.")

    # NB: we deliberately do NOT enforce `owner_ip == _client_ip(request)`.
    # Mobile clients on CGNAT shift IPs between requests; the
    # `_get_job` IP-scope check used to break legitimate users
    # for exactly this reason (see CHANGELOG `0.4.16.3-alpha`).
    # The token itself is the capability; one-shot use + 5 min
    # TTL is the security boundary.

    ip = _client_ip(request)
    with _active_streams_lock:
        if _active_streams.get(ip, 0) >= STREAM_MAX_PER_IP:
            raise HTTPException(
                status_code=429,
                detail=f"You have {STREAM_MAX_PER_IP} streams in flight already. Wait for one to finish.",
            )
        _active_streams[ip] = _active_streams.get(ip, 0) + 1

    def release_slot() -> None:
        with _active_streams_lock:
            n = _active_streams.get(ip, 0) - 1
            if n <= 0:
                _active_streams.pop(ip, None)
            else:
                _active_streams[ip] = n

    method = resolved.get("method", "direct")
    if method == "direct":
        return _serve_stream_direct(resolved, release_slot)
    elif method == "ytdlp-pipe":
        return _serve_stream_ytdlp_pipe(resolved, release_slot)
    else:
        release_slot()
        raise HTTPException(status_code=500, detail=f"Unknown stream method: {method}")


def _serve_stream_direct(resolved: dict, release_slot) -> StreamingResponse:
    """Tier-1 streaming: open a single HTTP GET and pipe the bytes
    straight through. Lowest overhead - no subprocess, no remux,
    just a TCP relay. Filesize cap and total-time cap are absent;
    a per-chunk read-timeout (`STREAM_READ_TIMEOUT_S`) catches
    upstreams that go silent."""
    upstream_url = resolved["url"]
    headers = dict(resolved.get("headers") or {})
    headers.setdefault("User-Agent", YDL_USER_AGENT)
    # Per-domain residential proxy routing - matches the rest of
    # the pipeline (see proxy_router.py). Falls back to direct if
    # no proxy is configured for this domain.
    proxies = None
    try:
        proxy_url = _proxy_router.proxy_for_url(upstream_url)
        if proxy_url:
            proxies = {"http": proxy_url, "https": proxy_url}
    except Exception:
        log.exception("stream proxy_for_url() failed; falling back to direct")
        proxies = None

    try:
        upstream = requests.get(
            upstream_url,
            headers=headers,
            stream=True,
            timeout=(STREAM_CONNECT_TIMEOUT_S, STREAM_READ_TIMEOUT_S),
            allow_redirects=True,
            proxies=proxies,
        )
        upstream.raise_for_status()
    except requests.RequestException as exc:
        release_slot()
        log.warning("stream upstream connect failed: %s", exc)
        raise HTTPException(status_code=502, detail="Couldn't reach the source server. Try again or use the desktop app.")

    response_headers = {
        "Content-Disposition": f'attachment; filename="{resolved["filename"]}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    upstream_len = upstream.headers.get("Content-Length")
    if upstream_len and upstream_len.isdigit():
        response_headers["Content-Length"] = upstream_len

    media_type = resolved.get("mime_type") or "application/octet-stream"

    def gen():
        try:
            for chunk in upstream.iter_content(chunk_size=STREAM_CHUNK_BYTES):
                if chunk:
                    yield chunk
        except (requests.RequestException, GeneratorExit):
            pass
        except Exception:
            log.exception("stream pipe failed mid-flight")
        finally:
            try:
                upstream.close()
            except Exception:
                pass
            release_slot()

    return StreamingResponse(gen(), media_type=media_type, headers=response_headers)


def _serve_stream_ytdlp_pipe(resolved: dict, release_slot) -> StreamingResponse:
    """Tier-2 streaming: spawn `python -m yt_dlp --output -` as a
    subprocess and pipe its stdout straight to the client. This is
    what makes HLS / DASH / segmented / separated-stream sources
    work without disk buffering: yt-dlp emits the muxed MP4
    incrementally, the client receives bytes as they're produced,
    no `/tmp` involvement, no 360s wall-clock cap.

    yt-dlp is a much heavier subprocess than a single TCP relay
    (Python startup + extractor + maybe ffmpeg for merging), but
    it inherits ALL the extractor and proxy logic from the slow
    path - cookies, headers, residential proxy routing, retries,
    every site yt-dlp supports. The trade-off is worth it for the
    URLs that tier-1 can't catch."""
    import sys
    import subprocess

    source_url = resolved["source_url"]
    target_height = int(resolved.get("target_height") or MAX_VIDEO_HEIGHT)
    target_fmt = resolved.get("target_fmt", "mp4")

    # Pick the right format ladder and merge container.
    if target_fmt == "webm":
        fmt_selector = _webm_format_fallback_ladder("source", height=target_height)
    elif target_fmt == "mkv":
        fmt_selector = _mkv_format_fallback_ladder("source", height=target_height)
    else:
        # mp4, mov, avi — all prefer AVC+AAC, differ only in container
        fmt_selector = _video_format_fallback_ladder("source", height=target_height)
    merge_fmt = target_fmt if target_fmt in ("mp4", "webm", "mkv", "mov", "avi") else "mp4"

    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--quiet", "--no-warnings", "--no-progress", "--no-call-home",
        "--no-mtime", "--no-part",
        "--format", fmt_selector,
        "--merge-output-format", merge_fmt,
        "--output", "-",
        "--user-agent", YDL_USER_AGENT,
        "--socket-timeout", "30",
        "--retries", "3",
    ]
    # Cookiefile resolution: the per-request ContextVar isn't
    # reliable here because the GET /api/stream/{token} arrives
    # in a different request context than the POST that minted
    # the token. The mint endpoint stashes the cookiefile path
    # on `resolved["_cookiefile"]` for exactly this case; fall
    # back to the env-var path otherwise.
    pipe_cf = resolved.get("_cookiefile") or YT_COOKIES_FILE
    if pipe_cf:
        cmd += ["--cookies", pipe_cf]

    try:
        proxy_url = _proxy_router.proxy_for_url(source_url)
    except Exception:
        log.exception("ytdlp-pipe proxy_for_url() failed; falling back to direct")
        proxy_url = None
    if proxy_url:
        cmd += ["--proxy", proxy_url]

    cmd.append(source_url)

    try:
        # `bufsize=0` -> unbuffered stdout, so chunks reach us as
        # yt-dlp produces them rather than getting stuck in a
        # 4 KiB stdio buffer.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except Exception as exc:
        release_slot()
        log.warning("ytdlp-pipe spawn failed: %s", exc)
        raise HTTPException(status_code=502, detail="Couldn't start the download. Try again or use the desktop app.")

    response_headers = {
        "Content-Disposition": f'attachment; filename="{resolved["filename"]}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    media_type = resolved.get("mime_type") or "application/octet-stream"

    def gen():
        try:
            while True:
                chunk = proc.stdout.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
        except (OSError, GeneratorExit):
            pass
        except Exception:
            log.exception("ytdlp-pipe gen failed mid-flight")
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
            except Exception:
                pass
            # Drain stderr for log diagnostics. Capped so a chatty
            # extractor can't blow the log buffer.
            try:
                err_tail = proc.stderr.read(8192) if proc.stderr else b""
                if err_tail:
                    log.info("ytdlp-pipe stderr tail: %s", err_tail[-2048:].decode("utf-8", "replace"))
            except Exception:
                pass
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
            release_slot()

    return StreamingResponse(gen(), media_type=media_type, headers=response_headers)


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
@limiter.limit("30/minute")
async def api_limits(request: Request):
    with _jobs_lock:
        live_jobs = sum(1 for j in _jobs.values() if not j.ready and j.error is None)
    with _failures_lock:
        cooled_ips = sum(1 for dq in _failures.values() if len(dq) >= FAILURE_THRESHOLD)
    caller_ip = _client_ip(request)
    return {
        "mini_version": MINI_VERSION,
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
        "audio_formats": sorted(ALLOWED_AUDIO_FORMATS),
        "default_audio_format": DEFAULT_AUDIO_FORMAT,
        "qualities": sorted(QUALITY_HEIGHTS.keys()),
        "quality_aliases": dict(QUALITY_ALIASES),
        "default_quality": DEFAULT_QUALITY,
        "quality_heights": dict(QUALITY_HEIGHTS),
        "quality_download_caps_mb": {k: v // (1024 * 1024) for k, v in QUALITY_DOWNLOAD_CAPS.items()},
        "audio_download_caps_mb": {k: v // (1024 * 1024) for k, v in AUDIO_DOWNLOAD_CAPS.items()},
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
            # Owner-IP allowlist status. The count is exposed (so you
            # can confirm the env var was parsed) but the entries
            # themselves are not, to avoid leaking the maintainer's
            # home IP if /api/limits is ever scraped.
            "owner_allowlist_size": len(_OWNER_NETWORKS),
            "owner_bypass_active": _is_owner_ip(caller_ip),
            # Per-domain residential proxy. The proxy URL itself
            # (which contains credentials!) is never exposed - we
            # only surface whether it's configured and how many
            # domains are on the allowlist, so the operator can
            # confirm the env var was parsed without leaking
            # creds into a publicly-readable response.
            "residential_proxy": _proxy_router.status(),
        },
        "yt_censor_enabled": True,
        "full_app_url": "https://cmvideo.online",
        "extractors": _extractors.available_tools(),
        "extractor_versions": _extractors.tool_versions(),
        "memory": _extractors.memory_status(),
        "llm": _llm_status_safe(),
        # Diagnostic for the bgutil PoToken sidecar that fronts
        # YouTube. Owner-IP only (creds-free) so we can debug
        # plugin engagement without redeploying. Returns 200 with
        # a `disabled: true` flag for non-owner callers.
        "potoken_diag": _potoken_runtime_diag() if _is_owner_ip(caller_ip) else {"disabled": True},
        # Non-sensitive public boolean: True = bgutil sidecar is up AND
        # at least one concrete PoTokenProvider subclass is registered.
        # False = either the sidecar isn't responding or the plugin
        # didn't load. Null = couldn't determine. Exposed publicly so
        # the operator can confirm the stack is live from any IP without
        # needing CMVIDEO_OWNER_IPS set.
        "bgutil_ok": _bgutil_ok_public(),
    }


def _bgutil_ok_public() -> Optional[bool]:
    """Non-sensitive public check: is the bgutil stack fully operational?

    Returns True only when BOTH conditions hold:
      1. The Node sidecar responds to its /ping endpoint.
      2. At least one concrete PoTokenProvider subclass (BgUtilHTTP or
         BgUtilScriptNode) is registered with yt-dlp's plugin system.
    False means something is broken. None means we couldn't tell."""
    try:
        import requests as _r
        port = os.environ.get("CMVIDEO_POTOKEN_PORT", "4416")
        r = _r.get(f"http://127.0.0.1:{port}/ping", timeout=2)
        sidecar_up = (r.status_code == 200)
    except Exception:  # noqa: BLE001
        return False
    if not sidecar_up:
        return False
    try:
        from yt_dlp.extractor.youtube.pot.provider import PoTokenProvider
        concrete = [
            c for c in PoTokenProvider.__subclasses__()
            if c.__name__ != "PoTokenProvider"
        ]
        # Also check grandchildren (BgUtilHTTP sits one level deeper
        # under BgUtilPTPBase in some plugin versions).
        for sub in list(concrete):
            concrete.extend(sub.__subclasses__())
        return bool(concrete)
    except Exception:  # noqa: BLE001
        return None


def _potoken_runtime_diag() -> dict:
    """Owner-only runtime snapshot of the bgutil PoToken stack.

    Returns the last 3kB of the bgutil-server log plus a fresh ping.
    Never read by the frontend; only useful when curl-ing /api/limits
    from the maintainer's IP."""
    out: dict = {}
    try:
        import requests as _r
        port = os.environ.get("CMVIDEO_POTOKEN_PORT", "4416")
        r = _r.get(f"http://127.0.0.1:{port}/ping", timeout=2)
        out["sidecar_ping_status"] = r.status_code
        out["sidecar_ping_body"] = r.text[:200]
    except Exception as exc:  # noqa: BLE001
        out["sidecar_ping_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    log_path = os.environ.get("CMVIDEO_POTOKEN_LOG", "/tmp/bgutil-server.log")
    try:
        from pathlib import Path as _P
        p = _P(log_path)
        if p.exists():
            data = p.read_bytes()
            tail = data[-3000:].decode("utf-8", "replace")
            out["sidecar_log_tail"] = tail
            out["sidecar_log_size"] = p.stat().st_size
        else:
            out["sidecar_log"] = f"missing: {log_path}"
    except Exception as exc:  # noqa: BLE001
        out["sidecar_log_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
    # Also surface the registered PoTokenProvider grandchildren (the
    # actual concrete BgUtilHTTP / BgUtilScriptNode classes), not just
    # the abstract base.
    try:
        from yt_dlp.extractor.youtube.pot.provider import PoTokenProvider
        seen = []
        for sub in PoTokenProvider.__subclasses__():
            seen.append(f"{sub.__module__}.{sub.__name__}")
            for grand in sub.__subclasses__():
                seen.append(f"  -> {grand.__module__}.{grand.__name__}")
        out["registered_providers"] = seen or ["<none>"]
    except Exception as exc:  # noqa: BLE001
        out["registered_providers_error"] = f"{type(exc).__name__}"
    return out
