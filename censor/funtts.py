"""Retro robotic TTS for the 'fun' censor mode.

We shell out to `espeak-ng` (or `espeak` as a fallback) using one of the
Klatt formant voice variants. The Klatt family is a classic formant
synthesizer first published in the early 80s; it gives the output that
unmistakable 1995-talking-computer character, and the only third-party
asset we depend on is espeak-ng itself.

Each flagged interval gets its own short WAV clip generated up-front,
then the audio renderer (`censor.audio.render_censored_fun`) mixes those
clips on top of the silenced original through a single ffmpeg pass.
"""

from __future__ import annotations

import logging
import random
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from . import elevenlabs_tts

_log = logging.getLogger(__name__)


# Klatt formant variants give that classic talking-computer timbre.
# `+klatt5` is a low/male voice which fits casual swears best.
_DEFAULT_VOICE = "en+klatt5"

# Six livestream-style ElevenLabs voices first (so they're the
# recommended pick when an API key is set), then the espeak fallbacks.
# Each row: (id stored in config / CensorOptions, UI label, espeak-ng
# `-v` argument used as fallback if the online voice fails).
FUN_VOICES: tuple[tuple[str, str, str], ...] = (
    # Online (ElevenLabs) - the espeak voice is only used if synth fails.
    *tuple((cid, label, "en+klatt5") for cid, label, _ in elevenlabs_tts.ELEVENLABS_VOICES),
    # Offline (espeak-ng).
    ("klatt5", "Classic Klatt", "en+klatt5"),
    ("klatt6", "Bright Klatt", "en+klatt6"),
    ("klatt4", "Deep Klatt", "en+klatt4"),
    ("klatt2", "Alt Klatt", "en+klatt2"),
    ("en_us", "US English", "en-us"),
    ("en_gb", "UK English", "en-gb"),
    ("scot", "Scotland", "en-gb-scotland"),
    ("rp", "RP English", "en-gb-x-rp"),
    ("lancs", "Lancashire", "en-gb-x-gbclan"),
    ("wmids", "West Midlands", "en-gb-x-gbcwmd"),
)

# Default = first offline voice (Classic Klatt). Online voices need a
# user-supplied API key, so we don't make them the default.
DEFAULT_FUN_VOICE_ID: str = "klatt5"


def fun_voice_labels(elevenlabs_key: bool = False) -> list[str]:
    """Human-readable labels in FUN_VOICES order.

    When ``elevenlabs_key`` is False the online voices get a "(needs
    API key)" suffix so users know why they fall back to the offline
    voice. The labels stay otherwise identical so the index <-> id
    helpers below keep working unchanged.
    """
    out: list[str] = []
    for vid, label, _ in FUN_VOICES:
        if vid.startswith("eleven_") and not elevenlabs_key:
            out.append(f"{label} (needs API key)")
        else:
            out.append(label)
    return out


def fun_voice_ids() -> list[str]:
    return [vid for vid, _, _ in FUN_VOICES]


def espeak_voice_for_choice(choice_id: str) -> str:
    """Map a stored voice id to an espeak-ng voice string."""
    for vid, _, esp in FUN_VOICES:
        if vid == choice_id:
            return esp
    return _DEFAULT_VOICE


def fun_voice_index_for_id(choice_id: str) -> int:
    for i, (vid, _, _) in enumerate(FUN_VOICES):
        if vid == choice_id:
            return i
    return 0


def fun_voice_id_at_index(index: int) -> str:
    if 0 <= index < len(FUN_VOICES):
        return FUN_VOICES[index][0]
    return DEFAULT_FUN_VOICE_ID
# Slightly faster than the espeak-ng default (175) so short PG words fit
# inside short intervals without obvious cut-offs.
_DEFAULT_SPEED = "200"
# Amplitude (0-200). Default is 100; bump to 150 so the TTS sits clearly
# above silence in the mixed output without obvious clipping.
_DEFAULT_AMPLITUDE = "150"


