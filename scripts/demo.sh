#!/usr/bin/env bash
#
# scripts/demo.sh — drive the firm through a full demo flow.
#
# Idempotent. Safe to re-run. Prints progress, summarizes at the end.
#
# Steps:
#   1. Verify the stack is up (or bring it up).
#   2. Health probe.
#   3. Render an Excel daily report.
#   4. Run the eval harness.
#   5. Print URLs the interviewer should open.

set -Eeuo pipefail
trap 'echo "[demo] FAILED at line $LINENO" >&2' ERR

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

HOST_PORT="${HOST_HTTP_PORT:-8080}"
BASE_URL="http://127.0.0.1:${HOST_PORT}"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
info()  { printf '\033[36m[demo]\033[0m %s\n' "$*"; }
ok()    { printf '\033[32m[ ok ]\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m[warn]\033[0m %s\n' "$*"; }

bold "===================================================="
bold " Horizon Capital — demo runner"
bold "===================================================="

# ----- Step 1: Stack -----
info "Step 1: ensure stack is up (docker compose ps)"
if ! docker compose ps --status running --services 2>/dev/null | grep -q '^web$'; then
    warn "web is not running — running 'make up'"
    docker compose up -d
    info "waiting 15s for services to settle…"
    sleep 15
else
    ok "web is already running"
fi

# ----- Step 2: Health -----
info "Step 2: /healthz + /readyz"
if curl -fsS "${BASE_URL}/healthz" >/dev/null; then
    ok "healthz returned 200"
else
    warn "healthz failed — falling back to direct web port"
    BASE_URL="http://127.0.0.1:8000"
    curl -fsS "${BASE_URL}/healthz" >/dev/null && ok "direct web healthz OK"
fi
echo "  readyz payload:"
curl -fsS "${BASE_URL}/readyz" | python3 -m json.tool | sed 's/^/    /'

# ----- Step 3: Daily report (Excel) -----
info "Step 3: render Excel daily report (channel #2)"
docker compose exec -T web python -m app.reports --demo
ok "daily.xlsx written to artifacts/reports/<date>/"

# ----- Step 4: Eval harness -----
info "Step 4: run eval harness (portfolio + process metrics)"
docker compose exec -T web python -m evals.run --window sample --out /app/artifacts/eval.json
ok "eval report written to artifacts/eval.json"

# ----- Step 5: URLs -----
bold ""
bold "===================================================="
bold " DEMO READY"
bold "===================================================="
echo "  Firm UI:        ${BASE_URL}"
echo "  Health:         ${BASE_URL}/healthz"
echo "  Ready:          ${BASE_URL}/readyz"
echo "  Metrics:        ${BASE_URL}/metrics"
echo "  Daily report:   ${BASE_URL}/reports/daily.xlsx"
echo "  Daily report (JSON): ${BASE_URL}/reports/daily.json"
echo ""
echo "  Grafana (if obs-up): http://127.0.0.1:3000  (admin/admin)"
echo "  Prometheus:          http://127.0.0.1:9090"
echo ""
echo "  Trigger a news event:  ${BASE_URL}/trigger"
echo "  HITL queue:            ${BASE_URL} (top of home page)"
