#!/usr/bin/env python3
"""End-to-end stress test against the deployed mini-app Space.

Three batteries:

  A. Real download via fast-path - hits /api/stream-download, then
     consumes 1 MB from the resulting /api/stream/{token} URL,
     verifies MP4 magic bytes (`ftyp`). Tests the FULL streaming
     pipeline including residential proxy + the recent tier 0/1/2
     resolver work. ~10 sites, paced to respect 5/min rate limit.

  B. Embed URL probe - exercises the fast-path resolver against
     `*/embed/<id>/` URLs from sites that support both canonical
     and embed forms (ThisVid, RedTube, YouTube). 200 = embed
     supported; 422 = falls back to slow path. Doesn't actually
     stream, just probes.

  C. Render-settings combinations through /api/process - submits a
     job with each combo, polls /api/jobs/{id} until done, fetches
     the resulting file, runs ffprobe to verify the output is
     valid. ~6 combos paced at >= 30s apart to respect 2/min limit.

Goal: confirm everything that's supposed to work end-to-end
ACTUALLY produces valid bytes, not just "metadata extraction OK".
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import requests

BASE = "https://dandyfeet-cmvideo-mini.hf.space"
RESULTS = Path(__file__).with_name("results_e2e.json")
TMPDIR = Path("/tmp/cmvm_e2e")
TMPDIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def post_json(path: str, body: dict, timeout=25) -> tuple[int, dict]:
    try:
        r = requests.post(f"{BASE}{path}", json=body, timeout=timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text[:300]}
    except requests.RequestException as exc:
        return 0, {"err": str(exc)[:200]}


def post_form(path: str, data: dict, timeout=25) -> tuple[int, dict]:
    try:
        r = requests.post(f"{BASE}{path}", data=data, timeout=timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text[:300]}
    except requests.RequestException as exc:
        return 0, {"err": str(exc)[:200]}


def get_json(path: str, timeout=15) -> tuple[int, dict]:
    try:
        r = requests.get(f"{BASE}{path}", timeout=timeout)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": r.text[:300]}
    except requests.RequestException as exc:
        return 0, {"err": str(exc)[:200]}


def stream_consume(stream_path: str, max_bytes: int = 1_000_000, timeout=60) -> dict:
    """Fetch up to `max_bytes` from /api/stream/{token} and verify
    we got real bytes. Returns dict with size + first-32-byte hex
    + ftyp-detection flag."""
    try:
        r = requests.get(f"{BASE}{stream_path}", stream=True, timeout=(10, timeout))
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "err": r.text[:200]}
        buf = b""
        ct = r.headers.get("Content-Type", "")
        cl = r.headers.get("Content-Length")
        for chunk in r.iter_content(chunk_size=64 * 1024):
            if not chunk:
                break
            buf += chunk
            if len(buf) >= max_bytes:
                break
        r.close()
        ftyp_at_4 = buf[4:8] == b"ftyp" if len(buf) >= 8 else False
        return {
            "ok": True,
            "status": 200,
            "bytes_received": len(buf),
            "advertised_length": int(cl) if cl and cl.isdigit() else None,
            "content_type": ct,
            "first8_hex": buf[:8].hex() if buf else "",
            "ftyp_at_offset_4": ftyp_at_4,
        }
    except requests.RequestException as exc:
        return {"ok": False, "err": str(exc)[:200]}


def ffprobe_summary(path: Path) -> dict | None:
    if not path.exists():
        return None
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
            "video_streams": sum(1 for s in streams if s.get("codec_type") == "video"),
            "audio_streams": sum(1 for s in streams if s.get("codec_type") == "audio"),
            "video_codec": next((s.get("codec_name") for s in streams if s.get("codec_type") == "video"), None),
            "audio_codec": next((s.get("codec_name") for s in streams if s.get("codec_type") == "audio"), None),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


# ---------------------------------------------------------------
# Battery A - real downloads via fast path
# ---------------------------------------------------------------
A_SITES = [
    # Picked from results_minisetup.json's fast-path winners,
    # avoiding adult content for a public test report.
    ("Streamable", "https://streamable.com/dnd1"),
    ("Vimeo", "https://vimeo.com/76979871"),
    ("PeerTube", "https://framatube.org/videos/watch/9c9de5e8-0a1e-484a-b099-e80766180a6d"),
    ("BitChute", "https://www.bitchute.com/video/UGlrF9o9b-Q/"),
    ("TED", "https://www.ted.com/talks/candace_parker_how_to_break_down_barriers_and_not_accept_limits"),
    ("Reddit", "https://www.reddit.com/r/videos/comments/6rrwyj/that_small_heart_attack/"),
    ("AlJazeera", "https://balkans.aljazeera.net/videos/2021/11/6/pojedini-domovi-u-sarajevu-jos-pod-vodom-mjestanima-se-dostavlja-hrana"),
    ("CNN", "https://www.cnn.com/2024/05/31/sport/video/jadon-sancho-borussia-dortmund-champions-league-exclusive-spt-intl"),
    ("NFL", "https://www.nfl.com/videos/baker-mayfield-s-game-changing-plays-from-3-td-game-week-14"),
    ("samplelib", "https://download.samplelib.com/mp4/sample-5s.mp4"),  # tier 0 sanity check
]


def battery_a():
    print("\n" + "=" * 70)
    print("BATTERY A - real end-to-end downloads via fast path")
    print("=" * 70)
    out = []
    for i, (name, url) in enumerate(A_SITES, 1):
        print(f"\n[{i:2d}/{len(A_SITES)}] {name}: {url[:70]}")
        # Mint stream token
        t0 = time.monotonic()
        status, body = post_json("/api/stream-download",
                                  {"url": url, "format": "mp4", "quality": "standard"})
        dt = time.monotonic() - t0
        rec = {"name": name, "url": url, "init_status": status, "init_dt": round(dt, 2)}
        if status != 200:
            rec["init_body"] = body
            tag = "init " + str(status)
            print(f"        {tag} ({dt:.1f}s) - {json.dumps(body)[:120]}")
            out.append(rec)
        else:
            stream_url = body.get("stream_url")
            rec["filename"] = body.get("filename")
            rec["filesize_advertised"] = body.get("filesize")
            print(f"        init 200 ({dt:.1f}s) - token minted, filename={body.get('filename')}, size={body.get('filesize')}")
            # Consume up to ~1 MB from the stream
            t1 = time.monotonic()
            consume = stream_consume(stream_url, max_bytes=1_000_000, timeout=45)
            consume["dt"] = round(time.monotonic() - t1, 2)
            rec["stream"] = consume
            if consume.get("ok"):
                ftyp = "yes" if consume.get("ftyp_at_offset_4") else "NO"
                print(f"        stream 200 ({consume['dt']:.1f}s) - "
                      f"{consume['bytes_received']} B, ftyp@4={ftyp}, ct={consume['content_type']}")
            else:
                print(f"        stream FAIL - {json.dumps(consume)[:200]}")
            out.append(rec)
        # Pace: 5 stream requests/min on the limiter, so >= 12s apart
        if i < len(A_SITES):
            time.sleep(13)
    return out


# ---------------------------------------------------------------
# Battery B - embed-URL probe
# ---------------------------------------------------------------
B_URLS = [
    # ThisVid: yt-dlp test confirms /embed/<id>/ is supported
    ("ThisVid embed", "https://thisvid.com/embed/3533241/"),
    ("ThisVid canonical (control)", "https://thisvid.com/videos/sitting-on-ball-tight-jeans/"),
    # YouTube /embed/ form - blocked client-side by the desktop
    # redirect for "censor" mode but allowed for download
    ("YouTube embed", "https://www.youtube.com/embed/jNQXAC9IVRw"),
    ("YouTube watch", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
    # Vimeo embed format
    ("Vimeo player embed", "https://player.vimeo.com/video/76979871"),
]


def battery_b():
    print("\n" + "=" * 70)
    print("BATTERY B - embedded URL probe (fast-path eligibility)")
    print("=" * 70)
    out = []
    for i, (name, url) in enumerate(B_URLS, 1):
        print(f"\n[{i}/{len(B_URLS)}] {name}: {url[:70]}")
        status, body = post_json("/api/stream-download",
                                  {"url": url, "format": "mp4", "quality": "standard"})
        rec = {"name": name, "url": url, "status": status}
        if status == 200:
            print(f"        200 - fast-path eligible (filename={body.get('filename')})")
            rec["filename"] = body.get("filename")
            rec["filesize"] = body.get("filesize")
            rec["fastpath"] = True
        elif status == 422:
            detail = body.get("detail", {})
            reason = detail.get("reason") if isinstance(detail, dict) else "?"
            print(f"        422 - {reason} (would fall back to slow path)")
            rec["fastpath"] = False
            rec["reason"] = reason
        else:
            print(f"        {status} - {json.dumps(body)[:200]}")
            rec["body"] = body
        out.append(rec)
        if i < len(B_URLS):
            time.sleep(13)
    return out


# ---------------------------------------------------------------
# Battery C - settings combinations through /api/process
# ---------------------------------------------------------------
# Use a tiny stable test URL so each job stays under 30s. We're
# verifying the pipeline accepts/processes the combo; we are NOT
# testing censorship correctness (which requires a swear-bearing
# audio track). For silence/beep modes the URL just needs to have
# audio - the words list won't match anything in samplelib's tone
# sweep, so the output will be identical to download mode. That's
# fine - we're verifying the SETTINGS flow through, not the words.
C_TEST_URL = "https://download.samplelib.com/mp4/sample-15s.mp4"
C_COMBOS = [
    # (label, format, mode, fps, quality)
    ("mp4 / download / source-fps / 720p", "mp4", "download", "source", "standard"),
    ("mp4 / download / 30fps / 720p",      "mp4", "download", "30",     "standard"),
    ("mp4 / download / 60fps / 720p",      "mp4", "download", "60",     "standard"),
    ("mp4 / download / source-fps / 1080p", "mp4", "download", "source", "hd"),
    ("mp4 / silence / source / 720p",      "mp4", "silence",  "source", "standard"),
    ("mp4 / beep / source / 720p",         "mp4", "beep",     "source", "standard"),
    ("mp3 / download / 320 / standard",    "mp3", "download", "source", "standard"),
]


def poll_job(job_id: str, max_wait_s: int = 300) -> dict:
    """Poll /api/jobs/{job_id} until ready or error or timeout."""
    deadline = time.monotonic() + max_wait_s
    last_pct = -1
    while time.monotonic() < deadline:
        status, body = get_json(f"/api/jobs/{job_id}")
        if status != 200:
            return {"poll_status": status, "body": body}
        pct = body.get("pct", 0)
        stage = body.get("stage_label") or body.get("stage")
        if pct != last_pct:
            print(f"          poll: stage={stage} pct={pct}", flush=True)
            last_pct = pct
        if body.get("ready"):
            return {"poll_status": 200, "body": body}
        if body.get("error"):
            return {"poll_status": 200, "body": body}
        time.sleep(2.5)
    return {"poll_status": -1, "err": f"timeout after {max_wait_s}s"}


def battery_c():
    print("\n" + "=" * 70)
    print(f"BATTERY C - settings combinations via /api/process")
    print(f"Test URL: {C_TEST_URL}")
    print("=" * 70)
    out = []
    for i, combo in enumerate(C_COMBOS, 1):
        label, fmt, mode, fps, quality = combo
        print(f"\n[{i}/{len(C_COMBOS)}] {label}")
        # Submit. /api/process is synchronous by default - it returns the
        # rendered file directly. We pass async=1 to opt into the job
        # model so we can ffprobe-verify intermediate state.
        t0 = time.monotonic()
        try:
            r = requests.post(
                f"{BASE}/api/process?async=1",
                data={
                    "url": C_TEST_URL,
                    "format": fmt,
                    "mode": mode,
                    "fps": fps,
                    "quality": quality,
                },
                timeout=30,
            )
            status = r.status_code
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text[:300]}
        except requests.RequestException as exc:
            status, body = 0, {"err": str(exc)[:200]}

        if status != 200:
            print(f"          submit FAIL: {status} {json.dumps(body)[:200]}")
            out.append({"combo": combo, "submit_status": status, "submit_body": body})
            time.sleep(35)
            continue
        job_id = body.get("job_id")
        if not job_id:
            print(f"          submit OK but no job_id: {body}")
            out.append({"combo": combo, "submit_status": status, "submit_body": body})
            time.sleep(35)
            continue
        print(f"          submit 200 ({time.monotonic()-t0:.1f}s) - job_id={job_id[:12]}...")
        # Poll
        poll = poll_job(job_id, max_wait_s=240)
        rec = {"combo": combo, "submit_status": status, "job_id": job_id, "poll": poll}
        b = (poll.get("body") or {})
        if b.get("ready"):
            print(f"          ready - filename={b.get('filename')}, "
                  f"bytes={b.get('bytes_done')}, time={time.monotonic()-t0:.1f}s")
            # Fetch file
            try:
                r = requests.get(f"{BASE}/api/jobs/{job_id}/file", timeout=120, stream=True)
                if r.status_code == 200:
                    out_path = TMPDIR / f"{i:02d}_{fmt}_{mode}_{fps}_{quality}.{fmt}"
                    written = 0
                    with out_path.open("wb") as fh:
                        for chunk in r.iter_content(chunk_size=128 * 1024):
                            if chunk:
                                fh.write(chunk)
                                written += len(chunk)
                    print(f"          downloaded {written} B -> {out_path.name}")
                    rec["downloaded_bytes"] = written
                    rec["ffprobe"] = ffprobe_summary(out_path)
                    if rec["ffprobe"]:
                        fp = rec["ffprobe"]
                        print(f"          ffprobe: {fp.get('format_name')} "
                              f"v={fp.get('video_codec')} a={fp.get('audio_codec')} "
                              f"dur={fp.get('duration_s'):.1f}s")
                else:
                    rec["file_status"] = r.status_code
                    print(f"          file fetch FAIL: {r.status_code}")
            except Exception as exc:  # noqa: BLE001
                rec["file_err"] = str(exc)[:200]
                print(f"          file fetch err: {exc}")
        elif b.get("error"):
            print(f"          job ERROR: {b.get('error')[:200]}")
        else:
            print(f"          poll outcome: {poll}")
        out.append(rec)
        # Pace: 2 jobs/min on /api/process, 35s gap is safe
        if i < len(C_COMBOS):
            time.sleep(35)
    return out


def main():
    a = battery_a()
    b = battery_b()
    c = battery_c()
    payload = {"battery_a": a, "battery_b": b, "battery_c": c, "ts": int(time.time())}
    RESULTS.write_text(json.dumps(payload, indent=2, default=str))
    # Final summary banner
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    a_ok = sum(1 for r in a if r.get("stream", {}).get("ok") and r["stream"].get("ftyp_at_offset_4"))
    a_init_ok = sum(1 for r in a if r.get("init_status") == 200)
    print(f"  Battery A: real fast-path downloads")
    print(f"      init success:   {a_init_ok}/{len(a)}")
    print(f"      stream + ftyp:  {a_ok}/{len(a)}  (verified actual valid MP4 bytes)")
    b_ok = sum(1 for r in b if r.get("fastpath"))
    print(f"  Battery B: embed URLs")
    print(f"      fast-path eligible: {b_ok}/{len(b)}")
    c_ok = sum(1 for r in c if (r.get("poll", {}).get("body", {}) or {}).get("ready"))
    c_valid = sum(1 for r in c if r.get("ffprobe") and not r["ffprobe"].get("error"))
    print(f"  Battery C: render-settings combinations")
    print(f"      job ready:        {c_ok}/{len(c)}")
    print(f"      ffprobe-valid file: {c_valid}/{len(c)}")
    print(f"\nFull results -> {RESULTS}")


if __name__ == "__main__":
    main()
