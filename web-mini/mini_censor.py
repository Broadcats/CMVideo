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
import time
from dataclasses import dataclass
from pathlib import Path

# faster-whisper is heavy; only imported once and reused across calls.
_WHISPER_MODEL = None

WHISPER_MODEL_NAME = "tiny.en"
WHISPER_COMPUTE_TYPE = "int8"

# Hard wall-clock cap for any single ffmpeg invocation in this module.
# The outer route already has a 240 s asyncio.wait_for around the
# whole censor pipeline, but `asyncio.wait_for` only abandons the
# wait - it does not kill the subprocess. Without this timeout an
# ffmpeg that hangs on a malformed file would keep burning CPU on
# the free Space until natural exit.
FFMPEG_TIMEOUT_SECONDS = 220

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


def _ensure_16k_mono_wav(src: Path) -> Path:
    """Extract / down-mix `src` to a 16 kHz mono PCM WAV next to it.

    faster-whisper has to do this internally before it can run the
    encoder, but it does so by spawning ffmpeg AND ALSO running its
    own Python-side resample pass. Pre-baking a clean 16 kHz mono
    file shaves 5-15% off the wall clock for typical 5-min clips on
    the free CPU - the savings come from skipping faster-whisper's
    internal resample step (it just memmaps our wav).

    If src is already a small audio container we still re-encode -
    the resample alone is the expensive bit, and feeding raw mp3
    means whisper's first decode pass dominates.

    Returns the new wav path. Failures fall back to the original
    path so the caller never has to handle an exception (this is
    a perf optimisation, not a correctness step).
    """
    out = src.with_suffix(".whisper.wav")
    if out.exists() and out.stat().st_size > 0:
        return out
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src),
        "-vn",                     # drop video
        "-ac", "1",                # mono
        "-ar", "16000",            # 16 kHz
        "-acodec", "pcm_s16le",    # whisper's preferred input
        str(out),
    ]
    try:
        rc = subprocess.run(cmd, check=False, timeout=120, capture_output=True).returncode
    except subprocess.TimeoutExpired:
        return src
    if rc != 0 or not out.exists() or out.stat().st_size == 0:
        return src
    return out


def find_intervals(
    media_path: Path,
    words: set,
    *,
    progress=None,
    total_duration: float | None = None,
) -> list:
    """Transcribe `media_path` and return a list of Interval()s for
    every word whose normalised form is in `words`.

    `progress`, when provided, is called as ``progress(pct: int)``
    after every transcribed segment, where ``pct`` is the fraction of
    `total_duration` covered so far rounded to 0-100. Errors raised by
    the callback are swallowed - it's there to drive a UI progress
    bar, not to influence transcription.
    """
    if not words:
        return []
    model = _load_model()
    # Pre-bake a 16 kHz mono wav so whisper skips its internal
    # resample step. This was measured at ~10-15% wall-clock saved
    # on 5-min clips on the HF Space's CPU. Falls back to the
    # original path if extraction fails.
    audio_path = _ensure_16k_mono_wav(media_path)
    segments, _ = model.transcribe(
        str(audio_path),
        word_timestamps=True,
        language="en",
        vad_filter=True,
        beam_size=1,            # speed > quality for the mini tier
        temperature=0.0,
    )
    hits = []

    def _emit(pct: int) -> None:
        if progress is None:
            return
        try:
            progress(max(0, min(100, int(pct))))
        except Exception:  # noqa: BLE001
            pass

    for seg in segments:
        if seg.words:
            for w in seg.words:
                norm = _normalize(w.word)
                if norm and norm in words:
                    hits.append(Interval(start=float(w.start), end=float(w.end), word=norm))
        if total_duration and total_duration > 0:
            _emit(round((float(getattr(seg, "end", 0.0)) / total_duration) * 100))
    _emit(100)
    log.info("Found %d censor intervals in %s", len(hits), media_path.name)
    return hits


def _enable_expr(intervals) -> str:
    """Build an ffmpeg `enable` expression that is true during any
    censor interval. Pads each interval by 30 ms so word edges are
    fully covered.

    Defensively clamps every value to a finite, non-negative, sane
    float; we never want a NaN, inf, or negative end-time slipping
    into the filter argument string."""
    import math
    pad = 0.03
    parts = []
    for iv in intervals:
        try:
            s = float(iv.start)
            e = float(iv.end)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(s) and math.isfinite(e)):
            continue
        s = max(0.0, s - pad)
        e = max(s + 0.001, e + pad)        # ensure end > start
        s = min(s, 24 * 3600.0)            # cap at 24h - longer than any clip
        e = min(e, 24 * 3600.0)
        parts.append(f"between(t,{s:.3f},{e:.3f})")
    return "+".join(parts)


