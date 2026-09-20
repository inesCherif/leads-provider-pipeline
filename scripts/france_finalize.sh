#!/bin/sh
# Final pass, to run once the three workers, the Agence Bio pass and the
# producteur.direct harvest have all finished.
#
# Why it is not optional: the two national sources land in ONE shared file
# each (agencebio_listings.csv, pd_listings.csv), written while the workers
# were already building départements. Every département built before its
# source landed currently ships WITHOUT it — Agence Bio is rank 1 for both
# phone and e-mail, so that is most of the contact data. This pass re-matches
# and re-exports every département against the complete files.
#
# It touches no network: match -> export -> gate, then the recap.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:${PYTHON_DIR:+$PYTHON_DIR:}$PATH"
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || echo python)"
LOG="exports/france_agriculture/finalize.log"

echo "FINALIZE started $(date)" >> "$LOG"

# The shared Agence Bio adapter, written once now that nothing else reads it.
"$PY" scripts/m6_s20_agencebio.py --departements all >> "$LOG" 2>&1

# Every département, rebuilt against the complete harvests. --redo because
# they are already marked ok.
#
# 03 and 63 are deliberately NOT rebuilt (no --include-done): Sam already
# holds their V2, and a fresh v1 would be a DIFFERENT file under a different
# name for the same département — exactly the confusion we want to avoid.
# They are copied into the delivery tree as they are, by france_copy_v2.py.
"$PY" scripts/france_run.py --all --rebuild-only --redo >> "$LOG" 2>&1
"$PY" scripts/france_copy_v2.py >> "$LOG" 2>&1

"$PY" scripts/france_run.py --recap >> "$LOG" 2>&1
echo "FINALIZE finished $(date)" >> "$LOG"
