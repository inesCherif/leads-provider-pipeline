#!/bin/sh
# Repair dept 56 (Morbihan) after workers 1 and 4 both pulled it.
#
# What happened: worker 4 was given Bretagne from the far end of worker 1's
# list on the assumption w1 would not reach it before w4 finished. w4 spent
# longer than expected on Normandie first, so both arrived on 56 at 14:08 and
# both appended to the same api_raw_56.jsonl. Two processes appending is not
# atomic: 2 of 9,339 lines came out torn and m6_s2_transform died on
# json.loads — the honest failure, and the reason the driver records a
# département and carries on instead of stopping France.
#
# The fix is a clean single-process re-pull, not a patch of the file: a torn
# line means the interleaving happened, and we cannot know which other lines
# were written half-and-half without re-reading them all anyway.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:/c/Users/USER/AppData/Local/Programs/Python/Python311:$PATH"
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || echo /c/Users/USER/AppData/Local/Programs/Python/Python311/python.exe)"
LOG="exports/france_agriculture/repair56.log"

echo "REPAIR 56 started $(date)" >> "$LOG"
rm -f exports/eleveurs/checkpoints/api_raw_56.jsonl exports/producteurs/checkpoints/api_raw_56.jsonl
rm -f exports/eleveurs/checkpoints/api_raw_56.jsonl.gz exports/producteurs/checkpoints/api_raw_56.jsonl.gz
rm -f exports/eleveurs/checkpoints/api_raw_2A.jsonl        # stray empty file, same collision

"$PY" scripts/m6_s1_acquire.py --departements 56 --active-only --skip-naf 01.46Z --fresh >> "$LOG" 2>&1
"$PY" scripts/m7_s1_acquire.py --departements 56 --active-only --fresh >> "$LOG" 2>&1
"$PY" scripts/france_run.py --departements 56,2A --worker repair --redo --skip-global >> "$LOG" 2>&1
echo "REPAIR 56 finished $(date)" >> "$LOG"
