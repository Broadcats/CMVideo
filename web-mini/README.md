---
title: CMVideo Mini
emoji: "✂"
colorFrom: indigo
colorTo: purple
sdk: docker
pinned: false
app_port: 7860
short_description: URL → MP4/MP3 (mini web version of cmvideo.online)
---

# CMVideo Mini

The browser-only slice of [CMVideo](https://cmvideo.online): paste a
video URL, get a clean **MP4** or **MP3** back. Powered by
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp) + `ffmpeg` running in a
Docker container on Hugging Face Spaces.

This service is intentionally limited so it can stay free:

| Cap | Value |
|---|---|
| Max duration per clip | 30 min |
| Max output filesize   | 200 MB |
| Max video resolution  | 720p MP4 |
| Max audio bitrate     | 192 kbps MP3 |
| Per-IP rate limit     | 5 downloads / hour |
| Job timeout           | 120 s |

For full quality (up to 4K / 320 kbps), every format (MP4, MOV, MKV,
WebM, AVI, FLV, MP3, M4A, AAC, OGG, Opus, WAV, FLAC), batch processing,
custom wordlists, and the actual **censoring** features (silence, beep,
retro robotic TTS), use the desktop app at
[cmvideo.online](https://cmvideo.online). Free, open source, runs
locally.

---

## Layout

```
web-mini/
├── Dockerfile          # python:3.11-slim + ffmpeg
├── app.py              # FastAPI app
├── requirements.txt
├── templates/index.html
├── static/style.css
├── static/app.js
└── README.md           # this file (also the HF Space card)
```

## Endpoints

| Path             | Method | What it does                                    |
|------------------|--------|-------------------------------------------------|
| `/`              | GET    | The mini-app UI                                 |
| `/api/info`      | POST   | `{url}` → title, duration, thumbnail, etc.      |
| `/api/download`  | POST   | `{url, format: "mp4"|"mp3"}` → streams the file |
| `/api/limits`    | GET    | JSON dump of the current caps                   |
| `/healthz`       | GET    | Liveness probe                                  |

---

## Local development

```bash
cd web-mini
docker build -t cmvideo-mini .
docker run --rm -p 7860:7860 cmvideo-mini
# open http://localhost:7860
```

Or without docker:

```bash
cd web-mini
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 7860
```

(You'll need `ffmpeg` installed on the host for the latter.)

---

## Deploying to Hugging Face Spaces (the free host)

The Space is configured via the YAML frontmatter at the top of this
file (`sdk: docker`, `app_port: 7860`, etc.) so deployment is just
"push these files to the Space repo".

### Quick path (recommended)

There's a one-shot script in `scripts/deploy-mini.py` that creates the
Space if it's missing and uploads everything in one go:

```bash
# 1. Generate a Write-access token at https://huggingface.co/settings/tokens
# 2. Run the deploy script:
HF_TOKEN=hf_xxx python3 scripts/deploy-mini.py
```

It defaults to `Broadcats/cmvideo-mini`; override with
`--owner <user>` and `--space <name>` if needed, or pass `--dry-run` to
see what would ship without touching HF.

HF will start building the Docker image as soon as the upload lands.
Build + start takes ~2-4 minutes the first time and ~1-2 minutes on
updates. Once it's running, the Space is reachable at:

- `https://<owner-lower>-<space>.hf.space` (direct app URL, no chrome)
- `https://huggingface.co/spaces/<owner>/<space>` (with HF chrome)

The widget on cmvideo.online points at the direct `.hf.space` URL
(currently `https://broadcats-cmvideo-mini.hf.space`). If you deploy
under a different name, search the main repo for that string and
update it:

```bash
grep -rln 'broadcats-cmvideo-mini.hf.space' site/ web-mini/
```

### Manual path

If you'd rather drive HF yourself: create the Space at
<https://huggingface.co/new-space> (Owner: `Broadcats`, Name:
`cmvideo-mini`, SDK: **Docker** → Blank, Visibility: Public, Hardware:
CPU basic / free), then push the directory the usual git way:

```bash
git clone https://huggingface.co/spaces/<owner>/cmvideo-mini hf-cmvideo-mini
rsync -a --delete --exclude .git /path/to/CMVideo/web-mini/. hf-cmvideo-mini/
cd hf-cmvideo-mini && git add -A && git commit -m "Deploy" && git push
```

HF will rebuild automatically on every push.

---

## Tweaking the caps

All cap constants live at the top of `app.py`:

```python
MAX_DURATION_SECONDS = 30 * 60
MAX_FILESIZE_BYTES = 200 * 1024 * 1024
MAX_VIDEO_HEIGHT = 720
AUDIO_BITRATE_KBPS = "192"
JOB_TIMEOUT_SECONDS = 120
RATE_LIMIT_PER_HOUR = "5/hour"
```

Lower = less abuse risk, less compute. Higher = better mini-app, but
defeats the "encourage the full download" goal of this service.
