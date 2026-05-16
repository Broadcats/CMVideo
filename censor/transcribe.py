"""Audio transcription with word-level timestamps via faster-whisper."""

from __future__ import annotations

import gc
import threading
from typing import Callable


# (word, start_seconds, end_seconds)
Word = tuple[str, float, float]


# ---------------------------------------------------------------------------
# Process-local model cache.
#
# Loading a faster-whisper model takes 1-2s for `small`, more for `medium`
# (model weights mmap'd from disk + ctranslate2 init). Re-creating it on
# every call wastes that cost on every job in a batch. We keep one
# per (model_size, device, compute_type) tuple in process memory.
# ---------------------------------------------------------------------------
_MODEL_CACHE: dict[tuple[str, str, str], object] = {}
_MODEL_CACHE_LOCK = threading.Lock()


def _get_or_create_model(model_size: str, device: str, compute_type: str, *, WhisperModel):
    """Return a cached `WhisperModel` for the given config, building one
    if necessary. Thread-safe: two callers asking for the same key
    concurrently will share the single instance."""
    key = (model_size, device, compute_type)
    with _MODEL_CACHE_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        _MODEL_CACHE[key] = model
        return model


def prewarm(model_size: str = "small", device: str = "cpu", compute_type: str = "int8") -> None:
    """Force the model to load now, so the first transcription job
    doesn't pay the load tax on the user's wall clock. Called from a
    daemon thread on app start. Failures are swallowed - if we can't
    pre-warm we'll just pay the cost lazily on the first real job."""
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]
        _get_or_create_model(model_size, device, compute_type, WhisperModel=WhisperModel)
    except Exception:  # noqa: BLE001
        pass


def clear_model_cache() -> None:
    """Drop all cached models. Forces a reload on the next transcribe.
    Called when the user changes model size in settings."""
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.clear()
    gc.collect()


def transcribe(
    wav_path: str,
    model_size: str = "small",
    device: str = "cuda",
    compute_type: str = "float16",
    language: str | None = None,
    progress_cb: Callable[[float], None] | None = None,
) -> list[Word]:
    """Transcribe a WAV file and return a list of (word, start, end) tuples.

    Tries CUDA first, then automatically falls back to CPU if the CUDA
    runtime libraries (libcublas, libcudnn) are missing or fail at any
    stage - including during inference, not just during model init.

    `progress_cb` is called with a float in [0.0, 1.0] as transcription
    proceeds, based on segment end time vs total audio duration.
    """
    from faster_whisper import WhisperModel

    backends = [
        (device, compute_type),
        ("cuda", "int8_float16"),
        ("cpu", "int8"),
    ]
    last_err: Exception | None = None

    for dev, ct in backends:
        try:
            return _run_once(
                wav_path=wav_path,
                model_size=model_size,
                device=dev,
                compute_type=ct,
                language=language,
                progress_cb=progress_cb,
                WhisperModel=WhisperModel,
            )
        except Exception as e:  # noqa: BLE001 - fall back on any backend error
            last_err = e
            # Reset progress so the bar doesn't look stuck across the retry.
            if progress_cb is not None:
                progress_cb(0.0)
            continue

    raise RuntimeError(
        f"Whisper failed on every backend (last error: {last_err}). "
        "Try running ./enable-gpu.sh to install CUDA runtime libraries, "
        "or check that audio is valid."
    )


def _run_once(
    *,
    wav_path: str,
    model_size: str,
    device: str,
    compute_type: str,
    language: str | None,
    progress_cb: Callable[[float], None] | None,
    WhisperModel,
) -> list[Word]:
    # Reuse the cached model when one already exists for this config.
    # Building the model (the slow part - ctranslate2 init + weight
    # mmap) happens lazily inside _get_or_create_model.
    model = _get_or_create_model(model_size, device, compute_type, WhisperModel=WhisperModel)
    try:
        segments_iter, info = model.transcribe(
            wav_path,
            word_timestamps=True,
            vad_filter=True,
            language=language,
        )

        duration = float(getattr(info, "duration", 0.0) or 0.0)
        words: list[Word] = []

        # IMPORTANT: iterate inside the same try-scope as the outer caller, so
        # any CUDA runtime error raised here triggers the CPU fallback.
        for segment in segments_iter:
            if progress_cb is not None and duration > 0:
                frac = max(0.0, min(0.99, float(segment.end) / duration))
                progress_cb(frac)
            if not segment.words:
                continue
            for w in segment.words:
                text = (w.word or "").strip()
                if not text:
                    continue
                words.append((text, float(w.start), float(w.end)))

        if progress_cb is not None:
            progress_cb(1.0)
        return words
    finally:
        # Explicitly release the ctranslate2 model so the CUDA context and
        # any held GPU memory get freed straight after transcription
        # instead of lingering until the next garbage cycle.
        try:
            del segments_iter  # type: ignore[possibly-undefined]
        except (NameError, UnboundLocalError):
            pass
        try:
            del info  # type: ignore[possibly-undefined]
        except (NameError, UnboundLocalError):
            pass
        del model
        gc.collect()
