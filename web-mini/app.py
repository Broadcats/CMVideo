"""CMVideo Mini - the URL -> MP4/MP3 slice of CMVideo, running on the web.

Designed to deploy as a Hugging Face Space (Docker SDK). Intentionally
limited: 720p / 192 kbps caps, 30 minute max duration, 200 MB max file,
5 downloads per hour per IP. Anything richer (censoring, batch, every
format and quality, no caps) lives in the desktop app at
https://cmvideo.online.
"""

import asyncio
import logging
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.background import BackgroundTask


# ---------------------------------------------------------------------------
# Hard caps - these are the whole point of the "mini" version. Bumping any
# of these turns the demo into a YouTube-downloads-as-a-service that I do
# not want to run on a free tier.
# ---------------------------------------------------------------------------
MAX_DURATION_SECONDS = 30 * 60         # 30 minutes
MAX_FILESIZE_BYTES = 200 * 1024 * 1024 # 200 MB
MAX_VIDEO_HEIGHT = 720                 # mp4 capped at 720p
AUDIO_BITRATE_KBPS = "192"             # mp3 capped at 192 kbps
JOB_TIMEOUT_SECONDS = 120              # yt-dlp wall-clock cap

RATE_LIMIT_PER_HOUR = "5/hour"

ALLOWED_FORMATS = {"mp4", "mp3"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cmvideo-mini")


# ---------------------------------------------------------------------------
# App boilerplate
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, default_limits=[])


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info(
        "CMVideo Mini starting. Caps: %s min duration, %s MB filesize, %sp / %s kbps, %s/IP",
        MAX_DURATION_SECONDS // 60,
        MAX_FILESIZE_BYTES // (1024 * 1024),
        MAX_VIDEO_HEIGHT,
        AUDIO_BITRATE_KBPS,
        RATE_LIMIT_PER_HOUR,
    )
    yield


app = FastAPI(title="CMVideo Mini", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS: the canonical UI is the widget embedded on cmvideo.online, which
# calls this Space cross-origin. The Space's own / endpoint is a fallback
# for direct visitors and same-origin from there needs no CORS.
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
# Request models
# ---------------------------------------------------------------------------
URL_RE = re.compile(r"^https?://", re.IGNORECASE)


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)
    format: str = Field(..., pattern="^(mp4|mp3)$")


class InfoRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)


# ---------------------------------------------------------------------------
# yt-dlp helpers
# ---------------------------------------------------------------------------
def _validate_url(url: str) -> str:
    """Trim and sanity-check the URL. yt-dlp itself does the heavy lifting."""
    url = url.strip()
    if not URL_RE.match(url):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")
    return url


