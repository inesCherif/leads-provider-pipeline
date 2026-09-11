#!/bin/sh
# Self-healing wrapper for the M7 Pages Jaunes harvest (copy of
# m6_s7_run_until_done.sh): relaunches after transient stops, gives up only
# when two consecutive runs make NO progress (real wall — a human is needed,
# or the harvest is complete). Chrome must be up with --remote-debugging-port=9222.
#   sh scripts/m7_s7_run_until_done.sh 63
#   sh scripts/m7_s7_run_until_done.sh 03
#   sh scripts/m7_s7_run_until_done.sh 63 pepinieristes --no-dept-level   # commune fallback for one slug
DEPT="${1:-63}"
WHAT="${2:-}"
MODE="${3:-}"
DONE="exports/producteurs/checkpoints/pj_done.txt"
# a detached sh (Start-Process) may not carry the user's PATH
PY="$(command -v python 2>/dev/null || echo /c/Users/USER/AppData/Local/Programs/Python/Python311/python.exe)"
LOG="exports/producteurs/checkpoints/pj_run_${DEPT}.log"
WHATARG=""
[ -n "$WHAT" ] && WHATARG="--what $WHAT"
noprog=0
for i in $(seq 1 15); do
  before=$(wc -l < "$DONE" 2>/dev/null || echo 0)
  PYTHONIOENCODING=utf-8 "$PY" scripts/m7_s7_pagesjaunes.py --attach --departement "$DEPT" $WHATARG $MODE 2>&1 \
    | tee -a "$LOG"
  after=$(wc -l < "$DONE" 2>/dev/null || echo 0)
  echo "WRAPPER: run $i finished, done-keys $before -> $after" | tee -a "$LOG"
  if [ "$after" -le "$before" ]; then
    noprog=$((noprog+1))
    [ "$noprog" -ge 2 ] && { echo "WRAPPER: no progress twice - stopping (complete, or a human is needed)" | tee -a "$LOG"; break; }
  else
    noprog=0
  fi
  sleep 90
done
