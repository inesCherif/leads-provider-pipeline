#!/bin/sh
# Catch-up rebuild for the départements built BEFORE the national Agence Bio
# listings were published at 11:48 (27,105 listings, against ~2,000 when the
# file still only held 03/63). Those départements shipped without rank-1
# phone and e-mail data: measured on Aveyron, phones 11 -> 267 and e-mails
# 144 -> 406 once it landed.
#
# No network: match -> export -> gate. Runs beside the workers; the
# départements listed here are all finished, so nothing is contended.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:/c/Users/USER/AppData/Local/Programs/Python/Python311:$PATH"
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || echo /c/Users/USER/AppData/Local/Programs/Python/Python311/python.exe)"
LOG="exports/france_agriculture/catchup.log"

echo "CATCHUP started $(date)" >> "$LOG"
"$PY" scripts/france_run.py --rebuild-only --redo --worker catchup \
    --departements "08,09,10,11,15,16,17,19,23,2B,30,32,51,52,54,55,57" >> "$LOG" 2>&1
echo "CATCHUP finished $(date)" >> "$LOG"
