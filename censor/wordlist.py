"""Wordlist loading, token normalization, and interval matching.

Two matchers live here:

* `find_flagged`            - exact, normalized token match. Cheap, predictable.
* `compile_fuzzy_pattern` + `find_flagged_fuzzy` - regex-based matcher that
  tolerates inflections (fucks/fucking/fucker), stretched letters
  (fuuuck), common phonetic swaps (phuck, kunt), and basic leet
  substitutions (@/4=a, 1/!=i, 0=o, $/5=s, 3=e, 7=t). It still
  full-matches each whole token, so 'Scunthorpe' won't trip 'cunt'.

The fuzzy path costs a little more CPU but is bounded by the size of
your wordlist; compilation happens once per pipeline run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_PUNCT_STRIP = " \t\n\r.,!?\"'`()[]{}:;<>/\\*~_-"


def _normalize(token: str) -> str:
    """Lowercase and strip surrounding punctuation/whitespace."""
    return token.strip(_PUNCT_STRIP).lower()


def load_wordlist(path: Path) -> set[str]:
    """Load a wordlist file. Returns a set of normalized lowercase words.

    Lines starting with '#' and blank lines are ignored.
    """
    words: set[str] = set()
    if not path.exists():
        return words
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        norm = _normalize(line)
        if norm:
            words.add(norm)
    return words


@dataclass
class FlaggedWord:
    word: str
    start: float
    end: float


def find_flagged(
    transcribed: Iterable[tuple[str, float, float]],
    wordset: set[str],
) -> list[FlaggedWord]:
    """Return the transcribed words whose normalized form is in `wordset`."""
    flagged: list[FlaggedWord] = []
    for word, start, end in transcribed:
        if _normalize(word) in wordset:
            flagged.append(FlaggedWord(word=word, start=start, end=end))
    return flagged


# ---------- Fuzzy matcher ----------

# For each base letter, the set of glyphs that may stand in for it. The
# letter itself is always included. These cover common leetspeak and the
# placeholder characters people use in casual censorship (*, -, _, .).
_LEET_MAP: dict[str, str] = {
    "a": "a@4",
    "b": "b8",
    "c": "c(",
    "e": "e3",
    "g": "g9",
    "h": "h#",
    "i": "i1!|l",
    "l": "l1!|",
    "o": "o0",
    "s": "s5$",
    "t": "t7+",
    "z": "z2",
}

# Characters that act as a wildcard for ANY letter (so 'f*ck' matches
# 'fuck'). They get added to every character class.
_WILDCARDS = "*-_."

# Common English inflections appended after the root. Empty match too.
_INFLECTIONS = r"(?:s|es|ed|er|ers|ing|in|y|ies)?"


def _letter_class(ch: str) -> str:
    """Return a regex character class that matches `ch` plus its leet
    equivalents plus the wildcard placeholders, repeated 1+ times to
    tolerate stretched letters (fuuuck).

    NOTE: hand-builds the class body because `re.escape` produces `\\-`
    which Python's regex engine treats as a literal `-` mid-class, and
    then interprets `\\*\\-_` as the range `*` through `_` (matching
    everything from ASCII 42 to 95, including uppercase letters).
    Inside `[...]` only `]`, `\\`, `^` need escaping; `-` is safe iff
    placed at the start or end of the class body.
    """
    alts = _LEET_MAP.get(ch, ch) + _WILDCARDS
    seen: dict[str, None] = {}
    for c in alts:
        seen.setdefault(c, None)
    has_dash = "-" in seen
    body_parts: list[str] = []
    for c in seen:
        if c == "-":
            continue
        if c in ("]", "\\", "^"):
            body_parts.append("\\" + c)
        else:
            body_parts.append(c)
    body = "".join(body_parts)
    if has_dash:
        body += "-"
    return "[" + body + "]+"


def _word_to_fuzzy_source(word: str) -> str:
    """Build a regex SOURCE (un-anchored) for one wordlist entry.

    Handles four bidirectional phonetic swaps as alternations:
      seed 'ph' <-> input 'f'   (e.g. seed 'phone' matches 'fone')
      seed 'f'  <-> input 'ph'  (e.g. seed 'fuck'  matches 'phuck')
      seed 'ck' <-> input 'k'   (e.g. seed 'fuck'  matches 'fuk')
      seed 'k'  <-> input 'ck'  (e.g. seed 'kunt'  matches 'ckunt' -- rare)
      seed 'c'  <-> input 'k'   (e.g. seed 'cunt'  matches 'kunt')
    Combined with leet substitutions per letter, this covers the
    overwhelming majority of casual obfuscation in spoken transcripts.
    """
    parts: list[str] = []
    i = 0
    n = len(word)
    while i < n:
        # Two-letter (digraph) seed positions get checked first so we
        # don't fragment 'ph' or 'ck' into separate letter slots.
        di = word[i : i + 2].lower()
        if di == "ph":
            parts.append(
                "(?:" + _letter_class("p") + _letter_class("h")
                + "|" + _letter_class("f") + ")"
            )
            i += 2
            continue
        if di == "ck":
            parts.append(
                "(?:" + _letter_class("c") + _letter_class("k")
                + "|" + _letter_class("k") + ")"
            )
            i += 2
            continue

        ch = word[i].lower()
        if ch == "f":
            # 'f' may be spelled 'ph' in the input.
            parts.append(
                "(?:" + _letter_class("f")
                + "|p" + _letter_class("h") + ")"
            )
        elif ch == "c":
            # 'c' may be spelled 'k' in the input (cunt -> kunt).
            parts.append(
                "(?:" + _letter_class("c") + "|" + _letter_class("k") + ")"
            )
        elif ch == "k":
            # bare 'k' may be spelled 'ck' in the input.
            parts.append(
                "(?:" + _letter_class("k") + "|c" + _letter_class("k") + ")"
            )
        else:
            parts.append(_letter_class(ch))
        i += 1
    return "".join(parts) + _INFLECTIONS


def compile_fuzzy_pattern(wordset: Iterable[str]) -> re.Pattern[str] | None:
    """Compile a single big alternation pattern over the wordlist.

    Returns None for an empty wordset. The pattern is intended for
    `fullmatch` against single tokens (lowercased, punctuation-stripped).
    """
    sources = [_word_to_fuzzy_source(w) for w in wordset if w]
    if not sources:
        return None
    # Sort longest-first so the engine prefers longer matches when there
    # are overlaps (e.g. 'motherfucker' vs 'fucker').
    sources.sort(key=len, reverse=True)
    pattern = "(?:" + "|".join(sources) + ")"
    return re.compile(pattern, re.IGNORECASE)


def find_flagged_fuzzy(
    transcribed: Iterable[tuple[str, float, float]],
    pattern: re.Pattern[str],
) -> list[FlaggedWord]:
    """Return transcribed words whose normalized token fully matches the
    fuzzy `pattern`. Word boundaries are implicit: we full-match each
    token so 'Scunthorpe' never matches 'cunt'."""
    flagged: list[FlaggedWord] = []
    for word, start, end in transcribed:
        token = _normalize(word)
        if token and pattern.fullmatch(token) is not None:
            flagged.append(FlaggedWord(word=word, start=start, end=end))
    return flagged


def merge_intervals(
    flagged: list[FlaggedWord],
    pad: float = 0.05,
) -> list[tuple[float, float]]:
    """Pad each flagged word by `pad` seconds on each side and merge overlaps.

    Returns sorted, non-overlapping (start, end) intervals.
    """
    if not flagged:
        return []
    padded = sorted(
        (max(0.0, f.start - pad), f.end + pad) for f in flagged
    )
    merged: list[tuple[float, float]] = [padded[0]]
    for start, end in padded[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged
