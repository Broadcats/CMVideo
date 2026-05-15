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

**1. Create the Space** (one-time).

Either through the web UI at <https://huggingface.co/new-space>:

- Owner: your account (e.g. `Broadcats`)
- Space name: `cmvideo-mini`
- License: leave as-is
- Space SDK: **Docker** → "Blank"
- Visibility: Public
- Hardware: CPU basic (free)

Or via the `huggingface_hub` CLI:

```bash
pip install huggingface_hub
huggingface-cli login              # paste a token from huggingface.co/settings/tokens
huggingface-cli repo create cmvideo-mini --type space --space_sdk docker
```

**2. Push the `web-mini/` directory as the Space root.**

The Space repo lives at `https://huggingface.co/spaces/<user>/cmvideo-mini`.

```bash
# Clone the (empty) Space repo somewhere convenient.
git clone https://huggingface.co/spaces/<user>/cmvideo-mini hf-cmvideo-mini
cd hf-cmvideo-mini

# Copy the mini app in.
cp -r /path/to/CMVideo/web-mini/. .

# Push.
git add -A
git commit -m "Initial deploy of CMVideo Mini"
git push
```

HF will start building the Docker image as soon as you push. Build +
start takes ~2-4 minutes the first time. Once it's running, the Space
is reachable at:

- `https://<user>-cmvideo-mini.hf.space` (direct app URL, no chrome)
- `https://huggingface.co/spaces/<user>/cmvideo-mini` (with HF chrome)

The cmvideo.online "Try the mini web version" CTA points at the direct
`.hf.space` URL.

**3. Update the link on cmvideo.online.**

Once you have your actual `<user>-cmvideo-mini.hf.space` URL, search
the main repo for the placeholder and replace it. The cmvideo.online
hero embeds the widget directly via cross-origin fetch, so the
primary place to update is `site/app.js`:

```bash
cd /path/to/CMVideo
grep -rln 'broadcats-cmvideo-mini.hf.space' site/ web-mini/
# matches: site/app.js  (MINI_API_BASE constant)
# edit the const to use your real Space URL
```

The Space's own root URL (`<user>-cmvideo-mini.hf.space`) still
serves the same widget at `/` as a fallback for visitors who land
on it directly.

**4. Updating the Space later.**

Any time you change `web-mini/`, just rsync into the `hf-cmvideo-mini`
clone and push again:

```bash
rsync -a --delete --exclude .git /path/to/CMVideo/web-mini/. hf-cmvideo-mini/
cd hf-cmvideo-mini
git add -A
git commit -m "Update mini app"
git push
```

HF will rebuild automatically.

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
