#!/usr/bin/env python3
"""Battery C re-run with proper synchronous-body capture.

The first Battery C run proved the /api/process pipeline COMPLETES
(every response was 200 with a valid `ftyp isom` MP4 / `ID3` MP3
body), but the test script discarded the bytes because it expected
JSON. This re-run saves each response body to disk and runs ffprobe
to verify the file actually contains usable streams with the
requested settings.

Pace: 35s between jobs (2/min limiter has ~1.5/min headroom).
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

import requests

BASE = "https://dandyfeet-cmvideo-mini.hf.space"
TMPDIR = Path("/tmp/cmvm_e2e_c2")
TMPDIR.mkdir(exist_ok=True)
RESULTS = Path(__file__).with_name("results_e2e_c2.json")

TEST_URL = "https://download.samplelib.com/mp4/sample-15s.mp4"

# Subset of the original 7 to stay well under the 20/hour limiter
# while still covering each axis (fps override, mode, format).
COMBOS = [
    # (label, format, mode, fps, quality, expected_video, expected_audio)
    ("baseline 720p source-fps download", "mp4", "download", "source", "standard", True, True),
    ("30 fps override 720p",              "mp4", "download", "30",     "standard", True, True),
    ("60 fps override 720p",              "mp4", "download", "60",     "standard", True, True),
    ("1080p source-fps",                  "mp4", "download", "source", "hd",       True, True),
    ("silence-mode censor 720p",          "mp4", "silence",  "source", "standard", True, True),
    ("beep-mode censor 720p",             "mp4", "beep",     "source", "standard", True, True),
    ("mp3 320kbps download",              "mp3", "download", "source", "standard", False, True),
]


def ffprobe(path: Path) -> dict:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=format_name,duration,size,bit_rate",
             "-show_streams", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return {"error": r.stderr.strip()[:200]}
        info = json.loads(r.stdout or "{}")
        fmt = info.get("format", {}) or {}
        streams = info.get("streams", []) or []
        v = next((s for s in streams if s.get("codec_type") == "video"), {})
        a = next((s for s in streams if s.get("codec_type") == "audio"), {})
        return {
            "format_name": fmt.get("format_name"),
            "duration_s": round(float(fmt.get("duration", 0) or 0), 2),
            "size": int(fmt.get("size", 0) or 0),
            "bitrate_bps": int(fmt.get("bit_rate", 0) or 0),
            "n_streams": len(streams),
            "video": {
                "codec": v.get("codec_name"),
                "width": v.get("width"),
                "height": v.get("height"),
                "fps_avg": v.get("avg_frame_rate"),
                "fps_r": v.get("r_frame_rate"),
            } if v else None,
            "audio": {
                "codec": a.get("codec_name"),
                "sample_rate": a.get("sample_rate"),
                "channels": a.get("channels"),
                "bitrate": a.get("bit_rate"),
            } if a else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def submit(combo) -> dict:
    label, fmt, mode, fps, quality, exp_v, exp_a = combo
    print(f"\n{label}")
    t0 = time.monotonic()
    try:
        r = requests.post(f"{BASE}/api/process",
                          data={"url": TEST_URL, "format": fmt,
                                "mode": mode, "fps": fps, "quality": quality},
                          timeout=180, stream=True)
    except requests.RequestException as exc:
        print(f"  request err: {exc}")
        return {"label": label, "err": str(exc)[:200]}
    dt = time.monotonic() - t0
    if r.status_code != 200:
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:300]}
        print(f"  HTTP {r.status_code} ({dt:.1f}s) - {body}")
        r.close()
        return {"label": label, "status": r.status_code, "body": body, "dt": round(dt, 2)}
    # 200 - save bytes, ffprobe
    safe = label.replace(" ", "_").replace("/", "-")[:60]
    out = TMPDIR / f"{safe}.{fmt}"
    written = 0
    with out.open("wb") as fh:
        for chunk in r.iter_content(chunk_size=128 * 1024):
            if chunk:
                fh.write(chunk)
                written += len(chunk)
    r.close()
    info = ffprobe(out)
    print(f"  HTTP 200 ({dt:.1f}s)  {written:,} B  -> {out.name}")
    if "error" in info:
        print(f"  ffprobe ERROR: {info['error']}")
    else:
        v = info.get("video") or {}
        a = info.get("audio") or {}
        meta_bits = []
        if v:
            meta_bits.append(f"v={v.get('codec')} {v.get('width')}x{v.get('height')} {v.get('fps_avg')}")
        if a:
            meta_bits.append(f"a={a.get('codec')} {a.get('sample_rate')}Hz {a.get('channels')}ch")
        print(f"  ffprobe: {info.get('format_name')} dur={info.get('duration_s')}s")
        print(f"           {' | '.join(meta_bits)}")
    # Pass/fail based on expected stream presence
    has_video = bool(info.get("video"))
    has_audio = bool(info.get("audio"))
    ok = (has_video == exp_v) and (has_audio == exp_a) and ("error" not in info)
    return {
        "label": label, "combo": combo, "status": 200, "dt": round(dt, 2),
        "bytes": written, "ffprobe": info,
        "expected_video": exp_v, "expected_audio": exp_a,
        "got_video": has_video, "got_audio": has_audio, "ok": ok,
    }


def main():
    print(f"Test URL: {TEST_URL}")
    print(f"Output dir: {TMPDIR}")
    out = []
    for i, combo in enumerate(COMBOS):
        rec = submit(combo)
        out.append(rec)
        if i < len(COMBOS) - 1:
            time.sleep(35)
    RESULTS.write_text(json.dumps(out, indent=2, default=str))
    print()
    print("=" * 70)
    n_ok = sum(1 for r in out if r.get("ok"))
    print(f"Battery C re-run: {n_ok}/{len(out)} combos produced valid output")
    print("=" * 70)
    for r in out:
        tag = "ok " if r.get("ok") else "BAD"
        v = "v" if r.get("got_video") else "-"
        a = "a" if r.get("got_audio") else "-"
        ev = "v" if r.get("expected_video") else "-"
        ea = "a" if r.get("expected_audio") else "-"
        size_kb = (r.get("bytes") or 0) // 1024
        dur = (r.get("ffprobe") or {}).get("duration_s")
        print(f"  {tag}  {v}{a}/{ev}{ea}  {size_kb:>6} KB  dur={dur}s  {r['label']}")
    print(f"\nResults -> {RESULTS}")


if __name__ == "__main__":
    main()
