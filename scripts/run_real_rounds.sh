#!/usr/bin/env bash
# Real-model runs behind RESULTS.md: one campaign run + N improvement rounds (ollama by default).
# Usage: scripts/run_real_rounds.sh [rounds=3] [limit=40] [trials=2]
set -euo pipefail
# macOS: keep the machine awake for the whole run (an overnight run lost ~6.5 h to sleep).
# Lid-closed on battery still sleeps — keep it plugged in.
if command -v caffeinate >/dev/null 2>&1 && [ -z "${PP_CAFFEINATED:-}" ]; then
  export PP_CAFFEINATED=1
  exec caffeinate -i -s "$0" "$@"
fi
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src" OTEL_ENABLED="${OTEL_ENABLED:-true}" LLM_PROVIDER="${LLM_PROVIDER:-ollama}"
ROUNDS="${1:-3}"; LIMIT="${2:-40}"; TRIALS="${3:-2}"
mkdir -p evals/runs/logs
echo "[$(date -u +%FT%TZ)] campaign run (provider=$LLM_PROVIDER)"
uv run prospectpilot demo --inline --reenrich 2>&1 | tee evals/runs/logs/campaign-$LLM_PROVIDER-before.log
for i in $(seq 1 "$ROUNDS"); do
  echo "[$(date -u +%FT%TZ)] improvement round $i/$ROUNDS (limit=$LIMIT trials=$TRIALS)"
  uv run prospectpilot improve --limit "$LIMIT" --trials "$TRIALS" 2>&1 | tee "evals/runs/logs/improve-$LLM_PROVIDER-$i.log"
done
echo "[$(date -u +%FT%TZ)] campaign run with the ACTIVE prompt after the rounds"
uv run prospectpilot demo --inline --reenrich 2>&1 | tee evals/runs/logs/campaign-$LLM_PROVIDER-after.log
echo "[$(date -u +%FT%TZ)] done"
