"""Desktop CMVideo version string.

Single source of truth for the SEMVER version of the desktop
binary releases (AppImage / Windows .exe / source tarball /
About-box / window title / copyright string). NOT shared with
the mini-app - the mini ships continuously and uses CalVer in
`web-mini/version.py` (see `MINI_VERSION` and the policy
docstring there).

Bump this only when the desktop tree actually changes (anything
under `app.py`, `censor/`, `scripts/build-*`, or the bundled
tools). Mini-only patches must NOT bump this.

History note: 0.4.16.1 through 0.4.16.4-alpha shipped before
this split landed and bumped this number despite being
mini-only. Those tags + their auto-built AppImages exist on
GitHub for continuity, but treat them as functionally
equivalent to v0.4.16-alpha. Going forward only real desktop
changes bump APP_VERSION; the next desktop release is v0.4.17-alpha.

Tag format for desktop releases going forward:
    desktop-v0.X.Y[.Z]-alpha
The `desktop-v` prefix differentiates from mini tags
(`mini-YYYY.MM.DD.N-alpha`); the AppImage / Windows .exe CI
workflows filter on `desktop-v*` (and legacy `v*`) so a
mini-only tag push won't accidentally trigger a desktop binary
build.
"""

from __future__ import annotations

APP_VERSION = "0.4.16.4-alpha"
