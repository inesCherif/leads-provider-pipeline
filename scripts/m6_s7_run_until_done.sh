#!/bin/sh
# Self-healing wrapper for the M6 Pages Jaunes harvest (copy of
# m3ag_s7_run_until_done.sh): relaunches after transient stops, gives up only
# when two consecutive runs make NO progress (real wall — a human is needed,
# or the harvest is complete). Chrome must be up with --remote-debugging-port=9222.
#   sh scripts/m6_s7_run_until_done.sh 03
#   sh scripts/m6_s7_run_until_done.sh 63
DEPT="${1:-03}"
WHAT="${2:-eleveurs,agriculteurs,elevages}"
DONE="exports/eleveurs/checkpoints/pj_done.txt"
# a detached sh (Start-Process) may not carry the user's PATH
PY="$(command -v python 2>/dev/null || echo /c/Users/USER/AppData/Local/Programs/Python/Python311/python.exe)"
LOG="exports/eleveurs/checkpoints/pj_run_${DEPT}.log"
noprog=0
for i in $(seq 1 15); do
  before=$(wc -l < "$DONE" 2>/dev/null || echo 0)
  PYTHONIOENCODING=utf-8 "$PY" scripts/m6_s7_pagesjaunes.py --attach --departement "$DEPT" --what "$WHAT" 2>&1 \
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
