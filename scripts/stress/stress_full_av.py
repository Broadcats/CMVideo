#!/usr/bin/env python3
"""Full-curated audio+video stress harness against the live mini.

Two-phase design:

  Phase 1 (probe):
    For every URL in `sites_curated.txt`, hit /api/info to confirm
    the source resolves and capture its duration. Filter out dead
    URLs (4xx/5xx) and short-canonicals (<300s). Save the surviving
    set to `phase1_qualifying.json`. This is cheap (info is cached
    server-side, 5/min rate limit) and answers the question "which
    sites can serve a 5+ minute test today?".

  Phase 2 (download):
    For each qualifying URL, submit two /api/process?async=1 jobs:
    one in mp4-720p video mode, one in mp3 audio mode. Poll each to
    completion, fetch the file, run ffprobe to verify the bytes are
    real. Job-rate limit is 2/minute so we pace 35s+ between submits.
    Results land in `results_full_av.json`.

The phases can be run independently:
    python stress_full_av.py probe   # writes phase1_qualifying.json
    python stress_full_av.py download  # reads phase1, runs downloads
    python stress_full_av.py both    # default

If the server-side cooldown rate-limiter trips (HTTP 429 with a
"cooling down for Ns" message in the body), we back off for the
quoted seconds + 5s slack and resume. This avoids wasting budget on
retries that will just be rejected.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE = "https://dandyfeet-cmvideo-mini.hf.space"
HERE = Path(__file__).parent
CURATED = HERE / "sites_curated.txt"
PHASE1_OUT = HERE / "phase1_qualifying.json"
PHASE1_FULL_OUT = HERE / "phase1_full.json"  # all probes including dead/short
RESULTS_OUT = HERE / "results_full_av.json"
TMPDIR = Path("/tmp/cmvm_full_av")
TMPDIR.mkdir(exist_ok=True)

MIN_DURATION_S = 300  # 5 minutes
INFO_PACING_S = 13    # 5/min => >=12s
JOB_PACING_S = 35     # 2/min => >=30s
JOB_POLL_TIMEOUT_S = 600  # max 10min per job

COOLDOWN_RE = re.compile(r"cooling down for (\d+)s")
RATE_LIMIT_PER_MIN_RE = re.compile(r"per 1 minute")
RATE_LIMIT_PER_HOUR_RE = re.compile(r"per 1 hour")


# -------- transport with cooldown awareness ---------------------------

def post(path: str, *, json_body=None, form_body=None, timeout=30, max_retries=8):
    """POST that respects server rate limiting.

    Two flavours of 429 to handle:
      1. Failure-cooldown ("cooling down for Ns") -> sleep N+5s and retry
      2. Bucket exhaustion ("Rate limit exceeded: 2 per 1 minute") -> sleep
         32s (one full bucket window + slack) and retry. Hour buckets are
         too long to wait for; report the failure instead.
    """
    attempt = 0
    while attempt < max_retries:
        attempt += 1
        try:
            if form_body is not None:
                r = requests.post(f"{BASE}{path}", data=form_body, timeout=timeout)
            else:
                r = requests.post(f"{BASE}{path}", json=json_body, timeout=timeout)
        except requests.RequestException as exc:
            return 0, {"err": str(exc)[:200]}

        if r.status_code != 429:
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"raw": r.text[:300]}

        # 429 - figure out which kind.
        try:
            j = r.json()
        except Exception:
            j = {"raw": r.text[:300]}
        body_text = json.dumps(j)
        m = COOLDOWN_RE.search(body_text)
        if m:
            wait_s = int(m.group(1)) + 5
            print(f"      [429 cooldown - sleeping {wait_s}s]", flush=True)
            time.sleep(wait_s)
            continue
        if RATE_LIMIT_PER_MIN_RE.search(body_text):
            wait_s = 32
            print(f"      [429 per-minute bucket full - sleeping {wait_s}s]", flush=True)
            time.sleep(wait_s)
            continue
        if RATE_LIMIT_PER_HOUR_RE.search(body_text):
            print(f"      [429 per-hour bucket full - giving up]", flush=True)
            return r.status_code, j
        # Unknown 429 - back off conservatively.
        print(f"      [429 unknown shape '{body_text[:120]}' - sleeping 30s]", flush=True)
        time.sleep(30)
    return 429, {"err": f"too many retries ({max_retries})"}


def get(path: str, timeout=15):
    try:
        r = requests.get(f"{BASE}{path}", timeout=timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text[:300]}
    except requests.RequestException as exc:
        return 0, {"err": str(exc)[:200]}


# -------- curated list parsing ----------------------------------------

def load_curated() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in CURATED.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            continue
        name, url = line.split("|", 1)
        out.append((name.strip(), url.strip()))
    return out


# -------- Phase 1: probe ----------------------------------------------

def phase1_probe(sites: list[tuple[str, str]]) -> tuple[list[dict], list[dict]]:
    print("=" * 72)
    print(f"PHASE 1: probing {len(sites)} URLs (need duration >= {MIN_DURATION_S}s)")
    print("=" * 72)
    qualifying: list[dict] = []
    full: list[dict] = []
    for i, (name, url) in enumerate(sites, 1):
        print(f"\n[{i:3d}/{len(sites)}] {name}", flush=True)
        print(f"          {url[:100]}", flush=True)
        t0 = time.monotonic()
        status, body = post("/api/info", json_body={"url": url}, timeout=45)
        dt = time.monotonic() - t0
        rec = {
            "name": name, "url": url, "info_status": status,
            "info_dt": round(dt, 2),
        }
        if status == 200:
            dur = body.get("duration")
            ext = body.get("extractor")
            title = (body.get("title") or "")[:60]
            over = body.get("over_cap")
            rec.update({
                "duration": dur, "extractor": ext, "title": title,
                "over_cap": over,
            })
            if isinstance(dur, (int, float)) and dur >= MIN_DURATION_S:
                qualifying.append(rec)
                print(f"          OK ({dt:.1f}s) dur={dur}s  ext={ext}  '{title}'  [QUALIFIES]")
            else:
                print(f"          OK ({dt:.1f}s) dur={dur}s  ext={ext}  '{title}'  [too short / unknown]")
        else:
            detail = body.get("detail") if isinstance(body, dict) else None
            rec["error"] = (detail if isinstance(detail, str) else json.dumps(detail))[:240]
            print(f"          FAIL {status} ({dt:.1f}s)  detail={(rec['error'] or '')[:120]}")
        full.append(rec)
        # Persist after each probe so partial runs aren't lost.
        PHASE1_FULL_OUT.write_text(json.dumps(full, indent=2, default=str))
        PHASE1_OUT.write_text(json.dumps(qualifying, indent=2, default=str))
        if i < len(sites):
            time.sleep(INFO_PACING_S)
    print(f"\n[phase 1 done] qualifying: {len(qualifying)}/{len(sites)}")
    print(f"[phase 1 done] saved -> {PHASE1_OUT.name} (qualifying) and {PHASE1_FULL_OUT.name} (full)")
    return qualifying, full


# -------- Phase 2: downloads ------------------------------------------

def ffprobe_summary(path: Path) -> dict | None:
    if not path.exists() or path.stat().st_size == 0:
        return {"error": "missing or empty"}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=format_name,duration,size,bit_rate",
             "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        if out.returncode != 0:
            return {"error": out.stderr.strip()[:200]}
        info = json.loads(out.stdout or "{}")
        fmt = info.get("format", {}) or {}
        streams = info.get("streams", []) or []
        return {
            "format_name": fmt.get("format_name"),
            "duration_s": float(fmt.get("duration", 0) or 0),
            "size_bytes": int(fmt.get("size", 0) or 0),
            "n_streams": len(streams),
            "video_codec": next((s.get("codec_name") for s in streams if s.get("codec_type") == "video"), None),
            "audio_codec": next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), None),
        }
    except Exception as exc:
        return {"error": str(exc)[:200]}


def download_job(name: str, url: str, *, fmt: str, mode: str = "download",
                 quality: str = "720p", index: int = 0) -> dict:
    """Submit a single async job, poll, fetch, ffprobe."""
    print(f"      submit {fmt} ({mode}, q={quality}) ...", flush=True)
    t0 = time.monotonic()
    status, body = post(
        "/api/process?async=1",
        form_body={"url": url, "format": fmt, "mode": mode,
                   "fps": "source", "quality": quality, "preset": "family-friendly"},
        timeout=30,
    )
    if status != 200:
        print(f"      submit FAIL {status}: {json.dumps(body)[:160]}")
        return {"submit_status": status, "submit_body": body, "fmt": fmt}
    job_id = body.get("job_id")
    if not job_id:
        print(f"      submit OK but no job_id: {body}")
        return {"submit_status": status, "submit_body": body, "fmt": fmt}
    print(f"      submit 200 ({time.monotonic()-t0:.1f}s) job={job_id[:12]}")

    # Poll
    deadline = time.monotonic() + JOB_POLL_TIMEOUT_S
    last_pct = -1
    last_stage = None
    job_body: dict = {}
    while time.monotonic() < deadline:
        time.sleep(5)
        ps, pb = get(f"/api/jobs/{job_id}")
        if ps != 200:
            print(f"      poll {ps}: {pb}")
            continue
        job_body = pb
        pct = pb.get("pct", 0)
        stage = pb.get("stage_label") or pb.get("stage")
        if pct != last_pct or stage != last_stage:
            elapsed = time.monotonic() - t0
            print(f"      t={elapsed:5.1f}s  stage={stage}  pct={pct}", flush=True)
            last_pct, last_stage = pct, stage
        if pb.get("ready") or pb.get("error"):
            break
    elapsed_total = time.monotonic() - t0

    rec = {
        "submit_status": 200, "fmt": fmt, "mode": mode, "quality": quality,
        "job_id": job_id, "elapsed_s": round(elapsed_total, 1),
    }
    if job_body.get("ready"):
        rec["ready"] = True
        rec["filename"] = job_body.get("filename")
        rec["bytes_done"] = job_body.get("bytes_done")
        # Fetch + ffprobe
        out_path = TMPDIR / f"{index:03d}_{re.sub(r'[^A-Za-z0-9]+', '_', name)[:24]}.{fmt}"
        try:
            r = requests.get(f"{BASE}/api/jobs/{job_id}/file", timeout=120, stream=True)
            if r.status_code == 200:
                written = 0
                with out_path.open("wb") as fh:
                    for chunk in r.iter_content(chunk_size=128 * 1024):
                        if chunk:
                            fh.write(chunk)
                            written += len(chunk)
                rec["downloaded_bytes"] = written
                rec["ffprobe"] = ffprobe_summary(out_path)
                fp = rec["ffprobe"] or {}
                if fp.get("error"):
                    print(f"      ffprobe ERR: {fp['error']}")
                else:
                    print(f"      DONE in {elapsed_total:.1f}s, {written/1024/1024:.2f}MB, "
                          f"v={fp.get('video_codec')} a={fp.get('audio_codec')} dur={fp.get('duration_s'):.0f}s")
                # Clean up to keep /tmp small
                try: out_path.unlink()
                except Exception: pass
            else:
                rec["file_status"] = r.status_code
                print(f"      file fetch {r.status_code}")
        except Exception as exc:
            rec["file_err"] = str(exc)[:200]
            print(f"      file fetch err: {exc}")
    elif job_body.get("error"):
        rec["error"] = (job_body.get("error") or "")[:240]
        print(f"      JOB ERROR: {rec['error'][:120]}")
    else:
        rec["error"] = f"poll timeout after {JOB_POLL_TIMEOUT_S}s"
        print(f"      TIMEOUT after {JOB_POLL_TIMEOUT_S}s")
    return rec


def phase2_downloads(qualifying: list[dict]) -> list[dict]:
    print("\n" + "=" * 72)
    print(f"PHASE 2: video+audio download for {len(qualifying)} qualifying URLs")
    print(f"  Estimated runtime: {len(qualifying)*2*JOB_PACING_S/60:.1f} min minimum (gated by 2/min limit)")
    print(f"  Realistic runtime: each long-video job takes 2-15 min through the proxy")
    print("=" * 72)
    out: list[dict] = []
    for i, q in enumerate(qualifying, 1):
        name, url = q["name"], q["url"]
        print(f"\n[{i:3d}/{len(qualifying)}] {name}  dur={q.get('duration')}s")
        rec = {**q, "video": None, "audio": None}
        rec["video"] = download_job(name, url, fmt="mp4", quality="720p", index=i*2)
        time.sleep(JOB_PACING_S)
        rec["audio"] = download_job(name, url, fmt="mp3", quality="standard", index=i*2 + 1)
        out.append(rec)
        # Persist after each site so a crash mid-run preserves work.
        RESULTS_OUT.write_text(json.dumps(out, indent=2, default=str))
        if i < len(qualifying):
            time.sleep(JOB_PACING_S)
    return out


def summary(results: list[dict]) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"Total qualifying sites tested: {len(results)}")
    v_ok = sum(1 for r in results if (r.get("video") or {}).get("ready") and not ((r["video"].get("ffprobe") or {}).get("error")))
    a_ok = sum(1 for r in results if (r.get("audio") or {}).get("ready") and not ((r["audio"].get("ffprobe") or {}).get("error")))
    print(f"  video downloads OK + ffprobe-valid: {v_ok}/{len(results)}")
    print(f"  audio downloads OK + ffprobe-valid: {a_ok}/{len(results)}")
    print()
    print(f"{'site':<30} {'dur':>6}  {'video':<14} {'audio':<14}")
    print(f"{'-'*30} {'-'*6}  {'-'*14} {'-'*14}")
    for r in results:
        v = r.get("video") or {}
        a = r.get("audio") or {}
        v_tag = "OK" if v.get("ready") and not ((v.get("ffprobe") or {}).get("error")) else \
                f"FAIL {(v.get('error') or v.get('submit_status') or '')!s:.20}"
        a_tag = "OK" if a.get("ready") and not ((a.get("ffprobe") or {}).get("error")) else \
                f"FAIL {(a.get('error') or a.get('submit_status') or '')!s:.20}"
        print(f"{r['name'][:30]:<30} {r.get('duration', '?'):>6}  {v_tag:<14} {a_tag:<14}")
    print(f"\nFull results -> {RESULTS_OUT}")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    if mode in ("probe", "both"):
        sites = load_curated()
        qualifying, _full = phase1_probe(sites)
    else:
        if not PHASE1_OUT.exists():
            sys.exit("phase1_qualifying.json missing - run probe first")
        qualifying = json.loads(PHASE1_OUT.read_text())

    if mode in ("download", "both"):
        results = phase2_downloads(qualifying)
        summary(results)


if __name__ == "__main__":
    main()
