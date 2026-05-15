"""High-level pipeline: extract -> transcribe -> match -> render."""

from __future__ import annotations

import gc
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import audio, download, funtts, transcribe, wordlist


if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    WORDLISTS_DIR = Path(sys._MEIPASS) / "wordlists"
else:
    WORDLISTS_DIR = Path(__file__).resolve().parent.parent / "wordlists"


@dataclass
class CensorOptions:
    remove_swears: bool = True
    remove_slurs: bool = True
    mode: str = "silence"
    model_size: str = "small"
    padding_seconds: float = 0.05
    save_transcript: bool = False
    # Fuzzy / leet matcher. Off by default - exact token match is faster
    # and more conservative.
    fuzzy_matching: bool = False
    # Used when a URL is supplied as the source.
    download_format: str = "mp4"  # mp4 / mov / mp3 / wav / ogg
    # Where to save all final artefacts (downloads, censored media,
    # transcripts). None = auto: ~/Downloads for URLs, next to the input
    # file for local jobs.
    output_dir: Path | None = None
    # Quality knobs. The video one is used when downloading MP4/MOV;
    # the audio one when downloading MP3/OGG.
    video_quality: str = "Best"     # Best / 1080p / 720p / 480p / 360p
    audio_quality: str = "192"      # kbps as a string; "0" means best
    # Optional Netscape-format cookies file, forwarded to yt-dlp as
    # `cookiefile`. Used to authenticate against login-gated sites.
    # Ignored for local-file jobs.
    cookies_file: Path | None = None


@dataclass
class CensorResult:
    """Result of a pipeline run.

    `output_path` is None in transcript-only mode (where the user un-ticked
    both Swears and Slurs and only asked for a transcript).
    """
    output_path: Path | None
    transcript_path: Path | None
    flagged_count: int


# stage in {"download", "extract", "transcribe", "match", "render",
#           "transcript", "done"}
ProgressCb = Callable[[str, float, str], None]


def _emit(cb: ProgressCb | None, stage: str, frac: float, msg: str) -> None:
    if cb is not None:
        cb(stage, frac, msg)


