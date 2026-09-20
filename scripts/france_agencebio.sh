#!/bin/sh
# Agence Bio for the whole country, beside the three registry workers.
#
# Split in two on purpose: m3ag_s1/m3ag_s2 write ONE FILE PER DÉPARTEMENT, so
# they are safe to run while the workers are matching. m6_s20_agencebio
# rewrites a single shared agencebio_listings.csv, so it runs ONCE at the end,
# never while a matcher might be reading it. The départements built before it
# lands pick it up in the final --rebuild-only pass, which costs no network.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:${PYTHON_DIR:+$PYTHON_DIR:}$PATH"
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || echo python)"
LOG="exports/france_agriculture/agencebio.log"
mkdir -p exports/france_agriculture

echo "AGENCEBIO started $(date)" >> "$LOG"
for d in $("$PY" -c "import sys; sys.path.insert(0,'scripts'); from france_lib import METRO_DEPARTEMENTS; print(' '.join(METRO_DEPARTEMENTS))"); do
    "$PY" scripts/m3ag_s1_acquire.py --departements "$d" >> "$LOG" 2>&1
    "$PY" scripts/m3ag_s2_transform.py --departements "$d" >> "$LOG" 2>&1
done
echo "AGENCEBIO acquire done $(date)" >> "$LOG"

# The one shared file, written once, when nothing else is reading it.
"$PY" scripts/m6_s20_agencebio.py --departements all >> "$LOG" 2>&1
echo "AGENCEBIO finished $(date)" >> "$LOG"
