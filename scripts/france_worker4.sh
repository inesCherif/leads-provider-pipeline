#!/bin/sh
# France worker 4 — the TAILS of the two slowest workers, so they finish
# together instead of w2 running ~90 min past w3.
#
# Normandie is the far end of worker 2's list and Bretagne + 2A the far end of
# worker 1's, so by the time either of them walks that far these départements
# are already built: their acquires find complete sidecars and return at once,
# costing about a minute of redundant match/export each. That is the reason
# the tails are safe to take and the heads are not.
#
# Four registry pullers at 0.7 s each is ~5.6 req/s, still under the API's
# documented 7 req/s per IP. Do not add a fifth.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:/c/Users/USER/AppData/Local/Programs/Python/Python311:$PATH"
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || echo /c/Users/USER/AppData/Local/Programs/Python/Python311/python.exe)"
D="14,27,50,61,76,22,29,35,56,2A"
echo "WORKER4 started $(date)"
"$PY" scripts/france_run.py --departements "$D" --worker w4 --skip-global
echo "WORKER4 finished $(date)"