def _format_timestamp(seconds: float) -> str:
    """HH:MM:SS.mmm timestamp for transcript lines."""
    hours = int(seconds // 3600)
    seconds -= hours * 3600
    minutes = int(seconds // 60)
    seconds -= minutes * 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def _write_transcript(
    transcript_path: Path,
    words: list[tuple[str, float, float]],
    flagged_set: set[tuple[float, float]],
) -> None:
    """Write an UNCENSORED transcript with timestamps, grouped per sentence.

    Words that the censor would remove are marked with a [*] suffix so you
    can see what the app caught without obscuring the text.
    """
    lines: list[str] = []
    lines.append("# Uncensored transcript")
    lines.append("# Format: [HH:MM:SS.mmm -> HH:MM:SS.mmm] text")
    lines.append("# Words marked with [*] are the ones the censor flagged.")
    lines.append("")

    if not words:
        lines.append("(no speech detected)")
        transcript_path.write_text("\n".join(lines), encoding="utf-8")
        return

    # Group words into pseudo-sentences with up to ~6s gap or punctuation breaks.
    current: list[tuple[str, float, float]] = []
    current_start = words[0][1]
    last_end = words[0][2]

    def flush() -> None:
        if not current:
            return
        parts: list[str] = []
        for w, s, e in current:
            tag = " [*]" if (s, e) in flagged_set else ""
            parts.append(f"{w}{tag}")
        lines.append(
            f"[{_format_timestamp(current_start)} -> {_format_timestamp(last_end)}] "
            + " ".join(parts)
        )

    for word, start, end in words:
        if current and (start - last_end > 1.5 or current[-1][0].endswith((".", "!", "?"))):
            flush()
            current = []
            current_start = start
        current.append((word, start, end))
        last_end = end
    flush()

    transcript_path.write_text("\n".join(lines), encoding="utf-8")


def run(
    input_path: Path | None,
    output_path: Path | None,
    options: CensorOptions,
    progress: ProgressCb | None = None,
    url: str | None = None,
) -> CensorResult:
    """Run the pipeline. Supports four modes:

    1. Censor mode: at least one of Swears/Slurs ticked. Transcribes,
       finds flagged words, renders the censored output, optionally
       writes a transcript.
    2. Transcript-only mode: neither Swears nor Slurs ticked, but
       `save_transcript=True`. Transcribes and writes the .txt only.
    3. Download mode: URL supplied, no censor/transcript options. Just
       downloads the file via yt-dlp and stops.
    4. Download + (censor or transcript): combinations of the above.

    `input_path` is mutually exclusive with `url`: supply exactly one.
    """
    if url and input_path:
        raise RuntimeError("Provide a URL or a local file, not both.")
    if not url and not input_path:
        raise RuntimeError("No input: drop a file or paste a URL.")

    # Stage 0: download (only if URL).
    if url:
        dl_dir = options.output_dir or download.default_download_dir()
        _emit(progress, "download", 0.0, "Starting download...")

        def _dlcb(frac: float, msg: str) -> None:
            _emit(progress, "download", frac, msg)

        try:
            downloaded = download.download(
                url=url,
                output_dir=dl_dir,
                fmt=options.download_format,
                video_quality=options.video_quality,
                audio_quality=options.audio_quality,
                cookies_file=options.cookies_file,
                progress_cb=_dlcb,
            )
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(str(e)) from e
        _emit(progress, "download", 1.0, f"Downloaded: {downloaded.name}")
        input_path = downloaded

        # Pure download mode - nothing else requested.
        if not (
            options.remove_swears
            or options.remove_slurs
            or options.save_transcript
        ):
            _emit(progress, "done", 1.0, "Done!")
            return CensorResult(
                output_path=downloaded,
                transcript_path=None,
                flagged_count=0,
            )

        # When following up with censoring, default output path lives
        # next to the downloaded file unless caller overrode it.
        if output_path is None:
            output_path = auto_output_path(downloaded, options.output_dir)

    assert input_path is not None  # for type-checkers
    if not input_path.exists():
        raise RuntimeError(f"Input file does not exist: {input_path}")
    if not audio.has_audio_stream(input_path):
        raise RuntimeError("This file has no audio track.")

    wordset: set[str] = set()
    if options.remove_swears:
        wordset |= wordlist.load_wordlist(WORDLISTS_DIR / "swears.txt")
    if options.remove_slurs:
        wordset |= wordlist.load_wordlist(WORDLISTS_DIR / "slurs.txt")

    transcript_only = not wordset

    if transcript_only and not options.save_transcript:
        raise RuntimeError(
            "Nothing to do: tick at least one of Swears, Slurs, or "
            "Save transcript."
        )

    transcript_path: Path | None = None
    final_output_path: Path | None = None

    with tempfile.TemporaryDirectory(prefix="censor_") as tmp:
        wav_path = Path(tmp) / "audio.wav"

        _emit(progress, "extract", 0.0, "Extracting audio...")
        audio.extract_audio_wav(input_path, wav_path)
        _emit(progress, "extract", 1.0, "Audio extracted.")

        _emit(progress, "transcribe", 0.0, "Transcribing audio...")

        def _tcb(frac: float) -> None:
            _emit(progress, "transcribe", frac, f"Transcribing audio... {int(frac * 100)}%")

        words = transcribe.transcribe(
            str(wav_path),
            model_size=options.model_size,
            progress_cb=_tcb,
        )
        _emit(progress, "transcribe", 1.0, f"Transcribed {len(words)} words.")

        flagged: list[wordlist.FlaggedWord] = []
        intervals: list[tuple[float, float]] = []
        if not transcript_only:
            _emit(progress, "match", 0.0, "Finding matches...")
            if options.fuzzy_matching:
                fuzzy = wordlist.compile_fuzzy_pattern(wordset)
                flagged = (
                    wordlist.find_flagged_fuzzy(words, fuzzy)
                    if fuzzy is not None
                    else []
                )
            else:
                flagged = wordlist.find_flagged(words, wordset)
            intervals = wordlist.merge_intervals(flagged, pad=options.padding_seconds)
            _emit(
                progress, "match", 1.0,
                f"Found {len(flagged)} word(s) to censor.",
            )

        if options.save_transcript:
            _emit(progress, "transcript", 0.0, "Writing transcript...")
            if transcript_only:
                # No media output, so write the transcript wherever the
                # user asked (or next to the input by default).
                tr_dir = options.output_dir or input_path.parent
                tr_dir.mkdir(parents=True, exist_ok=True)
                transcript_path = tr_dir / f"{input_path.stem}_transcript.txt"
                n = 2
                while transcript_path.exists():
                    transcript_path = (
                        tr_dir / f"{input_path.stem}_transcript_{n}.txt"
                    )
                    n += 1
            else:
                transcript_path = output_path.with_name(
                    output_path.stem + "_transcript.txt"
                )
            flagged_set = {(f.start, f.end) for f in flagged}
            _write_transcript(transcript_path, words, flagged_set)
            _emit(
                progress, "transcript", 1.0,
                f"Transcript saved: {transcript_path.name}",
            )

        if not transcript_only:
            if options.mode == "fun":
                if not funtts.is_available():
                    raise RuntimeError(funtts.INSTALL_HINT)
                _emit(
                    progress, "render", 0.0,
                    f"Generating Microsoft Sam clips for {len(intervals)} word(s)...",
                )
                tts_dir = Path(tmp) / "tts"
                tts_dir.mkdir(exist_ok=True)
                clips: list[tuple[float, float, Path]] = []
                for i, (s, e) in enumerate(intervals):
                    word = funtts.pick_pg_word(e - s)
                    clip_path = tts_dir / f"tts_{i:04d}.wav"
                    funtts.generate_tts(word, clip_path)
                    clips.append((s, e, clip_path))
                    if intervals and i % 4 == 0:
                        _emit(
                            progress, "render",
                            0.3 * (i + 1) / len(intervals),
                            f"Generating TTS clips... ({i + 1}/{len(intervals)})",
                        )
                _emit(progress, "render", 0.35, "Mixing Microsoft Sam into audio...")
                audio.render_censored_fun(input_path, output_path, clips)
            else:
                _emit(
                    progress, "render", 0.0,
                    f"Censoring {len(flagged)} word(s)...",
                )
                audio.render_censored(
                    input_path, output_path, intervals, options.mode
                )
            _emit(progress, "render", 1.0, "Render complete.")
            final_output_path = output_path

    # Force-reap transient buffers (audio chunks, segment lists) before
    # the worker idles or moves on to the next batch item. transcribe.py
    # already drops the Whisper model in its finally block; this catches
    # anything else still pinned.
    gc.collect()

    _emit(progress, "done", 1.0, "Done!")
    return CensorResult(
        output_path=final_output_path,
        transcript_path=transcript_path,
        flagged_count=len(flagged),
    )


def auto_output_path(input_path: Path, output_dir: Path | None = None) -> Path:
    """Return `<stem>_clean<ext>` either in `output_dir` (if given) or
    next to the input, preserving the original extension and avoiding
    collisions with existing files."""
    parent = output_dir if output_dir is not None else input_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem
    ext = input_path.suffix or ".mp4"
    candidate = parent / f"{stem}_clean{ext}"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = parent / f"{stem}_clean_{n}{ext}"
        if not candidate.exists():
            return candidate
        n += 1
