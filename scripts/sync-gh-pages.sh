#!/usr/bin/env bash
# scripts/sync-gh-pages.sh
#
# Flatten `site/` from the current main commit and push it to the
# `gh-pages` branch so cmvideo.online serves the latest widget.
#
# The gh-pages branch is laid out as a flat snapshot of `site/`
# (index.html, app.js, style.css, etc. live at the branch root) so
# GitHub Pages can serve them at the apex domain. This means we
# CANNOT just `git push main:gh-pages` - that would replace the flat
# files with the full repo tree and the site would 404.
#
# What this script does:
#   1. Verifies the working tree is clean and on `main` (or override
#      with --branch).
#   2. Resolves the version string from censor/version.py.
#   3. Creates a throwaway worktree at /tmp/cmvideo-ghpages-$$
#      reset to origin/gh-pages (so we never touch the user's main
#      checkout).
#   4. Wipes the worktree contents EXCEPT for `.git` and any names
#      listed in PRESERVE (default: CNAME).
#   5. Copies `site/.` into the worktree root.
#   6. Commits with the project's standard "site: sync main@<sha>
#      (<version>)" message and fast-forward-pushes to origin.
#   7. If the push is rejected as non-fast-forward (someone else
#      pushed to gh-pages between fetch and push) it auto-rebases on
#      origin/gh-pages and retries once. Never force-pushes unless
#      --force is explicitly passed.
#   8. Cleans up the worktree on exit (success OR failure).
#
# Usage:
#   scripts/sync-gh-pages.sh                  # normal release
#   scripts/sync-gh-pages.sh --dry-run        # show plan, no commits
#   scripts/sync-gh-pages.sh --force          # force-push (use only
#                                             #   if the gh-pages
#                                             #   history is wrong)
#   PRESERVE="CNAME robots.txt" scripts/sync-gh-pages.sh
#
set -euo pipefail

# -----------------------------------------------------------------------------
# Args
# -----------------------------------------------------------------------------
DRY_RUN=0
FORCE_PUSH=0
SOURCE_BRANCH="main"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)   DRY_RUN=1 ;;
        --force)     FORCE_PUSH=1 ;;
        --branch)    SOURCE_BRANCH="${2:-}"; shift ;;
        -h|--help)
            sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "[sync-gh-pages] Unknown arg: $1" >&2
            exit 2
            ;;
    esac
    shift
done

# -----------------------------------------------------------------------------
# Locate repo root + sanity checks
# -----------------------------------------------------------------------------
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "${REPO_ROOT}" ]]; then
    echo "[sync-gh-pages] Not inside a git repository." >&2
    exit 1
fi
cd "${REPO_ROOT}"

if [[ ! -d "site" ]]; then
    echo "[sync-gh-pages] No 'site/' directory at repo root." >&2
    exit 1
fi
if [[ ! -f "site/index.html" ]]; then
    echo "[sync-gh-pages] site/index.html missing - refusing to publish an empty site." >&2
    exit 1
fi

CUR_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "${CUR_BRANCH}" != "${SOURCE_BRANCH}" ]]; then
    echo "[sync-gh-pages] On '${CUR_BRANCH}', expected '${SOURCE_BRANCH}'. Use --branch to override." >&2
    exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
    echo "[sync-gh-pages] Working tree is dirty. Commit or stash before syncing." >&2
    git status --short >&2
    exit 1
fi

# -----------------------------------------------------------------------------
# Version + commit metadata
# -----------------------------------------------------------------------------
SHA_SHORT="$(git rev-parse --short HEAD)"

