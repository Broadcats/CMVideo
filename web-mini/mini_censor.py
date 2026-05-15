"""Small, standalone censoring pipeline for CMVideo Mini.

This is intentionally a slim version of the full `censor/` package that
ships with the desktop app. It supports just Silence and Beep modes
(no 'Fun' TTS), uses the smallest faster-whisper model that gives
useful word timestamps, and does exact-token matching against the
bundled wordlists. The full app has fuzzy/phonetic matching, all the
formats, batch processing, and the heavier whisper models - which is
exactly the pitch that the mini version makes to its visitors.
"""

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# faster-whisper is heavy; only imported once and reused across calls.
_WHISPER_MODEL = None

WHISPER_MODEL_NAME = "tiny.en"
WHISPER_COMPUTE_TYPE = "int8"

log = logging.getLogger("cmvideo-mini.censor")


@dataclass
class Interval:
    start: float
    end: float
    word: str


def _load_model():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        from faster_whisper import WhisperModel
        log.info("Loading whisper model: %s (compute=%s)", WHISPER_MODEL_NAME, WHISPER_COMPUTE_TYPE)
        _WHISPER_MODEL = WhisperModel(
            WHISPER_MODEL_NAME,
            device="cpu",
            compute_type=WHISPER_COMPUTE_TYPE,
        )
    return _WHISPER_MODEL


def load_wordlists(wordlists_dir: Path) -> set:
    """Read every *.txt in wordlists/, strip comments, return a set of
    lowercase tokens."""
    words = set()
    if not wordlists_dir.is_dir():
        log.warning("Wordlists dir %s missing - no words will be censored", wordlists_dir)
        return words
    for f in sorted(wordlists_dir.glob("*.txt")):
        for raw in f.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            words.add(line.lower())
    log.info("Loaded %d censor tokens from %s", len(words), wordlists_dir)
    return words


_TOKEN_STRIP_RE = re.compile(r"[^\w']")


def _normalize(token: str) -> str:
    return _TOKEN_STRIP_RE.sub("", token).lower()


def find_intervals(media_path: Path, words: set) -> list:
    """Transcribe `media_path` and return a list of Interval()s for
    every word whose normalised form is in `words`."""
    if not words:
        return []
    model = _load_model()
    segments, _ = model.transcribe(
        str(media_path),
        word_timestamps=True,
        language="en",
        vad_filter=True,
        beam_size=1,            # speed > quality for the mini tier
        temperature=0.0,
    )
    hits = []
    for seg in segments:
        if not seg.words:
            continue
        for w in seg.words:
            norm = _normalize(w.word)
            if norm and norm in words:
                hits.append(Interval(start=float(w.start), end=float(w.end), word=norm))
    log.info("Found %d censor intervals in %s", len(hits), media_path.name)
    return hits


def _enable_expr(intervals) -> str:
    """Build an ffmpeg `enable` expression that is true during any
    censor interval. Pads each interval by 30 ms so word edges are
    fully covered."""
    pad = 0.03
    parts = [
        f"between(t,{max(0.0, iv.start - pad):.3f},{iv.end + pad:.3f})"
        for iv in intervals
    ]
    return "+".join(parts)


def render(src: Path, dst: Path, intervals, mode: str, fmt: str) -> None:
    """Re-encode `src` to `dst` applying `mode` ('silence' or 'beep')
    to every interval. `fmt` is the requested output extension
    ('mp4' or 'mp3'); when there are no intervals we just copy."""
    if not intervals:
        log.info("No censor intervals - copying through")
        shutil.copy(src, dst)
        return

    enable = _enable_expr(intervals)
    is_audio_only = fmt == "mp3" or src.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".opus"}

    if mode == "silence":
        af = f"volume=enable='{enable}':volume=0"
        cmd = ["ffmpeg", "-y", "-i", str(src)]
        if is_audio_only:
            cmd += ["-vn", "-af", af, "-c:a", "libmp3lame" if fmt == "mp3" else "aac", "-b:a", "192k"]
        else:
            cmd += ["-af", af, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
        cmd += [str(dst)]
    elif mode == "beep":
        # Generate a continuous 1 kHz sine, gate it so it only sounds
        # *during* censor intervals, and mix it with the silenced
        # original. amix halves levels with N=2, so apply 2x to
        # restore.
        filter_complex = (
            f"[0:a]volume=enable='{enable}':volume=0[silenced];"
            f"[1:a]volume=enable='not({enable})':volume=0[gated];"
            f"[silenced][gated]amix=inputs=2:duration=first:dropout_transition=0,volume=2.0[a]"
        )
        cmd = [
            "ffmpeg", "-y",
            "-i", str(src),
            "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=44100",
            "-filter_complex", filter_complex,
        ]
        if is_audio_only:
            cmd += ["-map", "[a]", "-c:a", "libmp3lame" if fmt == "mp3" else "aac", "-b:a", "192k", "-shortest"]
        else:
            cmd += [
                "-map", "0:v?", "-map", "[a]",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", "-shortest",
            ]
        cmd += [str(dst)]
    else:
        raise ValueError(f"Unknown mode: {mode!r}")

    log.info("ffmpeg: %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        log.error("ffmpeg failed (%d):\n%s", res.returncode, res.stderr[-2000:])
        raise RuntimeError(f"ffmpeg returned {res.returncode}: {res.stderr.splitlines()[-1] if res.stderr else 'no output'}")


def probe_duration(path: Path) -> float:
    """Return media duration in seconds (0.0 if ffprobe can't read it)."""
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        return float(res.stdout.strip() or "0") if res.returncode == 0 else 0.0
    except (subprocess.SubprocessError, ValueError):
        return 0.0
