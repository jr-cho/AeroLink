#!/usr/bin/env bash
# End-to-end verification of the M6 exit criteria on the real stack (PLAN E7).
#   scripts/e2e.sh                 # down (keeps volumes), up --build, API checks, browser tests, restart/stop checks, table
#   E2E_SUITE=1 scripts/e2e.sh     # also run the whole backend pytest suite on the host (item 16; ~15 min)
#   E2E_MEDICAL_WINDOW_S=900 ...   # how long item 7 watches the medical_dispatch scenario (default 600)
#   E2E_NO_BUILD=1 ...             # skip `--build` (use the images already built)
# Results: .work/e2e-results/<run>/RESULTS.md (PASS/FAIL table), evidence/*.json, screenshots/, logs/.
# The stack is left RUNNING, started with the .env dashboard login (the run itself uses a throwaway login that is
# valid only until the final caddy restart). Needs docker (OrbStack), mosquitto clients, uv, node + playwright.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH=/usr/local/bin:/opt/homebrew/sbin:/opt/homebrew/bin:$PATH
RUN="${E2E_RUN:-$(date +%Y%m%d-%H%M%S)}"
OUT="$PWD/.work/e2e-results/$RUN"; mkdir -p "$OUT/screenshots" "$OUT/logs"
export E2E_OUT="$OUT" E2E_BASE=https://localhost
E2E_USER="$(grep '^DASHBOARD_USER=' .env | cut -d= -f2-)"; export E2E_USER
E2E_PASS="e2e-$(openssl rand -hex 8)"; export E2E_PASS   # throwaway; the .env hash belongs to the real operator
DASHBOARD_PASSWORD_HASH="$(docker run --rm caddy:2 caddy hash-password --plaintext "$E2E_PASS" | tail -1)"; export DASHBOARD_PASSWORD_HASH
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$OUT/logs/e2e.log"; }

restore() {  # leave the stack running with the real dashboard login from .env
  docker compose exec -T backend python -m aerolink.service.operators disable "${E2E_OPS_USER:-e2e-ops}" >>"$OUT/logs/e2e.log" 2>&1
  unset DASHBOARD_PASSWORD_HASH E2E_PASS E2E_OPS_PASS
  docker compose up -d caddy >>"$OUT/logs/e2e.log" 2>&1 && log "stack left running (dashboard login from .env)"
}
trap restore EXIT

log "run $RUN: docker compose down (volumes kept) + up"
docker compose down >>"$OUT/logs/compose.log" 2>&1
BUILD=(--build); [[ ${E2E_NO_BUILD:-0} == 1 ]] && BUILD=()
docker compose up -d ${BUILD[@]+"${BUILD[@]}"} >>"$OUT/logs/compose.log" 2>&1 || { log "compose up failed"; tail -20 "$OUT/logs/compose.log"; exit 2; }
for _ in $(seq 1 90); do curl -fsk https://localhost/healthz >/dev/null 2>&1 && break; sleep 3; done
# A flight the test unit left behind (volumes survive `down`) would hide the pilot screen's "Where to?": end it, reload.
docker compose exec -T postgres psql -U aerolink -d aerolink -c "update planned_flights set status='ended' where aircraft_id='dev-test' and status in ('planned','active','off_route')" >>"$OUT/logs/compose.log" 2>&1
docker compose restart backend >>"$OUT/logs/compose.log" 2>&1
for _ in $(seq 1 90); do curl -fsk https://localhost/healthz >/dev/null 2>&1 && break; sleep 3; done
# The ATC dashboard needs an operator login (G2): a throwaway operator, disabled again when the run ends.
E2E_OPS_USER="e2e-ops"; E2E_OPS_PASS="e2e-$(openssl rand -hex 8)"; export E2E_OPS_USER E2E_OPS_PASS
{ docker compose exec -T backend python -m aerolink.service.operators add "$E2E_OPS_USER" --name "E2E run" --password-stdin <<<"$E2E_OPS_PASS" \
  || docker compose exec -T backend python -m aerolink.service.operators passwd "$E2E_OPS_USER" --password-stdin <<<"$E2E_OPS_PASS"; } >>"$OUT/logs/compose.log" 2>&1
docker compose exec -T backend python -m aerolink.service.operators enable "$E2E_OPS_USER" >>"$OUT/logs/compose.log" 2>&1
log "stack healthy; letting the simulator ramp up"; sleep 30

(cd backend && uv run python ../scripts/e2e_check.py --out "$OUT" pre) 2>&1 | tee "$OUT/logs/pre.log"
# The API checks above plan flights for the test unit (e.g. item 15's revision-after-restart flight). The browser tests
# start from the pilot's idle screen, so end those flights again and reload the backend's in-memory flight table.
docker compose exec -T postgres psql -U aerolink -d aerolink -c "update planned_flights set status='ended' where aircraft_id='dev-test' and status in ('planned','active','off_route')" >>"$OUT/logs/compose.log" 2>&1
docker compose restart backend >>"$OUT/logs/compose.log" 2>&1
for _ in $(seq 1 90); do curl -fsk https://localhost/healthz >/dev/null 2>&1 && break; sleep 3; done
log "browser tests"
(cd web && npx playwright test -c tests/e2e/playwright.config.ts) 2>&1 | tee "$OUT/logs/playwright.log"
(cd backend && uv run python ../scripts/e2e_check.py --out "$OUT" post) 2>&1 | tee "$OUT/logs/post.log"
if [[ ${E2E_SUITE:-0} == 1 ]]; then (cd backend && uv run python ../scripts/e2e_check.py --out "$OUT" suite) 2>&1 | tee "$OUT/logs/suite.log"; fi
(cd backend && uv run python ../scripts/e2e_check.py --out "$OUT" report); rc=$?
log "results: $OUT/RESULTS.md (exit $rc)"
exit $rc
