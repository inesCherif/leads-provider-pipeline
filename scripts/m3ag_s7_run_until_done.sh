#!/bin/sh
# Self-healing wrapper for the PJ harvest: relaunches after transient stops,
# gives up only when two consecutive runs make NO progress (real wall — a
# human is needed, or the harvest is complete).
DEPT="${1:-63}"
DONE="exports/agriculteurs/checkpoints/pj_done.txt"
noprog=0
for i in $(seq 1 15); do
  before=$(wc -l < "$DONE" 2>/dev/null || echo 0)
  python scripts/m3ag_s7_pagesjaunes.py --attach --departement "$DEPT" 2>&1 \
    | tee -a "exports/agriculteurs/checkpoints/pj_run_${DEPT}.log"
  after=$(wc -l < "$DONE" 2>/dev/null || echo 0)
  echo "WRAPPER: run $i finished, done-keys $before -> $after"
  if [ "$after" -le "$before" ]; then
    noprog=$((noprog+1))
    [ "$noprog" -ge 2 ] && { echo "WRAPPER: no progress twice - stopping (complete, or a human is needed)"; break; }
  else
    noprog=0
  fi
  sleep 90
done
