"""yt-dlp wrapper for downloading video and audio.

Accepts any URL yt-dlp can extract, plus any community plugin sitting
in the CMVideo plugin folder (see `censor.plugins`). Output format is
one of `SUPPORTED_DOWNLOAD_FORMATS` (mp4/mov/mkv/webm/avi/flv for video,
mp3/wav/ogg/m4a/flac/opus for audio).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from . import plugins as _user_plugins


# Loose http(s) check; the actual extractor selection is yt-dlp's job.
_URL_RE = re.compile(r"^\s*https?://\S+\s*$", re.IGNORECASE)


VIDEO_DOWNLOAD_FORMATS = ("mp4", "mov", "mkv", "webm", "avi", "flv")
AUDIO_DOWNLOAD_FORMATS = ("mp3", "m4a", "ogg", "opus", "wav", "flac")
SUPPORTED_DOWNLOAD_FORMATS = VIDEO_DOWNLOAD_FORMATS + AUDIO_DOWNLOAD_FORMATS
_AUDIO_FORMATS = set(AUDIO_DOWNLOAD_FORMATS)
# Lossless formats: kbps choice is meaningless.
_LOSSLESS_FORMATS = {"wav", "flac"}

# UI labels -> max video height. None = no cap (best); "worst" = take the
# lowest-resolution stream the site offers.
VIDEO_QUALITY_LABELS = (
    "Best",
    "4K (2160p)",
    "1440p",
    "1080p",
    "720p",
    "480p",
    "360p",
    "240p",
    "144p",
    "Worst",
)
_VIDEO_HEIGHT: dict[str, int | None | str] = {
    "Best": None,
    "4K (2160p)": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "240p": 240,
    "144p": 144,
    "Worst": "worst",
}

# UI labels -> postprocessor preferredquality (kbps as string).
# "0" tells yt-dlp's FFmpegExtractAudio postprocessor to keep the source
# bitrate; for "Worst" we additionally pick worstaudio as the source.
AUDIO_QUALITY_LABELS = (
    "Best",
    "320 kbps",
    "256 kbps",
    "192 kbps",
    "160 kbps",
    "128 kbps",
    "96 kbps",
    "64 kbps",
    "48 kbps",
    "Worst",
)
_AUDIO_BITRATE = {
    "Best": "0",
    "320 kbps": "320",
    "256 kbps": "256",
    "192 kbps": "192",
    "160 kbps": "160",
    "128 kbps": "128",
    "96 kbps": "96",
    "64 kbps": "64",
    "48 kbps": "48",
    "Worst": "0",
    # Back-compat with the v0.4.0 labels and raw kbps strings (for configs
    # / scripts that still pass the old vocabulary through the pipeline).
    "High (192k)": "192",
    "Medium (128k)": "128",
    "Low (96k)": "96",
    "320": "320",
    "256": "256",
    "192": "192",
    "160": "160",
    "128": "128",
    "96": "96",
    "64": "64",
    "48": "48",
    "0": "0",
}

# yt-dlp postprocessor codec names per output format.
_YTDLP_AUDIO_CODEC = {
    "mp3": "mp3",
    "m4a": "m4a",
    "ogg": "vorbis",
    "opus": "opus",
    "wav": "wav",
    "flac": "flac",
}


# Progress callback signature: (fraction_0_to_1, message_text)
DownloadProgressCb = Callable[[float, str], None]


def is_url(text: str) -> bool:
    """True if the text looks like a downloadable URL."""
    return bool(_URL_RE.match(text or ""))


def default_download_dir() -> Path:
    """User's Downloads folder, or home if it doesn't exist."""
    candidates = [
        Path.home() / "Downloads",
        Path.home() / "downloads",
    ]
    for p in candidates:
        if p.is_dir():
            return p
    return Path.home()


def download(
    url: str,
    output_dir: Path,
    fmt: str = "mp4",
    video_quality: str = "Best",
    audio_quality: str = "192",
    progress_cb: DownloadProgressCb | None = None,
    cookies_file: Path | None = None,
) -> Path:
    """Download a URL to `output_dir` in the requested format.

    `video_quality` is one of VIDEO_QUALITY_LABELS (used for video
    formats: mp4/mov/mkv/webm/avi/flv).
    `audio_quality` is either an AUDIO_QUALITY_LABELS label or a raw
    kbps string ("48"-"320", or "0" for "keep source"). Used for the
    lossy audio formats; WAV and FLAC are always lossless.

    `cookies_file` (optional) is a Netscape-format `cookies.txt` exported
    from your browser. Passed straight to yt-dlp as `cookiefile`, which
    is enough to authenticate against sites like Patreon, soft-paywalled
    tubes, and members-only YouTube. Note: yt-dlp still refuses
    DRM-protected streams (Netflix, Disney+, paid Pornhub Premium etc.)
    regardless of cookies.

    Returns the final path of the saved file (post-postprocessing for audio).
    Raises RuntimeError on failure.
    """
    fmt = (fmt or "mp4").lower()
    if fmt not in SUPPORTED_DOWNLOAD_FORMATS:
        raise ValueError(f"Unsupported download format: {fmt!r}")

    if cookies_file is not None and not Path(cookies_file).is_file():
        raise RuntimeError(
            f"Cookies file not found: {cookies_file}. Re-pick it or clear "
            "the cookies setting."
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from yt_dlp import YoutubeDL  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "yt-dlp is not installed in the venv. Re-run ./run.sh to install it."
        ) from e

    # Idempotent; see `censor.plugins.register_with_yt_dlp`.
    _user_plugins.register_with_yt_dlp()

    hooks: list[Callable] = []
    final_path_holder: dict[str, str] = {}

    if progress_cb is not None:
        hooks.append(_make_download_hook(progress_cb))
    hooks.append(_make_finished_hook(final_path_holder))

    if fmt in VIDEO_DOWNLOAD_FORMATS:
        format_selector = _video_format_selector(fmt, video_quality)
        ydl_opts: dict = {
            "format": format_selector,
            "outtmpl": str(output_dir / "%(title).180B [%(id)s].%(ext)s"),
            "merge_output_format": fmt,
            "noplaylist": True,
            "progress_hooks": hooks,
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
        }
    else:
        # Lossless formats ignore the kbps choice; lossy formats honour it.
        if fmt in _LOSSLESS_FORMATS or audio_quality == "Best":
            preferred_quality = "0"
        else:
            preferred_quality = _AUDIO_BITRATE.get(audio_quality, "192")
        audio_source = "worstaudio/worst" if audio_quality == "Worst" else "bestaudio/best"
        ydl_opts = {
            "format": audio_source,
            "outtmpl": str(output_dir / "%(title).180B [%(id)s].%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": _YTDLP_AUDIO_CODEC[fmt],
                    "preferredquality": preferred_quality,
                }
            ],
            "noplaylist": True,
            "progress_hooks": hooks,
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
        }

    if cookies_file is not None:
        ydl_opts["cookiefile"] = str(cookies_file)

    if progress_cb is not None:
        progress_cb(0.0, "Resolving URL...")

    try:
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            base = Path(ydl.prepare_filename(info))
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Download failed: {e}") from e

    # Audio postprocessor / video remuxer often swap the extension.
    if fmt in _AUDIO_FORMATS or fmt != "mp4":
        candidate = base.with_suffix("." + fmt)
        if candidate.exists():
            return candidate

    if base.exists():
        return base
    if final_path_holder.get("path"):
        p = Path(final_path_holder["path"])
        if p.exists():
            if not p.suffix.lower().endswith(fmt):
                alt = p.with_suffix("." + fmt)
                if alt.exists():
                    return alt
            return p

    raise RuntimeError(
        f"Download finished but output file was not found at {base}"
    )


def _video_format_selector(fmt: str, quality_label: str) -> str:
    """Build a yt-dlp format selector for video output, capped to the chosen
    height (or pinned to ``worstvideo`` for ``Worst``).

    MP4 prefers native mp4 streams so the merge doesn't transcode.
    WebM prefers VP9/Opus streams so the resulting container is valid.
    Other containers (MOV/MKV/AVI/FLV) accept whatever streams yt-dlp
    grabs and rely on the FFmpeg remux to convert at write time.
    """
    height = _VIDEO_HEIGHT.get(quality_label)
    if height == "worst":
        return "worstvideo+worstaudio/worst"

    if fmt == "mp4":
        if height is None:
            return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        return (
            f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]/"
            f"best[ext=mp4][height<={height}]/best[height<={height}]/best"
        )
    if fmt == "webm":
        if height is None:
            return "bestvideo[ext=webm]+bestaudio[ext=webm]/bestvideo+bestaudio/best"
        return (
            f"bestvideo[ext=webm][height<={height}]+bestaudio[ext=webm]/"
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        )
    # mov / mkv / avi / flv: just take the best streams and let yt-dlp remux.
    if height is None:
        return "bestvideo+bestaudio/best"
    return f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"


def _make_download_hook(progress_cb: DownloadProgressCb) -> Callable:
    def hook(d):  # type: ignore[no-untyped-def]
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            if total:
                frac = max(0.0, min(0.99, downloaded / total))
                mb_done = downloaded / 1_048_576
                mb_total = total / 1_048_576
                mbps = speed / 1_048_576 if speed else 0
                msg = (
                    f"Downloading... {int(frac * 100)}% "
                    f"({mb_done:.1f} / {mb_total:.1f} MB at {mbps:.1f} MB/s)"
                )
                progress_cb(frac, msg)
            else:
                mb_done = downloaded / 1_048_576
                progress_cb(0.5, f"Downloading... {mb_done:.1f} MB")
        elif status == "finished":
            progress_cb(1.0, "Download complete, post-processing...")

    return hook


def _make_finished_hook(holder: dict) -> Callable:
    def hook(d):  # type: ignore[no-untyped-def]
        if d.get("status") == "finished":
            fn = d.get("filename")
            if fn:
                holder["path"] = fn

    return hook
