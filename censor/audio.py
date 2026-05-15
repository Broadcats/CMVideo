"""ffmpeg wrappers: probe, extract, build censor filter, mux back."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


SUPPORTED_INPUT_EXTS = {".mp4", ".mov", ".mp3", ".wav", ".ogg"}


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise RuntimeError("ffmpeg not found on PATH. Install: sudo apt install ffmpeg")
    return path


def _ffprobe() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise RuntimeError("ffprobe not found on PATH. Install: sudo apt install ffmpeg")
    return path


def _has_stream(input_path: Path, stream_kind: str) -> bool:
    """stream_kind is 'a' (audio) or 'v' (video)."""
    result = subprocess.run(
        [
            _ffprobe(),
            "-v", "error",
            "-select_streams", stream_kind,
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            str(input_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip())


def has_audio_stream(input_path: Path) -> bool:
    return _has_stream(input_path, "a")


def has_video_stream(input_path: Path) -> bool:
    return _has_stream(input_path, "v")


def extract_audio_wav(input_path: Path, wav_path: Path) -> None:
    """Extract a 16kHz mono PCM WAV from `input_path` for transcription.
    Works for both audio-only inputs (MP3/WAV/OGG) and video inputs (MP4/MOV)."""
    cmd = [
        _ffmpeg(),
        "-y",
        "-i", str(input_path),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(wav_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed:\n{proc.stderr.strip()}")


def _between_expr(intervals: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in intervals)


def _audio_codec_args_for(output_path: Path) -> list[str]:
    """Pick a sensible audio codec + bitrate based on the output extension."""
    ext = output_path.suffix.lower()
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-b:a", "192k"]
    if ext == ".wav":
        return ["-c:a", "pcm_s16le"]
    if ext == ".ogg":
        return ["-c:a", "libvorbis", "-q:a", "5"]
    # .mp4 / .mov / anything else with video: AAC
    return ["-c:a", "aac", "-b:a", "192k"]


# Linux caps each argv string at MAX_ARG_STRLEN = 32 * PAGE_SIZE = 128 KB
# (`getconf MAX_ARG_STRLEN` is not exposed; the value is hard-coded in the
# kernel). A long filter graph (thousands of intervals or fun-mode TTS
# clips) easily overruns that limit, producing E2BIG ("Argument list too
# long"). Anything over this threshold gets written to a temp file and
# fed to ffmpeg via `-filter_complex_script` / `-filter_script:a`.
_ARG_STRING_SAFE_LIMIT = 100_000


def _write_filter_script(filter_text: str) -> str:
    """Write `filter_text` to a temp file and return its path."""
    fd, path = tempfile.mkstemp(prefix="cmvideo_filter_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(filter_text)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _run_ffmpeg(cmd: list[str], failure_msg: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{failure_msg}:\n{proc.stderr.strip()}")


def render_censored(
    input_path: Path,
    output_path: Path,
    intervals: list[tuple[float, float]],
    mode: str,
) -> None:
    """Write `output_path` = `input_path` with the given audio intervals censored.

    Handles both video (MP4/MOV: video stream copied, audio re-encoded)
    and audio-only (MP3/WAV/OGG) inputs. Output codec is picked from the
    output extension.
    """
    if mode not in ("silence", "beep"):
        # 'fun' uses render_censored_fun which needs pre-generated TTS
        # clips, so it doesn't share this code path.
        raise ValueError(f"Unknown censor mode: {mode!r}")

    has_video = has_video_stream(input_path)
    audio_codec = _audio_codec_args_for(output_path)

    if not intervals:
        _run_ffmpeg(
            [_ffmpeg(), "-y", "-i", str(input_path), "-c", "copy", str(output_path)],
            "ffmpeg copy failed",
        )
        return

    expr = _between_expr(intervals)
    script_path: str | None = None
    try:
        if mode == "silence":
            afilter = f"volume=enable='{expr}':volume=0"
            af_args: list[str]
            if len(afilter) > _ARG_STRING_SAFE_LIMIT:
                script_path = _write_filter_script(afilter)
                af_args = ["-filter_script:a", script_path]
            else:
                af_args = ["-af", afilter]
            base = [_ffmpeg(), "-y", "-i", str(input_path)]
            if has_video:
                cmd = base + [
                    "-map", "0:v:0",
                    "-map", "0:a:0",
                    "-c:v", "copy",
                    *af_args,
                    *audio_codec,
                    str(output_path),
                ]
            else:
                cmd = base + [*af_args, *audio_codec, str(output_path)]
        else:  # beep
            filter_complex = (
                f"[0:a]volume=enable='{expr}':volume=0[muted];"
                f"sine=frequency=1000:sample_rate=48000,"
                f"volume='if({expr},0.5,0)':eval=frame[beep];"
                f"[muted][beep]amix=inputs=2:duration=first:normalize=0[a]"
            )
            fc_args: list[str]
            if len(filter_complex) > _ARG_STRING_SAFE_LIMIT:
                script_path = _write_filter_script(filter_complex)
                fc_args = ["-filter_complex_script", script_path]
            else:
                fc_args = ["-filter_complex", filter_complex]
            base = [_ffmpeg(), "-y", "-i", str(input_path), *fc_args]
            if has_video:
                cmd = base + [
                    "-map", "0:v:0",
                    "-map", "[a]",
                    "-c:v", "copy",
                    *audio_codec,
                    str(output_path),
                ]
            else:
                cmd = base + ["-map", "[a]", *audio_codec, str(output_path)]

        _run_ffmpeg(cmd, "ffmpeg censor render failed")
    finally:
        if script_path is not None:
            try:
                os.unlink(script_path)
            except OSError:
                pass


def render_censored_fun(
    input_path: Path,
    output_path: Path,
    clips: list[tuple[float, float, Path]],
) -> None:
    """'Fun' mode: silence each interval and mix a TTS clip on top.

    `clips` is a list of (start, end, tts_wav_path). Each WAV is trimmed
    to the interval length (so an over-long TTS can't bleed past the
    silenced region), shifted to its start time, and then summed with
    the muted original through `amix` with normalization disabled.
    """
    has_video = has_video_stream(input_path)
    audio_codec = _audio_codec_args_for(output_path)

    if not clips:
        _run_ffmpeg(
            [_ffmpeg(), "-y", "-i", str(input_path), "-c", "copy", str(output_path)],
            "ffmpeg copy failed",
        )
        return

    intervals = [(s, e) for s, e, _ in clips]
    silence_expr = _between_expr(intervals)

    cmd = [_ffmpeg(), "-y", "-i", str(input_path)]
    for _, _, tts_path in clips:
        cmd.extend(["-i", str(tts_path)])

    # Filter graph - one chunk per TTS clip:
    #   atrim                clamp the clip to the silenced gap
    #   asetpts=PTS-STARTPTS rebase so adelay measures from the clip start
    #   adelay=N|N           shift to the clip's wall-clock position
    #   volume=1.6           espeak-ng output is quiet; nudge it above
    #                        the silenced original in the mix
    filter_parts = [f"[0:a]volume=enable='{silence_expr}':volume=0[muted]"]
    for i, (start, end, _) in enumerate(clips):
        delay_ms = max(0, int(round(start * 1000)))
        duration = max(0.05, end - start)
        filter_parts.append(
            f"[{i + 1}:a]"
            f"atrim=duration={duration:.3f},"
            f"asetpts=PTS-STARTPTS,"
            f"adelay={delay_ms}|{delay_ms},"
            f"volume=1.6"
            f"[tts{i}]"
        )

    inputs_chain = "[muted]" + "".join(f"[tts{i}]" for i in range(len(clips)))
    filter_parts.append(
        f"{inputs_chain}amix=inputs={len(clips) + 1}:"
        f"duration=first:normalize=0[a]"
    )
    filter_complex = ";".join(filter_parts)

    # Long filter graphs blow past the kernel's 128 KB per-argv-string
    # cap (E2BIG). Anything over the safe limit is sidestepped via
    # -filter_complex_script.
    script_path: str | None = None
    try:
        if len(filter_complex) > _ARG_STRING_SAFE_LIMIT:
            script_path = _write_filter_script(filter_complex)
            cmd.extend(["-filter_complex_script", script_path])
        else:
            cmd.extend(["-filter_complex", filter_complex])

        if has_video:
            cmd.extend([
                "-map", "0:v:0",
                "-map", "[a]",
                "-c:v", "copy",
                *audio_codec,
                str(output_path),
            ])
        else:
            cmd.extend(["-map", "[a]", *audio_codec, str(output_path)])

        _run_ffmpeg(cmd, "ffmpeg fun-render failed")
    finally:
        if script_path is not None:
            try:
                os.unlink(script_path)
            except OSError:
                pass
