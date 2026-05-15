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
import logging
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

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


# ---------------------------------------------------------------------------
# Caps. The "mini" pitch only holds if these stay tight.
# ---------------------------------------------------------------------------
MAX_DOWNLOAD_DURATION_SECONDS = 30 * 60          # URL download only
MAX_DOWNLOAD_FILESIZE_BYTES = 200 * 1024 * 1024  # URL download only
MAX_CENSOR_DURATION_SECONDS = 8 * 60             # transcription is slow on free CPU
MAX_CENSOR_FILESIZE_BYTES = 100 * 1024 * 1024    # cap upload + output
MAX_UPLOAD_BYTES = 100 * 1024 * 1024             # multipart body size
MAX_VIDEO_HEIGHT = 720
AUDIO_BITRATE_KBPS = "192"

DOWNLOAD_TIMEOUT_SECONDS = 120
CENSOR_TIMEOUT_SECONDS = 240

RATE_LIMIT_PER_HOUR = "5/hour"

ALLOWED_FORMATS = {"mp4", "mp3"}
ALLOWED_MODES = {"download", "silence", "beep"}

WORDLISTS_DIR = Path(os.environ.get("CMVIDEO_WORDLISTS_DIR", "wordlists")).resolve()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cmvideo-mini")


# ---------------------------------------------------------------------------
# App boilerplate
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, default_limits=[])

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _validate_url(url: str) -> str:
    url = (url or "").strip()
    if not URL_RE.match(url):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    return url


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


def _video_format_selector() -> str:
    h = MAX_VIDEO_HEIGHT
    return (
        f"bestvideo[height<={h}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={h}][ext=mp4]+bestaudio/"
        f"best[height<={h}][ext=mp4]/"
        f"best[height<={h}]/best"
    )


def _do_download(url: str, fmt: str, tmpdir: Path, max_duration: int, max_filesize: int) -> Path:
    opts = _ydl_common_opts(tmpdir, max_duration, max_filesize)
    if fmt == "mp4":
        opts.update({
            "format": _video_format_selector(),
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

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise RuntimeError(f"Source rejected (likely longer than {max_duration // 60} min or larger than {max_filesize // (1024 * 1024)} MB).")

    out_files = [p for p in tmpdir.iterdir() if p.is_file() and p.suffix.lower() == f".{fmt}"]
    if not out_files:
        out_files = [p for p in tmpdir.iterdir() if p.is_file()]
        if not out_files:
            raise RuntimeError("Download finished but no output file was produced.")
    return max(out_files, key=lambda p: p.stat().st_size)


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
def _do_censor(src: Path, fmt: str, mode: str, tmpdir: Path) -> Path:
    duration = mini_censor.probe_duration(src)
    if duration > MAX_CENSOR_DURATION_SECONDS + 5:
        raise RuntimeError(
            f"Censoring is capped at {MAX_CENSOR_DURATION_SECONDS // 60} minutes "
            f"on the mini version (this clip is {int(duration // 60)} min). Use the full app."
        )
    words = _wordlist_tokens()
    intervals = mini_censor.find_intervals(src, words)
    dst = tmpdir / f"censored.{fmt}"
    mini_censor.render(src, dst, intervals, mode=mode, fmt=fmt)
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
@limiter.limit("30/hour")
async def api_info(request: Request, body: InfoRequest):
    url = _validate_url(body.url)
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_do_info, url), timeout=25)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Source took too long to respond.")
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=_friendly_ydl_error(e))
    except Exception as e:
        log.exception("info failed")
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(info)


@app.post("/api/process")
@limiter.limit(RATE_LIMIT_PER_HOUR)
async def api_process(
    request: Request,
    format: str = Form(...),
    mode: str = Form(...),
    url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
):
    fmt = (format or "").lower().strip()
    md = (mode or "").lower().strip()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail="Format must be 'mp4' or 'mp3'.")
    if md not in ALLOWED_MODES:
        raise HTTPException(status_code=400, detail="Mode must be 'download', 'silence', or 'beep'.")

    have_url = bool(url and url.strip())
    have_file = file is not None and bool(file.filename)
    if have_url == have_file:
        raise HTTPException(status_code=400, detail="Provide either a URL or a file, not both / neither.")

    if md == "download" and have_file:
        raise HTTPException(status_code=400, detail="Download mode is URL-only - you already have the file locally.")

    tmpdir = Path(tempfile.mkdtemp(prefix="cmvm_"))
    try:
        # 1) Get the source media into tmpdir.
        if have_url:
            clean_url = _validate_url(url)
            max_dur = MAX_DOWNLOAD_DURATION_SECONDS if md == "download" else MAX_CENSOR_DURATION_SECONDS
            max_size = MAX_DOWNLOAD_FILESIZE_BYTES if md == "download" else MAX_CENSOR_FILESIZE_BYTES
            try:
                src = await asyncio.wait_for(
                    asyncio.to_thread(_do_download, clean_url, fmt, tmpdir, max_dur, max_size),
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
                    asyncio.to_thread(_do_censor, src, fmt, md, tmpdir),
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
        "full_app_url": "https://cmvideo.online",
    }
