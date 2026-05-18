#!/usr/bin/env python3
"""Resume Phase 2 of the full A/V battery.

Reads `results_full_av.json` and re-runs only the sites where
either the video or audio job is missing/failed. Uses the same
download_job + ffprobe + cooldown-aware transport as
`stress_full_av.py`, just with a curated "still pending" list
instead of iterating the full qualifying set.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# Reuse helpers from the main harness.
import sys
HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import stress_full_av as full  # noqa: E402

RESULTS = HERE / "results_full_av.json"
PHASE1 = HERE / "phase1_qualifying.json"


def needs_retry(rec: dict, kind: str) -> bool:
    """True if a video/audio sub-record is missing or did not produce a
    valid ffprobe-clean file."""
    sub = rec.get(kind)
    if not isinstance(sub, dict):
        return True
    if sub.get("submit_status") != 200:
        return True
    if not sub.get("ready"):
        return True
    fp = sub.get("ffprobe") or {}
    if fp.get("error"):
        return True
    return False


def main():
    qualifying = {q["name"]: q for q in json.loads(PHASE1.read_text())}
    existing = json.loads(RESULTS.read_text()) if RESULTS.exists() else []
    by_name = {r["name"]: r for r in existing}

    pending: list[tuple[dict, list[str]]] = []
    for name, q in qualifying.items():
        rec = by_name.get(name) or {**q, "video": None, "audio": None}
        kinds: list[str] = []
        if needs_retry(rec, "video"):
            kinds.append("video")
        if needs_retry(rec, "audio"):
            kinds.append("audio")
        if kinds:
            by_name[name] = rec
            pending.append((rec, kinds))

    print("=" * 72)
    print(f"RESUMING phase 2 - {len(pending)} sites need re-run")
    for rec, kinds in pending:
        print(f"  {rec['name']:<14} dur={rec.get('duration')}s  retry: {','.join(kinds)}")
    print("=" * 72)

    if not pending:
        print("Nothing to do.")
        return

    for i, (rec, kinds) in enumerate(pending, 1):
        name, url = rec["name"], rec["url"]
        print(f"\n[{i}/{len(pending)}] {name}  dur={rec.get('duration')}s")
        if "video" in kinds:
            v = full.download_job(name, url, fmt="mp4", quality="720p", index=i * 10)
            rec["video"] = v
            time.sleep(full.JOB_PACING_S)
        if "audio" in kinds:
            a = full.download_job(name, url, fmt="mp3", quality="standard", index=i * 10 + 1)
            rec["audio"] = a
            if i < len(pending):
                time.sleep(full.JOB_PACING_S)
        # Write back as a list in stable order.
        ordered = [by_name[n] for n in qualifying]
        RESULTS.write_text(json.dumps(ordered, indent=2, default=str))

    full.summary([by_name[n] for n in qualifying])


if __name__ == "__main__":
    main()
