#!/usr/bin/env bash
# One-shot setup: deploy Cobalt to Fly.io, inject an API key, and
# print the two env vars you need to add to the HF Space.
#
# Prerequisites:
#   brew install flyctl   (or curl https://fly.io/install.sh | sh)
#   fly auth login
#
# Usage:
#   bash scripts/cobalt/setup.sh [app-name]
#
# [app-name] defaults to the value in fly.toml ("cmv-cobalt") but
# must be globally unique on fly.io — pick something like
# "cmv-cobalt-yourname" if the default is taken.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLY_TOML="$SCRIPT_DIR/fly.toml"

# ── 1. Determine app name ──────────────────────────────────────────
APP_NAME="${1:-}"
if [[ -z "$APP_NAME" ]]; then
    APP_NAME="$(grep '^app ' "$FLY_TOML" | head -1 | sed 's/app = "\(.*\)"/\1/')"
fi
echo "[cobalt-setup] App name: $APP_NAME"

# ── 2. Check flyctl ────────────────────────────────────────────────
FLY="$(command -v fly 2>/dev/null || command -v flyctl 2>/dev/null || echo "/home/dbcomp/.fly/bin/flyctl")"
if [[ ! -x "$FLY" ]]; then
    echo "[cobalt-setup] ERROR: flyctl not found at $FLY"
    echo "  Install: curl -L https://fly.io/install.sh | sh"
    exit 1
fi


# API_KEY is unused (no-auth mode) but kept in the output so
# future auth can be added without changing the HF Space wiring.
API_KEY="(no-auth)"

# ── 4. Create or verify the Fly.io app ────────────────────────────
if "$FLY" apps list 2>/dev/null | grep -q "^$APP_NAME"; then
    echo "[cobalt-setup] App '$APP_NAME' already exists — redeploying."
else
    echo "[cobalt-setup] Creating app '$APP_NAME' ..."
    "$FLY" apps create "$APP_NAME" --machines
fi

# ── 5. Set secrets ────────────────────────────────────────────────
APP_URL="https://${APP_NAME}.fly.dev"
echo "[cobalt-setup] Setting API_URL on $APP_NAME ..."
"$FLY" secrets set "API_URL=$APP_URL" --app "$APP_NAME"

# ── 6. Deploy ─────────────────────────────────────────────────────
echo "[cobalt-setup] Deploying image ghcr.io/imputnet/cobalt:10 ..."
"$FLY" deploy \
    --app "$APP_NAME" \
    --config "$FLY_TOML" \
    --image "ghcr.io/imputnet/cobalt:10" \
    --ha=false \
    --wait-timeout 120

# ── 7. Smoke test ─────────────────────────────────────────────────
echo "[cobalt-setup] Smoke-testing the API ..."
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "$APP_URL" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    -H "Authorization: Api-Key $API_KEY" \
    -d '{"url":"https://www.youtube.com/watch?v=jNQXAC9IVRw"}' \
    --max-time 15 || echo "000")

if [[ "$HTTP_STATUS" == "200" ]]; then
    echo "[cobalt-setup] ✓ API responding (HTTP 200)"
elif [[ "$HTTP_STATUS" == "000" ]]; then
    echo "[cobalt-setup] WARN: curl timed out — app may still be starting. Wait 30s and retry."
else
    echo "[cobalt-setup] WARN: unexpected HTTP $HTTP_STATUS — check 'fly logs --app $APP_NAME'"
fi

# ── 8. Print HF Space env vars ────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  Add these two secrets to your HF Space:"
echo "  (Space Settings → Variables and Secrets → New secret)"
echo ""
echo "  COBALT_API_BASE = $APP_URL"
echo "  COBALT_API_KEY  = $API_KEY"
echo ""
echo "  Or run deploy-mini.py with:"
echo "  COBALT_API_BASE=$APP_URL COBALT_API_KEY=$API_KEY python3 scripts/deploy-mini.py"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "[cobalt-setup] Done. Cobalt is live at $APP_URL"
