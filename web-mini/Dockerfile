FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.cache/huggingface \
    XDG_CACHE_HOME=/app/.cache \
    CMVIDEO_WORDLISTS_DIR=/app/wordlists

# System packages: ffmpeg for the censor pipeline, plus the X / GTK
# stack Chromium needs to run headless inside Docker. Without these
# Playwright's Chromium fails to start with cryptic libnss / libatk
# errors. Pinning explicitly here so the layer is cacheable
# instead of asking `playwright install --with-deps` to do it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates curl tar \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 libdrm2 libxshmfence1 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Pin the lux tarball version. lux is a Go binary, ~12 MB, no
# dependencies. Bumped manually at the same cadence as the
# weekly Mini rebuild workflow when a new lux release lands.
ARG LUX_VERSION=0.24.1
RUN curl -fsSL "https://github.com/iawia002/lux/releases/download/v${LUX_VERSION}/lux_${LUX_VERSION}_Linux_x86_64.tar.gz" \
        | tar -xz -C /usr/local/bin lux \
    && chmod +x /usr/local/bin/lux \
    && /usr/local/bin/lux -v

RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt
# Always pull the freshest yt-dlp regardless of pip cache layers.
# yt-dlp ships extractor fixes weekly; the floor pin in
# requirements.txt is intentionally `>=` so this upgrade actually
# moves forward on each rebuild.
RUN pip install --no-cache-dir --upgrade 'yt-dlp[default,curl-cffi]' 'gallery-dl' 'streamlink' 'you-get'
# Pull Chromium for Playwright. Chromium is ~165 MB compressed
# from Playwright's CDN and ~450 MB on disk. We point
# PLAYWRIGHT_BROWSERS_PATH at /opt so the binaries survive the
# user-switch below (HOME under /home/user gets chowned, /opt
# does not, so this avoids re-downloading on container start).
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN python -m playwright install chromium && python -m playwright install-deps chromium 2>/dev/null || true


# Pre-fetch whisper-tiny.en so the first request doesn't pay the
# ~75 MB model download tax. Bakes the model into the image at build
# time and re-uses HF_HOME for runtime cache.
RUN python - <<'PY'
from faster_whisper import WhisperModel
WhisperModel("tiny.en", device="cpu", compute_type="int8")
print("whisper tiny.en cached")
PY

COPY app.py mini_censor.py extractors.py /app/
COPY wordlists /app/wordlists
COPY templates /app/templates
COPY static /app/static

RUN chown -R user:user /app /opt/ms-playwright 2>/dev/null || chown -R user:user /app
USER user

EXPOSE 7860
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