# ---------------------------------------------------------------
# Version sources. Desktop + mini are versioned independently
# since v0.4.16.5-alpha (see censor/version.py and
# web-mini/version.py for the policy docstring). Each lives in
# its own file so a mini-only deploy doesn't touch the desktop
# version (and vice versa).
# ---------------------------------------------------------------
DESKTOP_VERSION_PY="censor/version.py"
MINI_VERSION_PY="web-mini/version.py"
APP_VERSION=""
MINI_VERSION=""
if [[ -f "${DESKTOP_VERSION_PY}" ]]; then
    APP_VERSION="$(grep -E '^APP_VERSION' "${DESKTOP_VERSION_PY}" | sed -E 's/.*"([^"]+)".*/\1/')"
fi
if [[ -f "${MINI_VERSION_PY}" ]]; then
    MINI_VERSION="$(grep -E '^MINI_VERSION' "${MINI_VERSION_PY}" | sed -E 's/.*"([^"]+)".*/\1/')"
fi
if [[ -z "${APP_VERSION}" ]]; then
    APP_VERSION="$(git describe --tags --match 'desktop-v*' --abbrev=0 2>/dev/null \
        || git describe --tags --match 'v*' --abbrev=0 2>/dev/null \
        || echo unknown)"
fi
if [[ -z "${MINI_VERSION}" ]]; then
    MINI_VERSION="$(git describe --tags --match 'mini-*' --abbrev=0 2>/dev/null \
        | sed 's/^mini-//' \
        || echo unknown)"
fi
COMMIT_MSG="site: sync ${SOURCE_BRANCH}@${SHA_SHORT} (desktop v${APP_VERSION} / mini ${MINI_VERSION})"

# Files at the gh-pages root that we want to keep across syncs even
# though they don't live in site/ (notably CNAME for the apex domain).
# Override with PRESERVE="CNAME robots.txt _headers" etc.
PRESERVE="${PRESERVE:-CNAME}"

# -----------------------------------------------------------------------------
# Set up worktree
# -----------------------------------------------------------------------------
WT_PATH="/tmp/cmvideo-ghpages-$$"
cleanup() {
    set +e
    if [[ -d "${WT_PATH}" ]]; then
        git worktree remove --force "${WT_PATH}" >/dev/null 2>&1 || rm -rf "${WT_PATH}"
    fi
    git worktree prune >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "[sync-gh-pages] Repo:        ${REPO_ROOT}"
echo "[sync-gh-pages] Source:      ${SOURCE_BRANCH}@${SHA_SHORT}"
echo "[sync-gh-pages] Desktop:     v${APP_VERSION}"
echo "[sync-gh-pages] Mini:        ${MINI_VERSION}"
echo "[sync-gh-pages] Preserving:  ${PRESERVE}"
echo "[sync-gh-pages] Worktree:    ${WT_PATH}"
echo "[sync-gh-pages] Commit msg:  ${COMMIT_MSG}"
[[ ${DRY_RUN}    -eq 1 ]] && echo "[sync-gh-pages] DRY RUN: no commits, no pushes."
[[ ${FORCE_PUSH} -eq 1 ]] && echo "[sync-gh-pages] FORCE PUSH enabled (only use to fix bad history)."
echo

git fetch origin gh-pages
git worktree add -B gh-pages "${WT_PATH}" origin/gh-pages >/dev/null

# -----------------------------------------------------------------------------
# Wipe + repopulate worktree
# -----------------------------------------------------------------------------
pushd "${WT_PATH}" >/dev/null

# Build a `find` exclusion clause from PRESERVE (space-separated).
FIND_ARGS=( -mindepth 1 -maxdepth 1 ! -name .git )
for keep in ${PRESERVE}; do
    FIND_ARGS+=( ! -name "${keep}" )
done
find . "${FIND_ARGS[@]}" -exec rm -rf {} +

# Copy site/. into the worktree root, preserving timestamps.
cp -a "${REPO_ROOT}/site/." .

# -----------------------------------------------------------------------------
# Auto-bump version stamps to the latest GitHub Release.
# -----------------------------------------------------------------------------
# index.html has two flavours of hardcoded version stamp:
#
#   1. COSMETIC display strings - the eyebrow line, the
#      "Get CMVideo X" h2, the alpha tag in the legal footer,
#      etc. These should always advertise the latest project tag
#      (even if it's a tag-only release with no new binaries -
#      e.g. mini / website / docs-only updates).
#
#   2. DOWNLOAD URLs + binary filenames - the per-OS dl-cards
#      and the chmod / install snippet. These have to point at
#      a release that actually has the AppImage / .exe / source
#      tarball assets attached. Bumping these to a tag-only
#      release would 404 every download button on the homepage.
#
# So we resolve TWO different versions:
#
#   LATEST_RELEASE       = whatever `gh release list --limit 1`
#                          returns. Used for cosmetic display.
#   LATEST_BINARY_RELEASE = most recent release whose asset list
#                          contains the AppImage. Used for download
#                          URL paths + filenames.
#
# When they're the same (the normal case after a binary release)
# the rewrites collapse to a single global sed, identical to the
# old behaviour. When they differ (we've cut a tag-only release
# on top of a binary release) cosmetic stamps move forward but
# download URLs stay pinned to the most recent release that
# actually serves bytes.
#
# Falls back to leaving the file alone if `gh` isn't installed or
# the call fails - we'd rather ship a slightly stale version stamp
# than break the deploy.
LATEST_RELEASE=""
LATEST_BINARY_RELEASE=""
if command -v gh >/dev/null 2>&1; then
    # LATEST_RELEASE = newest tag that's a desktop release (legacy
    # `v*-alpha` or new `desktop-v*-alpha`). Used for cosmetic
    # desktop version stamps OUTSIDE the eyebrow (e.g. the
    # "Get CMVideo X.Y.Z-alpha" h2 above the download cards).
    # Mini tags (`mini-*`) are always skipped here - they don't
    # advertise a desktop version.
    LATEST_RELEASE="$(gh release list --limit 25 -R Broadcats/CMVideo \
        --json tagName -q '.[].tagName' 2>/dev/null \
        | while read -r tag; do
            case "${tag}" in
                mini-*) continue ;;
                desktop-v*) printf '%s\n' "${tag}" | sed 's/^desktop-v//'; break ;;
                v*) printf '%s\n' "${tag}" | sed 's/^v//'; break ;;
            esac
        done)"
    # Walk the last 25 releases newest-first; pick the first one
    # whose asset list contains an `*.AppImage`. 25 is a generous
    # cap to cover repeated tag-only releases without paying a
    # full-history scan; we've never had more than ~10 between
    # binary releases historically.
    # Skip `mini-*` tags - those are mini-app deploys and never
    # carry desktop binaries; including them risks racing the
    # CI's auto-build of an old `v*` tag and finding stale
    # AppImages on a tag we don't want to advertise.
    LATEST_BINARY_RELEASE="$(gh release list --limit 25 -R Broadcats/CMVideo \
        --json tagName,isDraft,isPrerelease -q '.[] | select(.isDraft|not) | .tagName' 2>/dev/null \
        | while read -r tag; do
            case "${tag}" in
                mini-*) continue ;;
            esac
            if [[ -n "${tag}" ]] \
                && gh release view "${tag}" -R Broadcats/CMVideo \
                    --json assets -q '.assets[].name' 2>/dev/null \
                    | grep -q 'AppImage$'
            then
                # Strip whichever desktop prefix the tag carries.
                printf '%s\n' "${tag}" | sed -E 's/^(desktop-)?v//'
                break
            fi
        done)"
    # If the binary lookup fails for any reason, fall back to
    # LATEST_RELEASE so we degrade gracefully (old behaviour).
    if [[ -z "${LATEST_BINARY_RELEASE}" ]]; then
        LATEST_BINARY_RELEASE="${LATEST_RELEASE}"
    fi
