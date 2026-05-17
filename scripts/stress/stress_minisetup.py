#!/usr/bin/env python3
"""Mini-app coverage stress test.

Two probes, no real downloads:

  Probe A: yt-dlp `extract_info(download=False)` for each URL.
           Tells us "does yt-dlp recognise + parse this site's
           page". Audio AND video formats both come out of this
           single call so we get mp3 + mp4 coverage in one shot.

  Probe B: web-mini's `_resolve_streamable_format(url, fmt, q)`
           for both fmt='mp4' and fmt='mp3'. Tells us "would the
           streaming fast-path (tier 0 direct / tier 1 / tier 2)
           actually fire for this URL". For mp3 the fast-path
           always returns None on purpose (mp3 needs ffmpeg
           post-encode -> slow path), so the mp3 column reports
           whether yt-dlp gave us an audio-bearing format that
           the slow path COULD remux (i.e. has_audio=True).

We deliberately don't spawn the full extractor chain locally
because gallery-dl / playwright / lux / you-get / streamlink
aren't installed in the local venv. Sites that need those tiers
will show as "yt-dlp: fail" - the column is informative for
"which sites need the deployed Space's full chain".

Output:
  scripts/stress/results_minisetup.json   raw per-URL data
  scripts/stress/results_minisetup.md     readable per-site table
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import logging
import os
import socket
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("yt_dlp", "yt-dlp", "asyncio", "urllib3", "web-mini"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

socket.setdefaulttimeout(15)

REPO = Path(__file__).resolve().parents[2]
WEBMINI = REPO / "web-mini"
# app.py mounts `static/` relative to cwd, so chdir before import
# instead of fighting the FastAPI app's filesystem assumptions.
os.chdir(WEBMINI)
sys.path.insert(0, str(WEBMINI))

SITES_FILE = Path(__file__).with_name("sites.txt")
RESULTS_JSON = Path(__file__).with_name("results_minisetup.json")
RESULTS_MD = Path(__file__).with_name("results_minisetup.md")

PROBE_TIMEOUT = 20
WORKERS = 6   # gentle on the network; tube sites tend to throttle
MAX_VIDEO_HEIGHT_LOCAL = 1080
TARGET_HEIGHT = 720  # what the mini-app's "standard" quality requests


def _load_sites() -> list[tuple[str, str]]:
    sites = []
    for line in SITES_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        name, url = line.split("|", 1)
        sites.append((name.strip(), url.strip()))
    return sites


# Lazy-imported per worker so startup doesn't pay it n times
def _yt_dlp_probe(url: str) -> dict:
    from yt_dlp import YoutubeDL
    YDL_EXTRACTOR_ARGS = {
        "youtube": {
            "player_client": ["tv", "tv_embedded", "mediaconnect", "web_creator", "mweb"],
            "player_skip": ["configs"],
        },
    }
    YDL_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    )
    info_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 12,
        "extractor_args": YDL_EXTRACTOR_ARGS,
        "http_headers": {"User-Agent": YDL_USER_AGENT},
    }
    t0 = time.monotonic()
    try:
        with YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "dt": round(time.monotonic() - t0, 2),
            "err_class": type(exc).__name__,
            "err": str(exc).splitlines()[-1][:200] if str(exc) else "",
        }

    if info is None:
        return {
            "ok": False,
            "dt": round(time.monotonic() - t0, 2),
            "err_class": "EmptyInfo",
            "err": "extract_info returned None",
        }

    formats = info.get("formats") or []
    has_video = any(
        isinstance(f, dict) and (f.get("vcodec") and f.get("vcodec") != "none")
        for f in formats
    )
    has_audio = any(
        isinstance(f, dict) and (f.get("acodec") and f.get("acodec") != "none")
        for f in formats
    )
    # If `formats` is empty we might still have a single-format
    # dict at the top level (yt-dlp's "we couldn't merge but here's
    # one stream" response shape).
    if not formats and info.get("url"):
        has_video = bool(info.get("vcodec") and info.get("vcodec") != "none")
        has_audio = bool(info.get("acodec") and info.get("acodec") != "none")

    return {
        "ok": True,
        "dt": round(time.monotonic() - t0, 2),
        "title": (info.get("title") or "")[:80],
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "duration": info.get("duration"),
        "n_formats": len(formats),
        "has_video": has_video,
        "has_audio": has_audio,
    }


def _fastpath_probe(url: str, fmt: str) -> dict:
    """Call web-mini's `_resolve_streamable_format` directly.
    No HTTP, no token mint - we just want to know which tier
    (0 / 1 / 2 / None) would fire."""
    # Suppress noise from the resolver's own logging
    logging.getLogger("cmvideo-mini").setLevel(logging.CRITICAL)
    # Make sure each worker can import app.py - it pulls in a lot.
    # Cache the import at module level.
    global _APP
    try:
        _APP
    except NameError:
        # Safety: app.py wires up Starlette/FastAPI on import.
        # Set a no-op env var so non-secret-dependent code paths
        # don't crash. The proxy_router still reads at call time.
        os.environ.setdefault("CMVIDEO_LLM_BASE_URL", "")
        os.environ.setdefault("CMVIDEO_LLM_API_KEY", "")
        import app as _APP  # noqa: F401
    from app import _resolve_streamable_format

    t0 = time.monotonic()
    try:
        out = _resolve_streamable_format(url, fmt, "standard")
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "dt": round(time.monotonic() - t0, 2),
            "err_class": type(exc).__name__,
            "err": str(exc).splitlines()[-1][:200] if str(exc) else "",
        }
    dt = round(time.monotonic() - t0, 2)
    if out is None:
        return {"ok": False, "dt": dt, "method": None}
    method = out.get("method")
    # Tier 0 returns method=direct AND extractor=direct-url; tier 1
    # also returns method=direct but with the actual extractor name.
    tier = "0" if out.get("extractor") == "direct-url" else (
        "1" if method == "direct" else "2"
    )
    return {
        "ok": True,
        "dt": dt,
        "method": method,
        "tier": tier,
        "filesize": out.get("filesize"),
    }


def _probe_one(name_url: tuple[str, str]) -> dict:
    name, url = name_url
    yt = _yt_dlp_probe(url)
    fp_mp4 = _fastpath_probe(url, "mp4")
    # mp3 fast-path always returns None by design (slow-path
    # ffmpeg-encodes); track has_audio from yt-dlp instead so the
    # mp3 column means "could the slow path produce mp3 from this".
    fp_mp3 = {"ok": False, "method": None}  # placeholder - not run
    return {
        "name": name,
        "url": url,
        "yt": yt,
        "fp_mp4": fp_mp4,
        "fp_mp3": fp_mp3,
    }


def main():
    sites = _load_sites()
    print(f"Loaded {len(sites)} sites from {SITES_FILE}", flush=True)

    results = []
    t_start = time.monotonic()
    print(f"=== probe ({WORKERS} workers, {PROBE_TIMEOUT}s timeout) ===", flush=True)
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_probe_one, s): s for s in sites}
        done = 0
        for fut in cf.as_completed(futs):
            done += 1
            try:
                r = fut.result(timeout=PROBE_TIMEOUT * 3)
            except cf.TimeoutError:
                name, url = futs[fut]
                r = {"name": name, "url": url, "yt": {"ok": False, "err": "future timeout"}, "fp_mp4": {"ok": False}, "fp_mp3": {"ok": False}}
            except Exception as exc:  # noqa: BLE001
                name, url = futs[fut]
                r = {"name": name, "url": url, "yt": {"ok": False, "err": f"runner: {exc}"}, "fp_mp4": {"ok": False}, "fp_mp3": {"ok": False}}
            results.append(r)
            yt = r["yt"]
            fp = r["fp_mp4"]
            yt_tag = " OK" if yt.get("ok") else "FAIL"
            yt_av = ""
            if yt.get("ok"):
                v = "V" if yt.get("has_video") else "-"
                a = "A" if yt.get("has_audio") else "-"
                yt_av = f" [{v}{a}]"
            fp_tag = f"tier{fp['tier']}" if fp.get("ok") else "  no "
            extra = ""
            if not yt.get("ok"):
                extra = f"  err={(yt.get('err') or '')[:80]}"
            print(f"  [{done:3d}/{len(sites)}] yt={yt_tag}{yt_av:5s} fp={fp_tag} {r['name']:18s}{extra}", flush=True)

    elapsed = time.monotonic() - t_start
    print(f"\nElapsed: {elapsed:.1f}s ({elapsed/len(sites):.2f}s per site)\n", flush=True)

    # ---------- summary ----------
    n = len(results)
    n_yt_ok = sum(1 for r in results if r["yt"].get("ok"))
    n_video = sum(1 for r in results if r["yt"].get("ok") and r["yt"].get("has_video"))
    n_audio = sum(1 for r in results if r["yt"].get("ok") and r["yt"].get("has_audio"))
    n_fp_any = sum(1 for r in results if r["fp_mp4"].get("ok"))
    n_fp_t0 = sum(1 for r in results if r["fp_mp4"].get("ok") and r["fp_mp4"].get("tier") == "0")
    n_fp_t1 = sum(1 for r in results if r["fp_mp4"].get("ok") and r["fp_mp4"].get("tier") == "1")
    n_fp_t2 = sum(1 for r in results if r["fp_mp4"].get("ok") and r["fp_mp4"].get("tier") == "2")

    print("=" * 70)
    print(f"TOTAL SITES TESTED: {n}")
    print(f"  yt-dlp metadata extract OK:        {n_yt_ok:3d}/{n} ({n_yt_ok/n:.0%})")
    print(f"    of which has video format:       {n_video:3d}/{n} ({n_video/n:.0%})")
    print(f"    of which has audio format:       {n_audio:3d}/{n} ({n_audio/n:.0%})")
    print(f"  fast-path resolver (mp4) WIN:      {n_fp_any:3d}/{n} ({n_fp_any/n:.0%})")
    print(f"    tier 0 (direct URL, no yt-dlp):  {n_fp_t0:3d}")
    print(f"    tier 1 (yt-dlp -> http MP4):     {n_fp_t1:3d}")
    print(f"    tier 2 (yt-dlp subprocess pipe): {n_fp_t2:3d}")
    print("=" * 70)

    # Persist
    payload = {
        "summary": {
            "total": n,
            "yt_dlp_ok": n_yt_ok,
            "has_video": n_video,
            "has_audio": n_audio,
            "fastpath_mp4_ok": n_fp_any,
            "tier_breakdown": {"0": n_fp_t0, "1": n_fp_t1, "2": n_fp_t2},
        },
        "results": results,
    }
    RESULTS_JSON.write_text(json.dumps(payload, indent=2))
    print(f"\nRaw results: {RESULTS_JSON}")

    # Markdown table
    lines = []
    lines.append(f"# Mini-app coverage stress test - {time.strftime('%Y-%m-%d')}\n")
    lines.append(f"Sites tested: **{n}**  ·  yt-dlp metadata: **{n_yt_ok}** "
                 f"({n_yt_ok/n:.0%})  ·  fast-path mp4 win: **{n_fp_any}** "
                 f"({n_fp_any/n:.0%})  ·  yt-dlp version: " + __import__("yt_dlp").version.__version__)
    lines.append(f"\nTier breakdown: T0 {n_fp_t0} · T1 {n_fp_t1} · T2 {n_fp_t2}\n")
    lines.append(f"\n| # | Site | yt-dlp | V/A | mp4 fast-path | mp3 (audio in slow-path?) | Note |")
    lines.append("|---|------|--------|-----|---------------|---------------------------|------|")
    for i, r in enumerate(sorted(results, key=lambda x: x["name"].lower()), 1):
        yt = r["yt"]
        fp = r["fp_mp4"]
        yt_cell = "OK" if yt.get("ok") else "FAIL"
        if yt.get("ok"):
            v = "V" if yt.get("has_video") else "-"
            a = "A" if yt.get("has_audio") else "-"
            va = f"{v}{a}"
        else:
            va = "-"
        if fp.get("ok"):
            fp_cell = f"tier {fp['tier']} ({fp.get('method', '?')})"
        else:
            fp_cell = "no"
        mp3_cell = "yes" if (yt.get("ok") and yt.get("has_audio")) else "no"
        note = ""
        if not yt.get("ok"):
            note = (yt.get("err") or "")[:90].replace("|", "/")
        lines.append(f"| {i} | {r['name']} | {yt_cell} | {va} | {fp_cell} | {mp3_cell} | {note} |")
    RESULTS_MD.write_text("\n".join(lines))
    print(f"Per-site table: {RESULTS_MD}")


if __name__ == "__main__":
    main()
