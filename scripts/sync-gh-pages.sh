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
VERSION_PY="censor/version.py"
APP_VERSION=""
if [[ -f "${VERSION_PY}" ]]; then
    # Pull APP_VERSION = "x.y.z-alpha" out of the file without importing it.
    APP_VERSION="$(grep -E '^APP_VERSION' "${VERSION_PY}" | sed -E 's/.*"([^"]+)".*/\1/')"
fi
if [[ -z "${APP_VERSION}" ]]; then
    APP_VERSION="$(git describe --tags --abbrev=0 2>/dev/null || echo unknown)"
fi
COMMIT_MSG="site: sync ${SOURCE_BRANCH}@${SHA_SHORT} (v${APP_VERSION})"

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
echo "[sync-gh-pages] Version:     v${APP_VERSION}"
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
echo "[sync-gh-pages] Done. https://cmvideo.online should reflect v${APP_VERSION} within ~60s."
