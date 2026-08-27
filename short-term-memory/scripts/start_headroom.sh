#!/usr/bin/env bash
#
# One-shot: start Headroom proxy and auto warm-up Kompress.
#
# Usage:   bash scripts/start_headroom.sh
# Env:     HEADROOM_HOST, HEADROOM_PORT, OPENAI_API_URL, HEADROOM_CCR_TTL_SECONDS
#
# Behaviour:
#   1. If Headroom is already listening on the port, skip starting and just warm up.
#   2. Otherwise start `headroom proxy` in the background and wait for /health.
#   3. Fire one /v1/compress request to trigger lazy Kompress model load.
#   4. Poll /health until kompress.ready == true, then stay in the foreground.
#
set -u

HEADROOM_HOST="${HEADROOM_HOST:-127.0.0.1}"
HEADROOM_PORT="${HEADROOM_PORT:-8787}"
OPENAI_API_URL="${OPENAI_API_URL:-https://api.deepseek.com}"
export HEADROOM_CCR_TTL_SECONDS="${HEADROOM_CCR_TTL_SECONDS:-43200}"

HEALTH_URL="http://${HEADROOM_HOST}:${HEADROOM_PORT}/health"
COMPRESS_URL="http://${HEADROOM_HOST}:${HEADROOM_PORT}/v1/compress"

# --- 0. Is Headroom already up? -------------------------------------------------
if curl -s --max-time 2 "${HEALTH_URL}" >/dev/null 2>&1; then
    echo "Headroom already running at ${HEALTH_URL} — skipping start, warming up only."
    PID=""
else
    echo "Starting Headroom proxy on ${HEADROOM_HOST}:${HEADROOM_PORT} ..."
    headroom proxy --host "${HEADROOM_HOST}" --port "${HEADROOM_PORT}" \
        --mode token --openai-api-url "${OPENAI_API_URL}" &
    PID=$!

    # Wait up to ~30s for /health to respond.
    for _ in $(seq 1 30); do
        if curl -s --max-time 2 "${HEALTH_URL}" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    if ! curl -s --max-time 2 "${HEALTH_URL}" >/dev/null 2>&1; then
        echo "ERROR: Headroom did not come up in time." >&2
        [ -n "${PID}" ] && kill "${PID}" 2>/dev/null
        exit 1
    fi
    echo "Headroom started (pid ${PID})."
fi

# --- 1. Trigger lazy Kompress load, then wait until ready ------------------------
warmup_sent=0
for i in $(seq 1 90); do
    ready="$(curl -s --max-time 5 "${HEALTH_URL}" 2>/dev/null \
        | python3 -c "import sys,json
try:
    print(json.load(sys.stdin)['checks']['kompress']['ready'])
except Exception:
    print('False')" 2>/dev/null || echo False)"

    if [ "${ready}" = "True" ]; then
        echo "Kompress is ready (after ~$((i * 2))s)."
        break
    fi

    if [ "${warmup_sent}" -eq 0 ]; then
        echo "Sending one warm-up /v1/compress request to load Kompress ..."
        curl -s -X POST "${COMPRESS_URL}" \
            -H "Content-Type: application/json" \
            -d '{"model":"gpt-4o","messages":[{"role":"assistant","content":"Headroom lazy Kompress warm-up text. Repeated repeated repeated repeated repeated repeated repeated repeated repeated repeated text for warmup."}]}' \
            --max-time 300 >/dev/null 2>&1 || true
        warmup_sent=1
    fi
    sleep 2
done

# --- 2. Print final status ---------------------------------------------------------
echo
echo "Headroom status:"
curl -s --max-time 5 "${HEALTH_URL}" \
    | python3 -c "import sys,json
d = json.load(sys.stdin)
print('  status   :', d['status'])
print('  kompress :', d['checks']['kompress']['ready'])
print('  version  :', d['version'])"
echo
echo "Headroom is running. Press Ctrl+C to stop."

# --- 3. Stay in foreground so Ctrl+C stops the proxy we started --------------------
if [ -n "${PID:-}" ]; then
    wait "${PID}"
fi