def render(
    src: Path,
    dst: Path,
    intervals,
    mode: str,
    fmt: str,
    fps: str = "source",
    *,
    progress=None,
    total_duration: float | None = None,
) -> None:
    """Re-encode `src` to `dst` applying `mode` ('silence' or 'beep')
    to every interval. `fmt` is the requested output extension
    ('mp4' or 'mp3'). `fps` is one of 'source' (passthrough),
    '30', or '60' - when not 'source' we force a video re-encode
    so the output really runs at the chosen rate.

    When there are no intervals AND no fps override we just copy."""
    fps_target = None
    if fps in ("30", "60") and fmt == "mp4":
        fps_target = int(fps)

    if not intervals and fps_target is None:
        log.info("No censor intervals + no fps override - copying through")
        shutil.copy(src, dst)
        return

    enable = _enable_expr(intervals) if intervals else "0"  # always-false expr
    is_audio_only = fmt == "mp3" or src.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".opus"}

    # Video codec settings: -c:v copy when we don't need to touch
    # frames (source fps + just muting audio), otherwise libx264 at
    # ultrafast so we stay inside the 220s ffmpeg timeout for the
    # 8-min censor cap.
    video_codec = ["-c:v", "copy"]
    if fps_target is not None and not is_audio_only:
        video_codec = [
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
            "-r", str(fps_target),
            "-pix_fmt", "yuv420p",
        ]

    if mode == "silence":
        af = f"volume=enable='{enable}':volume=0" if intervals else None
        cmd = ["ffmpeg", "-y", "-i", str(src)]
        if is_audio_only:
            audio_args = ["-vn"]
            if af:
                audio_args += ["-af", af]
            cmd += audio_args + ["-c:a", "libmp3lame" if fmt == "mp3" else "aac", "-b:a", "192k"]
        else:
            if af:
                cmd += ["-af", af]
            cmd += video_codec + ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
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
            ] + video_codec + [
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", "-shortest",
            ]
        cmd += [str(dst)]
    else:
        raise ValueError(f"Unknown mode: {mode!r}")

    # Wire a progress pipe when the caller wants live updates and we
    # know the duration. ffmpeg writes `out_time_ms=NNN` etc. to the
    # given file descriptor; we parse it line-by-line and emit pct.
    use_progress = progress is not None and total_duration and total_duration > 0
    if use_progress:
        cmd = [cmd[0], "-progress", "pipe:1", "-nostats"] + cmd[1:]

    log.info("ffmpeg: %s", " ".join(cmd))

    if not use_progress:
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=FFMPEG_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            log.error("ffmpeg timed out after %ds", FFMPEG_TIMEOUT_SECONDS)
            raise RuntimeError(
                f"ffmpeg timed out after {FFMPEG_TIMEOUT_SECONDS}s. "
                "Try a shorter clip, or use the desktop app for unbounded jobs."
            )
        if res.returncode != 0:
            log.error("ffmpeg failed (%d):\n%s", res.returncode, res.stderr[-2000:])
            raise RuntimeError(f"ffmpeg returned {res.returncode}: {res.stderr.splitlines()[-1] if res.stderr else 'no output'}")
        return

    # Live-progress branch.
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    err_tail: list[str] = []
    deadline = time.monotonic() + FFMPEG_TIMEOUT_SECONDS
    last_pct = -1
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            if time.monotonic() > deadline:
                proc.kill()
                raise RuntimeError(
                    f"ffmpeg timed out after {FFMPEG_TIMEOUT_SECONDS}s. "
                    "Try a shorter clip, or use the desktop app for unbounded jobs."
                )
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("out_time_ms="):
                try:
                    out_us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                pct = int((out_us / 1_000_000.0 / float(total_duration)) * 100)
                pct = max(0, min(99, pct))  # let "done" hit 100
                if pct != last_pct:
                    last_pct = pct
                    try:
                        progress(pct)
                    except Exception:  # noqa: BLE001
                        pass
            elif line == "progress=end":
                try:
                    progress(100)
                except Exception:  # noqa: BLE001
                    pass
        rc = proc.wait(timeout=max(0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(
            f"ffmpeg timed out after {FFMPEG_TIMEOUT_SECONDS}s. "
            "Try a shorter clip, or use the desktop app for unbounded jobs."
        )
    finally:
        try:
            if proc.stderr is not None:
                err_tail.append(proc.stderr.read() or "")
        except Exception:  # noqa: BLE001
            pass
    if rc != 0:
        msg = "\n".join(err_tail)[-2000:]
        log.error("ffmpeg failed (%d):\n%s", rc, msg)
        raise RuntimeError(
            f"ffmpeg returned {rc}: "
            f"{(msg.splitlines() or ['no output'])[-1]}"
        )


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
