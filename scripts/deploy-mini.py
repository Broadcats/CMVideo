#!/usr/bin/env python3
"""One-shot deploy of web-mini/ to a Hugging Face Space.

Usage:
    HF_TOKEN=hf_xxx python3 scripts/deploy-mini.py

The token must have **write** access. Generate one at
https://huggingface.co/settings/tokens (pick "Write" or a fine-grained
token with write access to your namespace).

Optional flags:
    --owner    HF user/org to host the Space under (default: Dandyfeet,
               which owns the production Space at
               https://dandyfeet-cmvideo-mini.hf.space)
    --space    Space name (default: cmvideo-mini)
    --private  Make the Space private
    --dry-run  Print what would happen, don't push

When the script finishes it prints the live Space URL. HF will start
building the Docker image automatically; that takes ~2-4 minutes on the
first deploy and ~1-2 minutes on subsequent updates.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_MINI = REPO_ROOT / "web-mini"

# Files/dirs that should never end up in the Space repo. Patterns
# are gitignore-style and matched against any path component, so a
# pattern of ".venv*" catches .venv, .venv-mini, .venv-test,
# .venv-deploy, etc. Critical: a stray test venv inside web-mini/
# is hundreds of megabytes and will silently fill up the Space's
# 1 GB free-tier storage in one upload.
IGNORE_GLOBS = (
    "__pycache__",
    ".venv*",
    "venv",
    "env",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".coverage",
    ".coverage.*",
    ".DS_Store",
    "*.egg-info",
    "node_modules",
    "*.pyc",
    "*.pyo",
)


def _ensure_hf_hub() -> None:
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        print("[deploy-mini] huggingface_hub not installed; installing into user site...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--user", "--quiet", "huggingface_hub>=0.23"]
        )


def _resolve_token(cli_token: str | None) -> str:
    """Resolve an HF token in this priority order:
      1. --token CLI flag
      2. HF_TOKEN / HUGGING_FACE_HUB_TOKEN env vars
      3. The cached token at ~/.cache/huggingface/token
         (populated by `hf auth login`)

    The cache lookup is preferred over env vars in real usage
    because pasting a token into a shell command leaks it into
    bash history; `hf auth login` reads it from a masked stdin
    and persists it at mode 0600. We still honour env vars for
    CI / Docker contexts where there's no interactive login."""
    token = cli_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        # Newer huggingface_hub (>= 0.27) removed `HfFolder`; the
        # canonical way to read the cached token is `get_token()`.
        # Older versions still expose `HfFolder.get_token()` which
        # is what we fall back to.
        try:
            from huggingface_hub import get_token
            token = get_token()
        except ImportError:
            try:
                from huggingface_hub import HfFolder  # type: ignore[attr-defined]
                token = HfFolder.get_token()
            except Exception:  # noqa: BLE001 - cache lookup is best-effort
                token = None
        except Exception:  # noqa: BLE001
            token = None
    if not token:
        sys.exit(
            "[deploy-mini] No HF token found.\n"
            "  Either:\n"
            "    A. `hf auth login` (preferred - stores token securely at ~/.cache/huggingface/token)\n"
            "    B. Generate one at https://huggingface.co/settings/tokens (Write access)\n"
            "       and re-run with `HF_TOKEN=hf_xxx scripts/deploy-mini.py`"
        )
    return token


def _allowed(path: Path) -> bool:
    """Return False if ANY path component matches one of the
    IGNORE_GLOBS patterns. Uses fnmatch so we get gitignore-style
    wildcards (e.g. '.venv*' catches .venv, .venv-test, .venv-deploy).
    The check runs on each component independently so a leaf file
    inside an ignored directory is correctly rejected."""
    import fnmatch
    rel_parts = path.relative_to(WEB_MINI).parts
    for component in rel_parts:
        for pattern in IGNORE_GLOBS:
            if fnmatch.fnmatch(component, pattern):
                return False
    return True