fi
if [[ -n "${LATEST_RELEASE}" && -f index.html ]]; then
    # Detect the dominant existing version stamp in the file.
    # We use the most-frequent occurrence to identify the version
    # that needs replacing, then run targeted seds in dependency
    # order: filename / URL paths first (they're the strict subset
    # the cosmetic substitution must not stomp on), cosmetic last.
    OLD_VER="$(grep -oE 'v?0\.4\.[0-9]+(\.[0-9]+)?-alpha' index.html \
        | sort | uniq -c | sort -rn | awk 'NR==1{print $2}' | sed 's/^v//')"
    if [[ -n "${OLD_VER}" ]]; then
        if [[ "${LATEST_BINARY_RELEASE}" != "${OLD_VER}" ]]; then
            echo "[sync-gh-pages] Rewriting download URLs: ${OLD_VER} -> ${LATEST_BINARY_RELEASE}"
            # Pass A: every `releases/download/v${OLD_VER}/` path becomes
            # `releases/download/v${LATEST_BINARY_RELEASE}/` so the GH
            # asset URL resolves. Pass B: every `CMVideo-${OLD_VER}-`
            # filename prefix bumps to match (covers both the dl-card
            # hrefs and the visible chmod / install snippet).
            sed -i \
                -e "s|releases/download/v${OLD_VER}/|releases/download/v${LATEST_BINARY_RELEASE}/|g" \
                -e "s|CMVideo-${OLD_VER}-|CMVideo-${LATEST_BINARY_RELEASE}-|g" \
                index.html
        else
            echo "[sync-gh-pages] Download URLs already at v${LATEST_BINARY_RELEASE}, no rewrite needed"
        fi
        # Pass C: rewrite EVERY remaining `vX.Y.Z-alpha` cosmetic
        # reference to LATEST_RELEASE - not just the dominant one.
        # If a previous manual edit dropped a single stray version
        # somewhere (e.g. someone bumped just the eyebrow on a
        # release day), the dominant-detection above only catches
        # the most-frequent value and the stray would survive
        # untouched. Iterating over EVERY distinct value found in
        # the file (after Pass A / B have already moved binary
        # refs out of the way) keeps the cosmetic surface
        # consistent. PRESERVE_VERSIONS is a small allowlist of
        # versions we *want* to remain on-page as historic
        # references (the 0.4.0-alpha in the licence-history line
        # is the canonical example). Adjust here, not in the file.
        PRESERVE_VERSIONS=( "0.4.0-alpha" )
        DISTINCT_VERS="$(grep -oE 'v?0\.4\.[0-9]+(\.[0-9]+)?-alpha' index.html \
            | sed 's/^v//' | sort -u)"
        rewritten_any=0
        for v in ${DISTINCT_VERS}; do
            if [[ "${v}" == "${LATEST_RELEASE}" ]]; then
                continue
            fi
            skip=0
            for keep in "${PRESERVE_VERSIONS[@]}"; do
                if [[ "${v}" == "${keep}" ]]; then
                    skip=1
                    break
                fi
            done
            [[ ${skip} -eq 1 ]] && continue
            echo "[sync-gh-pages] Rewriting cosmetic version stamp: ${v} -> ${LATEST_RELEASE}"
            sed -i "s/${v}/${LATEST_RELEASE}/g" index.html
            rewritten_any=1
        done
        if [[ ${rewritten_any} -eq 0 ]]; then
            echo "[sync-gh-pages] Cosmetic desktop stamps already at v${LATEST_RELEASE}, no rewrite needed"
        fi
    else
        echo "[sync-gh-pages] index.html has no detectable desktop version stamps to rewrite"
    fi
