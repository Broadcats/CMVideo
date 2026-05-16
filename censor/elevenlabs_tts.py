"""ElevenLabs livestream-style TTS for the 'fun' censor mode.

The audio renderer in :mod:`censor.audio` mixes one short WAV per
flagged interval. We expose a single :func:`synthesize` that returns
the WAV path for a given (voice_id, text) combo, hitting a small disk
cache first so the same PG word never goes to the network twice.

ElevenLabs only ships MP3, so we re-encode to a 16 kHz mono WAV via
ffmpeg before returning. The cache key is ``sha1(voice_id + text +
voice_settings)`` so stability + privacy + offline replay all come for
free.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


# ElevenLabs voice IDs are stable; these six are the most-used voices on
# Twitch / TikTok donation TTS panels (Streamlabs default + StreamElements
# default + the four "iconic" donation reads). Picked deliberately so
# they sound familiar to anyone who has watched a stream in the last
# five years.
ELEVENLABS_VOICES: tuple[tuple[str, str, str], ...] = (
    # (internal id, label, ElevenLabs voice_id)
    ("eleven_brian",  "ElevenLabs - Brian (Streamlabs)", "nPczCjzI2devNBz1zQrb"),
    ("eleven_adam",   "ElevenLabs - Adam (Donation)",    "pNInz6obpgDQGBDnEUPy"),
    ("eleven_sam",    "ElevenLabs - Sam",                "yoZ06aMxZJJ28mfd3POQ"),
    ("eleven_rachel", "ElevenLabs - Rachel",             "21m00Tcm4TlvDq8ikWAM"),
    ("eleven_antoni", "ElevenLabs - Antoni",             "ErXwobaYiN019PkySvjV"),
    ("eleven_domi",   "ElevenLabs - Domi",               "AZnzlk1XvdvUeBnXmlld"),
)

# Same default settings ElevenLabs uses on the dashboard. Stability low
# enough to keep livestream-energy reads, similarity high so the voice
# stays recognisable across short PG words.
DEFAULT_VOICE_SETTINGS = {
    "stability": 0.35,
    "similarity_boost": 0.75,
    "style": 0.0,
    "use_speaker_boost": True,
}

DEFAULT_MODEL = "eleven_monolingual_v1"

API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_TIMEOUT_S = 25.0


class ElevenLabsError(RuntimeError):
    """Raised when synth fails. Caller should fall back to espeak."""


def voice_id_for(internal_id: str) -> Optional[str]:
    """Map ``eleven_brian`` -> ElevenLabs voice id, or None if unknown."""
    for cid, _label, vid in ELEVENLABS_VOICES:
        if cid == internal_id:
            return vid
    return None


def is_eleven_id(internal_id: str) -> bool:
    return internal_id.startswith("eleven_")


def labels() -> list[tuple[str, str]]:
    """[(internal_id, label), ...]."""
    return [(cid, label) for cid, label, _ in ELEVENLABS_VOICES]


def _cache_root(override: Optional[Path] = None) -> Path:
    if override is not None:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "cmvideo" / "tts_cache"


def _cache_key(voice_id: str, text: str, settings: dict) -> str:
    payload = json.dumps(
        {"v": voice_id, "t": text, "s": settings, "m": DEFAULT_MODEL},
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def _cache_path(cache_root: Path, voice_id: str, key: str) -> Path:
    bucket = cache_root / voice_id
    bucket.mkdir(parents=True, exist_ok=True)
    return bucket / f"{key}.wav"


def _ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise ElevenLabsError("ffmpeg is not on PATH; cannot decode ElevenLabs MP3.")
    return path


def _mp3_to_wav(mp3_bytes: bytes, dest: Path) -> None:
    """Decode the ElevenLabs MP3 stream to a 16 kHz mono WAV."""
    with tempfile.NamedTemporaryFile(
        suffix=".mp3", delete=False
    ) as src_file:
        src_file.write(mp3_bytes)
        src_path = src_file.name
    try:
        cmd = [
            _ffmpeg(),
            "-y",
            "-loglevel", "error",
            "-i", src_path,
            "-ar", "22050",
            "-ac", "1",
            str(dest),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ElevenLabsError(
                f"ffmpeg failed to decode ElevenLabs MP3: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
    finally:
        try:
            os.unlink(src_path)
        except OSError:
            pass


def _post_synth(
    api_key: str,
    voice_id: str,
    text: str,
    settings: dict,
    timeout_s: float,
) -> bytes:
    url = f"{API_BASE}/text-to-speech/{voice_id}"
    body = {
        "text": text,
        "model_id": DEFAULT_MODEL,
        "voice_settings": settings,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
            "User-Agent": "CMVideo/0.4.6 (+https://cmvideo.online)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        # Surface the API's error JSON if it sent one (rate limit etc).
        try:
            payload = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            payload = ""
        raise ElevenLabsError(
            f"ElevenLabs HTTP {e.code}: {payload[:300]}"
        ) from e
    except urllib.error.URLError as e:
        raise ElevenLabsError(f"ElevenLabs network error: {e.reason!r}") from e


def synthesize(
    *,
    text: str,
    internal_voice_id: str,
    api_key: str,
    output_path: Path,
    cache_root: Optional[Path] = None,
    voice_settings: Optional[dict] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Path:
    """Render ``text`` for ``internal_voice_id`` to ``output_path``.

    Returns the path actually written. Caches the result so repeat
    requests for the same word + voice don't touch the network.

    Raises :class:`ElevenLabsError` on any failure (no key, voice id
    unknown, API error, ffmpeg failure). The pipeline should catch
    that and fall back to espeak-ng so the job still succeeds.
    """
    if not text or not text.strip():
        raise ElevenLabsError("ElevenLabs synth called with empty text.")
    if not api_key or not api_key.strip():
        raise ElevenLabsError("ElevenLabs API key is not configured.")

    voice_id = voice_id_for(internal_voice_id)
    if voice_id is None:
        raise ElevenLabsError(f"Unknown ElevenLabs voice {internal_voice_id!r}.")

    settings = dict(DEFAULT_VOICE_SETTINGS)
    if voice_settings:
        settings.update(voice_settings)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cache_dir = _cache_root(cache_root)
    key = _cache_key(voice_id, text, settings)
    cached = _cache_path(cache_dir, voice_id, key)

    if cached.is_file() and cached.stat().st_size > 0:
        # Hard-link (POSIX) or copy into place. The renderer wants the
        # WAV at exactly `output_path`.
        try:
            if output_path.exists():
                output_path.unlink()
            os.link(cached, output_path)
        except OSError:
            shutil.copyfile(cached, output_path)
        return output_path

    mp3 = _post_synth(api_key.strip(), voice_id, text, settings, timeout_s)

    # Write to cache first, then copy to the requested output path.
    _mp3_to_wav(mp3, cached)
    try:
        if output_path.exists():
            output_path.unlink()
        os.link(cached, output_path)
    except OSError:
        shutil.copyfile(cached, output_path)
    return output_path


def has_api_key() -> bool:
    """Convenience for the UI / settings dialog. Reads env var only;
    the desktop GUI also stores the key in :func:`censor.config_store`
    and passes it explicitly via :func:`synthesize`."""
    return bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())