# Short, monosyllabic PG substitutes. Used for short flagged intervals
# (typical for words like "fuck", "ass", "damn").
PG_WORDS_SHORT: tuple[str, ...] = (
    "fudge", "darn", "dang", "heck", "shoot", "crud", "rats",
    "drat", "blast", "gosh", "yikes", "oof", "shucks", "phooey",
    "biscuits", "bother", "nuts", "bummer", "good grief", "oh dear",
)

# Multi-word phrases for longer intervals (laughs, drawn-out yelling).
PG_WORDS_LONG: tuple[str, ...] = (
    "fiddlesticks", "horsefeathers", "balderdash", "poppycock",
    "good gravy", "for crying out loud", "oh my goodness",
    "holy moly", "what the heck", "you doodle", "by jingo",
    "great scott", "jiminy cricket", "well I never",
)


def _engine() -> str | None:
    """Return the path to a TTS binary on PATH, preferring espeak-ng."""
    for name in ("espeak-ng", "espeak"):
        path = shutil.which(name)
        if path:
            return path
    return None


def is_available() -> bool:
    """True if a usable TTS engine is installed."""
    return _engine() is not None


# Public so the UI can show a friendlier install hint.
INSTALL_HINT = (
    "'Fun' mode needs espeak-ng. Install it with:\n"
    "    sudo apt install espeak-ng\n"
    "or on Fedora:\n"
    "    sudo dnf install espeak-ng"
)


def pick_pg_word(duration: float, rng: random.Random | None = None) -> str:
    """Pick a PG substitute sized to the target duration.

    Short intervals (<0.45s) only get monosyllables. Mid intervals get a
    mix. Long intervals can use full silly phrases, which sound funnier
    when there's room for them to play out.
    """
    r = rng or random
    if duration < 0.45:
        return r.choice(PG_WORDS_SHORT)
    if duration < 1.2:
        return r.choice(PG_WORDS_SHORT + PG_WORDS_LONG[:4])
    return r.choice(PG_WORDS_LONG)


def generate_tts(
    word: str,
    output_path: Path,
    voice: str = _DEFAULT_VOICE,
    speed: str = _DEFAULT_SPEED,
    amplitude: str = _DEFAULT_AMPLITUDE,
    *,
    voice_id: Optional[str] = None,
    elevenlabs_api_key: Optional[str] = None,
) -> None:
    """Render `word` to a WAV at `output_path`.

    When ``voice_id`` matches an ElevenLabs voice and an API key is
    supplied, we route through the online TTS service (with on-disk
    caching). On *any* failure of the online path we silently fall back
    to espeak-ng with ``voice``, so the job still completes even if the
    user has no internet, the key is invalid, or ElevenLabs has hiccups.

    Raises RuntimeError only when both paths fail (e.g. espeak-ng not
    installed and the online call also failed).
    """
    if voice_id and voice_id.startswith("eleven_") and elevenlabs_api_key:
        try:
            elevenlabs_tts.synthesize(
                text=word,
                internal_voice_id=voice_id,
                api_key=elevenlabs_api_key,
                output_path=output_path,
            )
            return
        except elevenlabs_tts.ElevenLabsError as e:
            _log.warning(
                "ElevenLabs synth for %r failed (%s); falling back to espeak.",
                word, e,
            )

    engine = _engine()
    if engine is None:
        raise RuntimeError(INSTALL_HINT)

    cmd = [
        engine,
        "-v", voice,
        "-s", speed,
        "-a", amplitude,
        "-w", str(output_path),
        word,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        # Some espeak builds don't ship the klatt variants. Retry once
        # with the plain Klatt-less English voice so the feature still
        # works (just sounds a touch less Sam-y).
        if voice != "en":
            return generate_tts(
                word, output_path, voice="en", speed=speed,
                amplitude=amplitude,
                voice_id=voice_id,
                elevenlabs_api_key=None,  # already failed; don't loop
            )
        raise RuntimeError(
            f"TTS failed for {word!r}:\n{proc.stderr.strip() or proc.stdout.strip()}"
        )
