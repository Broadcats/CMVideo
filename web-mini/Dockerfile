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
        ffmpeg ca-certificates curl tar git gnupg \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 libcairo2 libasound2 libdrm2 libxshmfence1 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 - needed for the PoToken provider sidecar that mints
# proof-of-origin tokens for YouTube's bot challenge. Without
# PoTokens, datacenter IPs (which is what HF Spaces all are) get
# 'Sign in to confirm you're not a bot' on a sizable chunk of
# YouTube videos. With PoTokens, most of those clear without
# user-supplied cookies. This is the same trick y2down / cobalt
# use on their backends.
#
# Install is best-effort: a missing Node still produces a working
# image, the entrypoint just skips the sidecar and falls back to
# the existing client-rotation behavior.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    || echo "WARN: Node.js install failed; PoToken sidecar will be skipped at runtime"

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
# yt-dlp ships extractor fixes daily to the nightly channel
# (`*.dev0` versions on PyPI); the stable cadence is monthly.
# `--pre` opts into the nightly channel - May 2026 stress test
# showed Eporner / RedTube / XHamster / PBS / NBC / Aparat all
# fixed in nightly but still broken in last-stable.
RUN pip install --no-cache-dir --upgrade --pre 'yt-dlp[default,curl-cffi]' \
    && pip install --no-cache-dir --upgrade 'gallery-dl' 'streamlink' 'you-get'

# PoToken provider plugin for yt-dlp. Auto-detected at runtime;
# if the Node sidecar (built below) is reachable on
# localhost:4416 the plugin mints PoTokens for YouTube requests,
# which clears most 'Sign in to confirm you're not a bot' walls
# from datacenter IPs. If the sidecar is missing, the plugin
# no-ops and yt-dlp falls back to its built-in client rotation.
RUN pip install --no-cache-dir bgutil-ytdlp-pot-provider \
    || echo "WARN: bgutil-ytdlp-pot-provider plugin install failed; YouTube without user cookies may be flaky"

# Build the bgutil PoToken provider Node service. We pin to a
# release tag so a regression upstream can't break our image
# build silently. Build artifacts land in /opt/bgutil-server;
# the entrypoint script starts `node build/main.js` on port
# 4416 before uvicorn comes up.
ARG BGUTIL_VERSION=1.3.1
RUN if command -v node >/dev/null 2>&1; then \
        set -e; \
        echo "Building bgutil-ytdlp-pot-provider v${BGUTIL_VERSION}"; \
        mkdir -p /opt/bgutil-server && cd /opt/bgutil-server; \
        curl -fsSL "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/${BGUTIL_VERSION}.tar.gz" \
            | tar -xz --strip-components=2 -C . "bgutil-ytdlp-pot-provider-${BGUTIL_VERSION}/server"; \
        npm ci --omit=dev --no-audit --no-fund 2>&1 | tail -20; \
        npm ci --no-audit --no-fund 2>&1 | tail -20; \
        npx tsc 2>&1 | tail -20; \
        rm -rf node_modules/.cache; \
        echo "bgutil-server build complete; entry: /opt/bgutil-server/build/main.js"; \
    else \
        echo "WARN: Node.js not present; skipping bgutil-server build"; \
    fi || echo "WARN: bgutil-server build failed; runtime will skip the sidecar"
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

COPY app.py mini_censor.py extractors.py llm_extract.py proxy_router.py version.py /app/
COPY entrypoint.sh /app/entrypoint.sh
COPY wordlists /app/wordlists
COPY templates /app/templates
COPY static /app/static
RUN chmod +x /app/entrypoint.sh

# /opt/bgutil-server is built earlier; fold it into the chown so
# the unprivileged `user` can read & exec the Node entry point.
RUN chown -R user:user /app /opt/ms-playwright 2>/dev/null || chown -R user:user /app
RUN [ -d /opt/bgutil-server ] && chown -R user:user /opt/bgutil-server || true
USER user

EXPOSE 7860
ENV CMVIDEO_POTOKEN_PORT=4416
CMD ["/app/entrypoint.sh"]