def _push_space_secrets(api, repo_id: str, args) -> None:
    """Push HF Space secrets (encrypted env vars) for the live container.

    Secrets are sourced in priority order:
      1. CLI flag  (--cobalt-url / --cobalt-key)
      2. Env var   (COBALT_API_BASE / COBALT_API_KEY)
      3. Nothing   (secret is not touched)

    Calling `add_space_secret` on an existing secret overwrites it, so
    re-running deploy after rotating a key always stays current.
    """
    secrets_to_push: dict[str, str] = {}

    cobalt_url = args.cobalt_url or os.environ.get("COBALT_API_BASE", "")
    cobalt_key = args.cobalt_key or os.environ.get("COBALT_API_KEY", "")
    if cobalt_url:
        secrets_to_push["COBALT_API_BASE"] = cobalt_url.rstrip("/")
    if cobalt_key:
        secrets_to_push["COBALT_API_KEY"] = cobalt_key

    # Residential proxy — carry through if set in the deployer's env.
    for var in ("RESIDENTIAL_PROXY", "PROXY_URL", "CMVIDEO_PROXY"):
        val = os.environ.get(var, "")
        if val:
            secrets_to_push[var] = val
            break  # only push the first one found

    if not secrets_to_push:
        return

    try:
        from huggingface_hub import add_space_secret  # type: ignore[attr-defined]
    except ImportError:
        print("[deploy-mini] WARN: huggingface_hub too old to call add_space_secret — update with `pip install -U huggingface_hub`")
        return

    for key, val in secrets_to_push.items():
        try:
            add_space_secret(repo_id=repo_id, key=key, value=val)
            print(f"[deploy-mini] Secret set: {key} = {'*' * 8}{val[-4:] if len(val) > 8 else '****'}")
        except Exception as exc:  # noqa: BLE001
            print(f"[deploy-mini] WARN: could not set secret {key}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", default="Dandyfeet")
    ap.add_argument("--space", default="cmvideo-mini")
    ap.add_argument("--token", default=None)
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    # Cobalt sidecar. Values can also come from env vars with the same names.
    # Run scripts/cobalt/setup.sh first; it prints both values.
    ap.add_argument("--cobalt-url",  default=None,
                    help="Cobalt API base URL (or set COBALT_API_BASE env var)")
    ap.add_argument("--cobalt-key",  default=None,
                    help="Cobalt API key (or set COBALT_API_KEY env var)")
    args = ap.parse_args()

    if not WEB_MINI.is_dir():
        sys.exit(f"[deploy-mini] {WEB_MINI} not found.")

    files = sorted(p for p in WEB_MINI.rglob("*") if p.is_file() and _allowed(p))
    if not files:
        sys.exit("[deploy-mini] web-mini/ is empty?")

    # Pre-flight size sanity check. The mini's source code should
    # be a few hundred KB total - if our IGNORE_GLOBS missed
    # something (a stray venv, a leaked Whisper model, build
    # artefacts) we'd silently push hundreds of MB to the Space
    # and burn through the 1 GB storage cap. Cap at 50 MB; the
    # actual real payload is comfortably under 500 KB.
    total_size = sum(f.stat().st_size for f in files)
    SIZE_LIMIT_MB = 50
    print(f"[deploy-mini] Total payload: {total_size / 1e6:.2f} MB across {len(files)} files")
    if total_size > SIZE_LIMIT_MB * 1024 * 1024:
        biggest = sorted(files, key=lambda p: -p.stat().st_size)[:8]
        print()
        print(f"[deploy-mini] REFUSING to deploy: payload {total_size / 1e6:.0f} MB exceeds {SIZE_LIMIT_MB} MB sanity cap.")
        print("  This usually means an ignored directory leaked into web-mini/.")
        print("  Biggest files in the upload set:")
        for f in biggest:
            rel = f.relative_to(WEB_MINI)
            print(f"    {f.stat().st_size / 1e6:>6.1f} MB  {rel}")
        print()
        print("  Either:")
        print("   * remove the offending directory from web-mini/, OR")
        print("   * add its name to IGNORE_GLOBS in scripts/deploy-mini.py")
        sys.exit(2)

    repo_id = f"{args.owner}/{args.space}"
    print(f"[deploy-mini] Target: https://huggingface.co/spaces/{repo_id}")
    print(f"[deploy-mini] Uploading {len(files)} files from {WEB_MINI}")
    for f in files:
        rel = f.relative_to(WEB_MINI)
        print(f"    {rel}  ({f.stat().st_size:>8} bytes)")

    if args.dry_run:
        print("[deploy-mini] --dry-run: stopping before any API call.")
        return 0

    _ensure_hf_hub()
    from huggingface_hub import HfApi
    from huggingface_hub.utils import HfHubHTTPError

    token = _resolve_token(args.token)
    api = HfApi(token=token)

    # 1. Make sure the Space exists with the right SDK.
    #    The HF create_repo endpoint requires create-permissions
    #    on the target namespace EVEN WITH exist_ok=True - the
    #    library only swallows HTTP 409 ("already exists"), not
    #    HTTP 403 ("you can't create here at all"). Tokens that
    #    are scoped to write a specific repo (the common case
    #    for fine-grained / org-member tokens that aren't admins)
    #    will 403 here even though the upload below would
    #    succeed.
    #
    #    So we probe with `repo_info` first; if the Space exists
    #    we skip the create call entirely and go straight to
    #    upload. Only fall back to create_repo on 404 (truly
    #    missing).
    try:
        api.repo_info(repo_id=repo_id, repo_type="space")
        print(f"[deploy-mini] Space already exists: {repo_id}")
    except HfHubHTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
        if int(status) == 404:
            try:
                api.create_repo(
                    repo_id=repo_id,
                    repo_type="space",
                    space_sdk="docker",
                    private=args.private,
                    exist_ok=True,
                )
                print(f"[deploy-mini] Space created: {repo_id}")
            except HfHubHTTPError as cexc:
                sys.exit(
                    f"[deploy-mini] Space {repo_id} doesn't exist and your "
                    f"token can't create it: {cexc}\n"
                    f"  Generate a token at https://huggingface.co/settings/tokens\n"
                    f"  with 'Write' scope (or fine-grained: write to "
                    f"{repo_id})."
                )
        else:
            sys.exit(
                f"[deploy-mini] Could not access Space {repo_id}: {exc}\n"
                f"  Token may be revoked, expired, or lack read access."
            )

    # 2. Upload the whole directory, with retry-with-backoff on
    #    transient HF backend errors. HF's commit endpoint
    #    intermittently 500s, especially right after a Space has
    #    been (re)created, while their backend storage catches
    #    up. We retry up to 4 times with exponential backoff
    #    (15s, 30s, 60s, 120s) before giving up.
    import time
    import random
    last_exc = None
    backoffs = [15, 30, 60, 120]
    for attempt in range(len(backoffs) + 1):
        try:
            api.upload_folder(
                folder_path=str(WEB_MINI),
                repo_id=repo_id,
                repo_type="space",
                commit_message="Deploy CMVideo Mini",
                ignore_patterns=(
                    [f"{p}/**" for p in IGNORE_GLOBS]
                    + [f"**/{p}/**" for p in IGNORE_GLOBS]
                    + list(IGNORE_GLOBS)
                ),
            )
            break
        except HfHubHTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", 0) or 0
            transient = 500 <= int(status) < 600 or status in (408, 409, 429)
            if not transient or attempt >= len(backoffs):
                raise
            delay = backoffs[attempt] + random.uniform(0, 5)
            print(
                f"[deploy-mini] HF returned HTTP {status} on commit "
                f"(attempt {attempt + 1}/{len(backoffs) + 1}). "
                f"Retrying in {delay:.0f}s..."
            )
            time.sleep(delay)
            last_exc = exc
    else:  # pragma: no cover - reachable only if loop exits via break
        if last_exc is not None:
            raise last_exc

    # 3. Squash git history when the Space's storage is getting close
    #    to the free-tier 1 GB cap. Free Spaces accumulate git history
    #    on every deploy, and even small files re-stored across 30+
    #    commits add up. `super_squash_history` collapses every
    #    commit into one and reclaims storage. Cheap, idempotent,
    #    safe to run every deploy.
    try:
        info = api.repo_info(repo_id=repo_id, repo_type="space")
        used = getattr(info, "used_storage", None) or getattr(
            info, "usedStorage", None
        )
        # used_storage is in bytes when the API returns it. We squash
        # at 700 MB to leave generous headroom under the 1 GB cap.
        SQUASH_THRESHOLD = 700 * 1024 * 1024
        if isinstance(used, (int, float)) and used > SQUASH_THRESHOLD:
            print(
                f"[deploy-mini] Space storage at {used / 1e9:.2f} GB - "
                "squashing git history to reclaim space..."
            )
            api.super_squash_history(repo_id=repo_id, repo_type="space")
            print("[deploy-mini] History squashed.")
        elif isinstance(used, (int, float)):
            print(f"[deploy-mini] Space storage: {used / 1e6:.0f} MB / 1 GB cap")
    except Exception as exc:  # noqa: BLE001 - storage check is best-effort
        # Older huggingface_hub versions may not expose used_storage
        # or super_squash_history. We still uploaded successfully so
        # don't fail the whole deploy.
        print(f"[deploy-mini] (storage check skipped: {exc})")

    # 4. Push Space secrets (Cobalt URL/key, and any others from env).
    #    HF Space secrets are encrypted at rest and exposed as env vars
    #    inside the running container. We push them after every deploy
    #    so a deploy with --cobalt-url always syncs the secret even if
    #    the code files didn't change.
    _push_space_secrets(api, repo_id, args)

    direct_url = f"https://{args.owner.lower()}-{args.space}.hf.space"
    page_url = f"https://huggingface.co/spaces/{repo_id}"
    print()
    print("[deploy-mini] Upload complete. HF is building the Docker image now.")
    print(f"    Logs / status: {page_url}")
    print(f"    Live URL:      {direct_url}")
    print()
    print("  First build is ~2-4 minutes. Once it shows 'Running', refresh")
    print(f"  cmvideo.online and try the widget.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
