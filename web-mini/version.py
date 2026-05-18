"""Mini-app version, separate from the desktop binary version.

Why a separate version file:
    The desktop app and the mini-app live in the same repo but ship
    on completely different cadences. Desktop is semver-style:
    cut a binary release, attach AppImage / .exe / source tarball,
    users download a specific version that stays installed forever.
    Mini is continuously deployed: every commit that touches
    `web-mini/` rolls out to the HF Space and the live URL serves
    whatever's there. There's no concept of "I'm running mini
    v0.4.16.4" because users don't pick a version - they hit the URL
    and get whatever's live RIGHT NOW.

    Pretending these two surfaces share a version number was
    confusing in practice: every mini-only hotfix bumped a number
    that desktop users see when they look at the About box, and
    every desktop release bumped a number that mini users see in
    the eyebrow. From here on they diverge:

      * Desktop:  semver (`APP_VERSION` in `censor/version.py`)
                  e.g. `0.4.16-alpha`. Bumps only when desktop
                  code actually changes.
      * Mini:     CalVer (`MINI_VERSION` here)
                  format `YYYY.MM.DD.N[-suffix]` where N is the
                  per-day deploy counter. e.g. `2026.05.16.1-alpha`
                  is the first mini deploy on May 16, 2026.

    The CalVer choice is deliberate. Mini doesn't have a "version"
    in the SemVer sense - there are no breaking-change boundaries
    because there are no clients pinned to a version. CalVer is
    honest about what the number actually represents: "the build
    that went live on this date".

How to bump this:
    1. Set MINI_VERSION below to today's CalVer with the right
       counter. If this is the second mini deploy today, increment
       the trailing N (e.g. 2026.05.16.2-alpha).
    2. Add a CHANGELOG entry under a new
       `## [mini-YYYY.MM.DD.N-alpha]` heading scoped to web-mini.
    3. Commit. Tag the commit with the same string (`git tag -a
       mini-YYYY.MM.DD.N-alpha -m "..."`). The tag prefix
       differentiates from desktop tags (`desktop-v*`); CI
       workflows for AppImage / Windows .exe filter on
       `desktop-v*` so a mini-only tag won't accidentally
       trigger a desktop binary build.
    4. Run `scripts/sync-gh-pages.sh` to push the new MINI_VERSION
       to the website eyebrow alongside the (unchanged) desktop
       version.
    5. Run `scripts/deploy-mini.py` to push to the HF Space.

The `-alpha` suffix is the project-wide stage marker. Drop it when
the project as a whole moves out of alpha; it's not specific to
mini. Both APP_VERSION and MINI_VERSION carry the same suffix at
any given time.
"""

MINI_VERSION = "2026.05.18.11-alpha"