else
    echo "[sync-gh-pages] Skipping desktop version-stamp rewrite (gh CLI missing or no release found)"
fi

# -----------------------------------------------------------------------------
# Mini version stamp (CalVer YYYY.MM.DD.N-alpha)
# -----------------------------------------------------------------------------
# Mini ships continuously and is versioned independently from
# desktop (see web-mini/version.py). The only stamp it owns on
# the static site is the eyebrow's "mini ..." segment. Pattern
# `\d{4}\.\d{2}\.\d{2}\.\d+-[a-z]+` is distinct from the
# desktop semver pattern (`v?\d+\.\d+\.\d+(\.\d+)?-[a-z]+`), so
# we don't need to coordinate with the desktop sed passes
# above - find every mini stamp in index.html and replace with
# the value from web-mini/version.py.
if [[ -n "${MINI_VERSION}" && -f index.html ]]; then
    DISTINCT_MINI="$(grep -oE '20[0-9]{2}\.[0-9]{2}\.[0-9]{2}\.[0-9]+-[a-z]+' index.html | sort -u)"
    mini_rewritten=0
    for v in ${DISTINCT_MINI}; do
        if [[ "${v}" == "${MINI_VERSION}" ]]; then
            continue
        fi
        echo "[sync-gh-pages] Rewriting mini version stamp: ${v} -> ${MINI_VERSION}"
        sed -i "s/${v}/${MINI_VERSION}/g" index.html
        mini_rewritten=1
    done
    if [[ ${mini_rewritten} -eq 0 ]]; then
        if [[ -n "${DISTINCT_MINI}" ]]; then
            echo "[sync-gh-pages] Mini version stamp already at ${MINI_VERSION}, no rewrite needed"
        else
            echo "[sync-gh-pages] index.html has no mini version stamps - skipping"
        fi
    fi
fi

# -----------------------------------------------------------------------------
# Diff summary
# -----------------------------------------------------------------------------
git add -A
if git diff --cached --quiet; then
    echo "[sync-gh-pages] No changes to publish - gh-pages already matches site/."
    popd >/dev/null
    exit 0
fi

echo "[sync-gh-pages] Staged changes:"
git diff --cached --stat | sed 's/^/    /'
echo

if [[ ${DRY_RUN} -eq 1 ]]; then
    echo "[sync-gh-pages] Dry run, exiting before commit."
    popd >/dev/null
    exit 0
fi

# -----------------------------------------------------------------------------
# Commit + push (fast-forward; rebase-and-retry on conflict; force only if asked)
# -----------------------------------------------------------------------------
git commit -m "${COMMIT_MSG}"

push_args=( origin gh-pages )
if [[ ${FORCE_PUSH} -eq 1 ]]; then
    push_args=( --force-with-lease origin gh-pages )
fi

if git push "${push_args[@]}"; then
    echo "[sync-gh-pages] Pushed cleanly."
else
    if [[ ${FORCE_PUSH} -eq 1 ]]; then
        echo "[sync-gh-pages] Force push failed - investigate manually." >&2
        popd >/dev/null
        exit 1
    fi
    echo "[sync-gh-pages] Push rejected (non-fast-forward). Rebasing on origin/gh-pages and retrying once..."
    git fetch origin gh-pages
    git rebase origin/gh-pages
    git push origin gh-pages
    echo "[sync-gh-pages] Pushed after rebase."
fi
popd >/dev/null

echo
echo "[sync-gh-pages] Done. https://cmvideo.online should reflect desktop v${APP_VERSION} / mini ${MINI_VERSION} within ~60s."
