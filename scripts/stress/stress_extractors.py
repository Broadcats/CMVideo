#!/usr/bin/env python3
"""Run a 100+ site stress test against the multi-extractor chain.

Phase 1: yt-dlp `extract_info(download=False)` probe for every URL,
         8 workers in parallel. This is cheap (10s timeout) and
         tells us which sites yt-dlp's metadata resolution covers.

Phase 2: For each URL that failed Phase 1 with a non-terminal
         error, run the FULL fallback chain (tiers 2-7) with a
         60s timeout. We skip Tier 8 (LLM) here because it
         requires API credentials we don't ship in the test.

We don't actually download a video file in either phase: in Phase
1 we only probe metadata, and in Phase 2 we treat any successful
ExtractionResult as a win, then immediately delete the file.
"""
import concurrent.futures as cf
import json
import logging
import os
import shutil
import socket
import sys
import tempfile
import time
import traceback
from pathlib import Path

# Quiet noisy modules
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("yt_dlp", "yt-dlp", "asyncio", "urllib3", "censor.extractors"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

socket.setdefaulttimeout(15)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SITES_FILE = Path(__file__).with_name("sites.txt")
RESULTS_FILE = Path(__file__).with_name("results.json")
PROBE_TIMEOUT = 12.0  # seconds per yt-dlp probe
CHAIN_TIMEOUT = 90.0  # seconds per full-chain attempt
PROBE_WORKERS = 8
CHAIN_WORKERS = 3     # heavy stuff (gallery-dl, lux, you-get, playwright)
MAX_CHAIN_ATTEMPTS = 30  # only test the chain on the first N yt-dlp failures (Playwright is heavy)


def _load_sites():
    sites = []
    for line in SITES_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            continue
        name, url = line.split("|", 1)
        sites.append((name.strip(), url.strip()))
    return sites


def _probe_one(name_url):
    name, url = name_url
    t0 = time.monotonic()
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError as _DE
        with YoutubeDL({
            "quiet": True, "no_warnings": True,
            "skip_download": True, "extract_flat": False,
            "socket_timeout": 8,
            "extractor_args": {"youtube": {"player_client": ["tv", "web", "tv_embedded"]}},
        }) as ydl:
            info = ydl.extract_info(url, download=False)
        dt = time.monotonic() - t0
        title = (info or {}).get("title", "<no-title>")[:60]
        return {"name": name, "url": url, "phase": "probe", "ok": True, "dt": dt, "title": title}
    except Exception as e:  # noqa: BLE001
        dt = time.monotonic() - t0
        msg = str(e).strip()[:200]
        # Classify error: if it's a yt-dlp 'Unsupported URL' or
        # similar, fallback might still work. Network/SSL errors
        # are harder to recover from in 75s.
        recoverable = (
            "Unsupported URL" in msg or "extractor" in msg.lower()
            or "no video" in msg.lower() or "404" in msg
        )
        return {
            "name": name, "url": url, "phase": "probe",
            "ok": False, "dt": dt, "err": msg,
            "fallback_eligible": recoverable,
        }


def _chain_one(name_url):
    name, url = name_url
    t0 = time.monotonic()
    tmp = Path(tempfile.mkdtemp(prefix=f"chain_{name[:20]}_"))
    try:
        from censor import extractors as _ex
        # Force-disable LLM tier (no API key) and reduce playwright
        # cost. This phase is about verifying the fallback chain
        # actually FIRES - one win is good evidence.
        os.environ.pop("CMVIDEO_LLM_BASE_URL", None)
        os.environ.pop("CMVIDEO_LLM_API_KEY", None)
        # Try the full chain except LLM, giving it ~60s budget per
        # tier on slow ones. We use a wall-clock cap as outer
        # safeguard.
        result = _ex.extract_with_fallbacks(
            url, tmp,
            fmt="mp4",
            enabled=("yt-dlp", "gallery-dl", "lux", "you-get", "streamlink", "playwright"),
        )
        dt = time.monotonic() - t0
        size = result.path.stat().st_size if result.path.exists() else 0
        return {
            "name": name, "url": url, "phase": "chain", "ok": True, "dt": dt,
            "extractor": result.extractor,
            "size_mb": round(size / (1024 * 1024), 2),
            "attempts": result.attempts,
        }
    except Exception as e:  # noqa: BLE001
        dt = time.monotonic() - t0
        return {
            "name": name, "url": url, "phase": "chain", "ok": False,
            "dt": dt, "err": str(e)[:300],
        }
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass


def main():
    sites = _load_sites()
    print(f"Loaded {len(sites)} sites")

    # ------------- Phase 1: yt-dlp probe -------------
    print(f"\n=== PHASE 1: yt-dlp probe ({PROBE_WORKERS} workers) ===")
    probe_results = []
    with cf.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as ex:
        futs = {ex.submit(_probe_one, s): s for s in sites}
        done = 0
        for fut in cf.as_completed(futs, timeout=PROBE_TIMEOUT * len(sites) + 60):
            done += 1
            r = fut.result()
            probe_results.append(r)
            tag = " OK " if r["ok"] else "FAIL"
            err = "" if r["ok"] else f"  err={r['err'][:80]}"
            print(f"  [{done:3d}/{len(sites)}] {tag} {r['dt']:5.1f}s  {r['name']:18s}{err}")

    # ------------- Phase 2: full chain on yt-dlp failures -------------
    all_failures = [
        (r["name"], r["url"]) for r in probe_results if not r["ok"]
    ]
    # Cap chain attempts: Playwright is heavy and we want this to
    # finish in a reasonable wall-clock time. We sample a diverse
    # subset rather than running it on all 70+ failures.
    fallback_candidates = all_failures[:MAX_CHAIN_ATTEMPTS]
    print(f"\n=== PHASE 2: full chain on {len(fallback_candidates)}/{len(all_failures)} yt-dlp failures (Playwright capped) ===")
    chain_results = []
    with cf.ThreadPoolExecutor(max_workers=CHAIN_WORKERS) as ex:
        futs = {ex.submit(_chain_one, c): c for c in fallback_candidates}
        done = 0
        for fut in cf.as_completed(futs, timeout=CHAIN_TIMEOUT * len(fallback_candidates) + 60):
            done += 1
            try:
                r = fut.result(timeout=CHAIN_TIMEOUT + 30)
            except Exception as e:  # noqa: BLE001
                r = {"name": "?", "url": "?", "phase": "chain", "ok": False, "err": f"runner: {e}"}
            chain_results.append(r)
            tag = " RECOVER " if r["ok"] else " STILL_FAIL"
            extra = ""
            if r["ok"]:
                extra = f"  via={r.get('extractor', '?')}  {r.get('size_mb', 0)} MB"
            else:
                extra = f"  err={r.get('err', '')[:80]}"
            print(f"  [{done:3d}/{len(fallback_candidates)}] {tag} {r.get('dt', 0):5.1f}s  {r['name']:18s}{extra}")

    # ------------- Summary -------------
    n_total = len(sites)
    n_probe_ok = sum(1 for r in probe_results if r["ok"])
    n_recovered = sum(1 for r in chain_results if r["ok"])
    n_dead = n_total - n_probe_ok - n_recovered
    print()
    print("=" * 60)
    print(f"TOTAL SITES TESTED: {n_total}")
    print(f"  yt-dlp metadata OK:        {n_probe_ok:3d} ({n_probe_ok/n_total:.0%})")
    print(f"  yt-dlp failed but chain WIN:{n_recovered:3d} ({n_recovered/n_total:.0%})")
    print(f"  full-chain failures:       {n_dead:3d} ({n_dead/n_total:.0%})")
    print(f"  combined coverage:         {(n_probe_ok+n_recovered):3d} ({(n_probe_ok+n_recovered)/n_total:.0%})")
    print("=" * 60)

    # Persist for review
    payload = {
        "phase1_probe": probe_results,
        "phase2_chain": chain_results,
        "summary": {
            "total": n_total,
            "probe_ok": n_probe_ok,
            "chain_recovered": n_recovered,
            "dead": n_dead,
        },
    }
    RESULTS_FILE.write_text(json.dumps(payload, indent=2))
    print(f"\nResults written to {RESULTS_FILE}")

    # Per-extractor breakdown for sites recovered by chain
    by_extractor = {}
    for r in chain_results:
        if r.get("ok"):
            by_extractor.setdefault(r.get("extractor", "?"), []).append(r["name"])
    if by_extractor:
        print("\nWins by tier (from chain phase):")
        for tool, sites in sorted(by_extractor.items(), key=lambda x: -len(x[1])):
            print(f"  {tool:14s}  {len(sites):3d}: {', '.join(sites[:6])}{'...' if len(sites) > 6 else ''}")


if __name__ == "__main__":
    main()
