"""yt-dlp wrapper for downloading video and audio.

Accepts any URL yt-dlp can extract, plus any community plugin sitting
in the CMVideo plugin folder (see `censor.plugins`). Output format is
one of {mp4, mov, mp3, wav, ogg}.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from . import plugins as _user_plugins


# Loose http(s) check; the actual extractor selection is yt-dlp's job.
_URL_RE = re.compile(r"^\s*https?://\S+\s*$", re.IGNORECASE)


SUPPORTED_DOWNLOAD_FORMATS = ("mp4", "mov", "mp3", "wav", "ogg")
_AUDIO_FORMATS = {"mp3", "wav", "ogg"}

# UI labels -> max video height. "Best" means no cap.
VIDEO_QUALITY_LABELS = ("Best", "1080p", "720p", "480p", "360p")
_VIDEO_HEIGHT = {
    "Best": None,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}

# UI labels -> postprocessor preferredquality (kbps as string, "0" = best).
AUDIO_QUALITY_LABELS = (
    "Best",
    "High (192k)",
    "Medium (128k)",
    "Low (96k)",
)
_AUDIO_BITRATE = {
    "Best": "0",
    "High (192k)": "192",
    "Medium (128k)": "128",
    "Low (96k)": "96",
    # Also accept raw kbps strings so the pipeline can pass them through.
    "320": "320",
    "192": "192",
    "128": "128",
    "96": "96",
    "0": "0",
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

    `video_quality` is one of VIDEO_QUALITY_LABELS (used for MP4/MOV).
    `audio_quality` is either an AUDIO_QUALITY_LABELS label or a raw
    kbps string ("96", "128", "192", "320", or "0" for best). Used for
    MP3/OGG only; WAV is always lossless PCM.

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

    if fmt in ("mp4", "mov"):
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
        codec_map = {"mp3": "mp3", "wav": "wav", "ogg": "vorbis"}
        # WAV is uncompressed; kbps is meaningless. Lossy formats honour
        # the requested bitrate.
        if fmt == "wav":
            preferred_quality = "0"
        else:
            preferred_quality = _AUDIO_BITRATE.get(audio_quality, "192")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(output_dir / "%(title).180B [%(id)s].%(ext)s"),
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": codec_map[fmt],
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

    # Audio postprocessor / video remuxer swap the extension.
    if fmt in _AUDIO_FORMATS or fmt == "mov":
        candidate = base.with_suffix("." + fmt)
        if candidate.exists():
            return candidate

    # Some merges land at a different extension (e.g. .webm if merge failed).
    if base.exists():
        return base
    if final_path_holder.get("path"):
        p = Path(final_path_holder["path"])
        if p.exists():
            # Audio postprocessor / mov remux might still have changed the suffix.
            if fmt != "mp4" and not p.suffix.lower().endswith(fmt):
                alt = p.with_suffix("." + fmt)
                if alt.exists():
                    return alt
            return p

    raise RuntimeError(
        f"Download finished but output file was not found at {base}"
    )


def _video_format_selector(fmt: str, quality_label: str) -> str:
    """Build a yt-dlp format selector for MP4/MOV, capped to the chosen height.

    MP4 prefers native mp4 streams (no transcoding on merge). MOV doesn't
    care about input streams - we just remux into a QuickTime container.
    """
    height = _VIDEO_HEIGHT.get(quality_label)
    if fmt == "mp4":
        if height is None:
            return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        return (
            f"bestvideo[ext=mp4][height<={height}]+bestaudio[ext=m4a]/"
            f"best[ext=mp4][height<={height}]/best[height<={height}]/best"
        )
    # mov
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
