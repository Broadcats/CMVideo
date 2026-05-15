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

import random
import shutil
import subprocess
from pathlib import Path


# Klatt formant variants give that classic talking-computer timbre.
# `+klatt5` is a low/male voice which fits casual swears best.
_DEFAULT_VOICE = "en+klatt5"
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
) -> None:
    """Render `word` to a WAV at `output_path`.

    Raises RuntimeError if no TTS engine is installed, or if espeak-ng
    fails for some reason (e.g. invalid voice).
    """
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
            return generate_tts(word, output_path, voice="en", speed=speed,
                                amplitude=amplitude)
        raise RuntimeError(
            f"TTS failed for {word!r}:\n{proc.stderr.strip() or proc.stdout.strip()}"
        )