def _ydl_common_opts(tmpdir: Path) -> dict:
    return {
        "outtmpl": str(tmpdir / "%(title).80s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "max_filesize": MAX_FILESIZE_BYTES,
        "match_filter": yt_dlp.utils.match_filter_func(
            f"duration <? {MAX_DURATION_SECONDS}"
        ),
        "socket_timeout": 25,
        "retries": 1,
        "concurrent_fragment_downloads": 1,
    }


def _video_format_selector() -> str:
    """Cap height + prefer mp4-friendly streams."""
    h = MAX_VIDEO_HEIGHT
    return (
        f"bestvideo[height<={h}][ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={h}][ext=mp4]+bestaudio/"
        f"best[height<={h}][ext=mp4]/"
        f"best[height<={h}]/best"
    )


def _do_download(url: str, fmt: str, tmpdir: Path) -> Path:
    """Run yt-dlp synchronously and return the produced file path."""
    opts = _ydl_common_opts(tmpdir)
    if fmt == "mp4":
        opts.update({
            "format": _video_format_selector(),
            "merge_output_format": "mp4",
            "postprocessors": [
                {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
            ],
        })
    else:  # mp3
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
            raise RuntimeError("Source rejected by length / size filter (likely longer than 30 min or larger than 200 MB).")

    # The post-processor swap means the on-disk extension can drift from
    # what extract_info reports, so just glob what landed in tmpdir.
    out_files = [p for p in tmpdir.iterdir() if p.is_file() and p.suffix.lower() == f".{fmt}"]
    if not out_files:
        # Fallback: any file (could be a partial mp4 webm pre-remux race)
        out_files = [p for p in tmpdir.iterdir() if p.is_file()]
        if not out_files:
            raise RuntimeError("Download finished but no output file was produced.")
    return max(out_files, key=lambda p: p.stat().st_size)


def _do_info(url: str) -> dict:
    """Cheap metadata pull. No download. Strict timeouts via socket_timeout."""
    with yt_dlp.YoutubeDL({
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 15,
    }) as ydl:
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
        "over_cap": isinstance(duration, (int, float)) and duration > MAX_DURATION_SECONDS,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def index(request: Request):
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "max_duration_min": MAX_DURATION_SECONDS // 60,
            "max_filesize_mb": MAX_FILESIZE_BYTES // (1024 * 1024),
            "max_video_height": MAX_VIDEO_HEIGHT,
            "audio_bitrate_kbps": AUDIO_BITRATE_KBPS,
            "rate_limit": RATE_LIMIT_PER_HOUR,
        },
    )


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True}


@app.post("/api/info")
@limiter.limit("30/hour")
async def api_info(request: Request, body: InfoRequest):
    url = _validate_url(body.url)
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_do_info, url), timeout=25)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Source took too long to respond.")
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(status_code=400, detail=str(e).splitlines()[-1])
    except Exception as e:
        log.exception("info failed")
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(info)


@app.post("/api/download")
@limiter.limit(RATE_LIMIT_PER_HOUR)
async def api_download(request: Request, body: DownloadRequest):
    url = _validate_url(body.url)
    fmt = body.format.lower()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(status_code=400, detail="Format must be 'mp4' or 'mp3'.")

    tmpdir = Path(tempfile.mkdtemp(prefix="cmvm_"))
    try:
        try:
            out_path: Path = await asyncio.wait_for(
                asyncio.to_thread(_do_download, url, fmt, tmpdir),
                timeout=JOB_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=f"Download exceeded the {JOB_TIMEOUT_SECONDS}s mini-app cap. Try a shorter clip or download the full desktop app.",
            )
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).splitlines()[-1]
            if "File is larger than max-filesize" in msg or "max-filesize" in msg.lower():
                raise HTTPException(status_code=413, detail=f"That clip is over the {MAX_FILESIZE_BYTES // (1024 * 1024)} MB mini-app cap.")
            raise HTTPException(status_code=400, detail=msg)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

        size = out_path.stat().st_size
        if size > MAX_FILESIZE_BYTES:
            raise HTTPException(status_code=413, detail=f"Output is over the {MAX_FILESIZE_BYTES // (1024 * 1024)} MB cap.")

        # Sanitised download filename for the browser.
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", out_path.stem)[:80] or "video"
        download_name = f"{safe_name}.{fmt}"

        media_type = "video/mp4" if fmt == "mp4" else "audio/mpeg"
        return FileResponse(
            path=str(out_path),
            media_type=media_type,
            filename=download_name,
            background=BackgroundTask(shutil.rmtree, tmpdir, ignore_errors=True),
        )
    except HTTPException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise


@app.get("/api/limits", include_in_schema=False)
async def api_limits():
    return {
        "max_duration_seconds": MAX_DURATION_SECONDS,
        "max_filesize_bytes": MAX_FILESIZE_BYTES,
        "max_video_height": MAX_VIDEO_HEIGHT,
        "audio_bitrate_kbps": AUDIO_BITRATE_KBPS,
        "rate_limit": RATE_LIMIT_PER_HOUR,
        "full_app_url": "https://cmvideo.online",
    }
