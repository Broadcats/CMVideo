#!/usr/bin/env python3
"""One-shot deploy of web-mini/ to a Hugging Face Space.

Usage:
    HF_TOKEN=hf_xxx python3 scripts/deploy-mini.py

The token must have **write** access. Generate one at
https://huggingface.co/settings/tokens (pick "Write" or a fine-grained
token with write access to your namespace).

Optional flags:
    --owner    HF user/org to host the Space under (default: Broadcats)
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

# Files/dirs that should never end up in the Space repo.
IGNORE_PATTERNS = {
    "__pycache__",
    ".venv",
    ".venv-mini",
    ".pytest_cache",
    ".mypy_cache",
    ".DS_Store",
}


def _ensure_hf_hub() -> None:
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        print("[deploy-mini] huggingface_hub not installed; installing into user site...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--user", "--quiet", "huggingface_hub>=0.23"]
        )


def _resolve_token(cli_token: str | None) -> str:
    token = cli_token or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        sys.exit(
            "[deploy-mini] No HF token found.\n"
            "  Generate one at https://huggingface.co/settings/tokens (Write access)\n"
            "  then re-run:  HF_TOKEN=hf_xxx python3 scripts/deploy-mini.py"
        )
    return token


def _allowed(path: Path) -> bool:
    parts = set(path.relative_to(WEB_MINI).parts)
    return not (parts & IGNORE_PATTERNS)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--owner", default="Broadcats")
    ap.add_argument("--space", default="cmvideo-mini")
    ap.add_argument("--token", default=None)
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not WEB_MINI.is_dir():
        sys.exit(f"[deploy-mini] {WEB_MINI} not found.")

    files = sorted(p for p in WEB_MINI.rglob("*") if p.is_file() and _allowed(p))
    if not files:
        sys.exit("[deploy-mini] web-mini/ is empty?")

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
    try:
        api.create_repo(
            repo_id=repo_id,
            repo_type="space",
            space_sdk="docker",
            private=args.private,
            exist_ok=True,
        )
        print(f"[deploy-mini] Space exists / created: {repo_id}")
    except HfHubHTTPError as exc:
        sys.exit(f"[deploy-mini] Could not create/access Space: {exc}")

    # 2. Upload the whole directory.
    api.upload_folder(
        folder_path=str(WEB_MINI),
        repo_id=repo_id,
        repo_type="space",
        commit_message="Deploy CMVideo Mini",
        ignore_patterns=[f"{p}/**" for p in IGNORE_PATTERNS] + list(IGNORE_PATTERNS),
    )

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
