#!/usr/bin/env bash
# Smoke-test a running Cobalt instance.
# Usage:
#   bash scripts/cobalt/verify.sh <api-url> <api-key>
#
# Or from env:
#   COBALT_API_BASE=https://... COBALT_API_KEY=abc bash scripts/cobalt/verify.sh
#
# Tests a short YouTube video. Exit 0 = healthy, 1 = broken.

set -euo pipefail

API_URL="${1:-${COBALT_API_BASE:-}}"
API_KEY="${2:-${COBALT_API_KEY:-}}"

if [[ -z "$API_URL" ]]; then
    echo "Usage: $0 <api-url> <api-key>"
    echo "  Or:  COBALT_API_BASE=... COBALT_API_KEY=... $0"
    exit 1
fi

API_URL="${API_URL%/}"
echo "[cobalt-verify] Testing $API_URL ..."

# ── 1. Health check (GET /) ────────────────────────────────────────
HEALTH=$(curl -s --max-time 10 "$API_URL" \
    -H "Accept: application/json" \
    ${API_KEY:+-H "Authorization: Api-Key $API_KEY"} || echo "CURL_FAIL")

if echo "$HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print('version:', d.get('cobalt',{}).get('version','?'))" 2>/dev/null; then
    echo "[cobalt-verify] ✓ Health check passed"
else
    echo "[cobalt-verify] Health response: ${HEALTH:0:200}"
fi

# ── 2. YouTube test (Me at the Zoo, 19s) ─────────────────────────
echo "[cobalt-verify] Testing YouTube extraction ..."
RESPONSE=$(curl -s --max-time 20 -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    ${API_KEY:+-H "Authorization: Api-Key $API_KEY"} \
    -d '{"url":"https://www.youtube.com/watch?v=jNQXAC9IVRw","videoQuality":"720","downloadMode":"auto"}')

STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "parse_error")
URL=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('url',''))" 2>/dev/null || echo "")

if [[ "$STATUS" == "redirect" || "$STATUS" == "tunnel" || "$STATUS" == "stream" ]]; then
    echo "[cobalt-verify] ✓ YouTube → status=$STATUS url=${URL:0:60}..."
    EXIT=0
else
    echo "[cobalt-verify] ✗ YouTube → status=$STATUS"
    echo "  Full response: ${RESPONSE:0:300}"
    EXIT=1
fi

# ── 3. Twitter/X test ─────────────────────────────────────────────
echo "[cobalt-verify] Testing Twitter/X extraction ..."
TW=$(curl -s --max-time 15 -X POST "$API_URL" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json" \
    ${API_KEY:+-H "Authorization: Api-Key $API_KEY"} \
    -d '{"url":"https://x.com/NASA/status/1785701540481503371","downloadMode":"auto"}')

TW_STATUS=$(echo "$TW" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "?")
if [[ "$TW_STATUS" == "redirect" || "$TW_STATUS" == "tunnel" || "$TW_STATUS" == "stream" || "$TW_STATUS" == "picker" ]]; then
    echo "[cobalt-verify] ✓ Twitter/X → status=$TW_STATUS"
else
    echo "[cobalt-verify] ✗ Twitter/X → status=$TW_STATUS (${TW:0:120})"
    EXIT=1
fi

echo ""
[[ "$EXIT" == "0" ]] && echo "[cobalt-verify] All tests passed ✓" || echo "[cobalt-verify] Some tests failed ✗"
exit "$EXIT"
