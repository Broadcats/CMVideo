#!/bin/sh
# CMVideo Mini container entrypoint.
#
# Starts the bgutil PoToken provider in the background (if it was
# built into the image) and then exec's uvicorn so signals reach
# the right process. The PoToken sidecar mints proof-of-origin
# tokens for YouTube's bot challenge; without it, datacenter IPs
# get 'Sign in to confirm you're not a bot' on most YouTube
# videos. With it, most of those clear without user cookies
# (the same trick y2down/cobalt use).
#
# All sidecar steps are guarded so a missing Node, missing build
# output, or runtime crash of the sidecar doesn't take down the
# main app - we just fall back to the built-in yt-dlp client
# rotation, which still works for many videos.
set -eu

POT_PORT="${CMVIDEO_POTOKEN_PORT:-4416}"
POT_LOG="${CMVIDEO_POTOKEN_LOG:-/tmp/bgutil-server.log}"
POT_BIN="/opt/bgutil-server/build/main.js"

if [ "${CMVIDEO_DISABLE_POTOKEN:-}" = "1" ]; then
    echo "[entrypoint] CMVIDEO_DISABLE_POTOKEN=1 - skipping PoToken sidecar"
elif command -v node >/dev/null 2>&1 && [ -f "$POT_BIN" ]; then
    echo "[entrypoint] starting bgutil PoToken provider on :$POT_PORT"
    # The provider binds to "::"/0.0.0.0 internally - HF Spaces only
    # exposes port 7860 to the public net so 4416 is effectively
    # local. yt-dlp's plugin connects via http://127.0.0.1:$POT_PORT
    # (its default), no extra wiring needed.
    cd /opt/bgutil-server && node "$POT_BIN" --port "$POT_PORT" \
        > "$POT_LOG" 2>&1 &
    POT_PID=$!
    echo "[entrypoint] bgutil-server pid=$POT_PID, log=$POT_LOG"
    # Trap SIGTERM/SIGINT so we kill the sidecar cleanly when the
    # container shuts down.
    trap 'kill "$POT_PID" 2>/dev/null || true' INT TERM
    cd /app
    # Wait up to 30 s for the sidecar to respond on its ping endpoint.
    # The Node process needs time to load the botGuard JS from YouTube
    # and solve the initial challenge before it can mint tokens. Without
    # this wait, yt-dlp's first few YouTube requests may fire before the
    # provider is ready and get no PoToken, triggering a bot-wall failure.
    READY=0
    for i in $(seq 1 30); do
        if curl -sf "http://127.0.0.1:$POT_PORT/ping" >/dev/null 2>&1; then
            echo "[entrypoint] bgutil sidecar ready after ${i}s"
            READY=1
            break
        fi
        sleep 1
    done
    if [ "$READY" = "0" ]; then
        echo "[entrypoint] WARN: bgutil sidecar did not respond within 30s; continuing without PoToken"
    fi
else
    echo "[entrypoint] no bgutil sidecar available (node=$(command -v node || echo missing); bin=$POT_BIN); falling back to yt-dlp client rotation"
fi

exec uvicorn app:app --host 0.0.0.0 --port 7860
