#!/bin/sh
# PACA lever A3 — bienvenue-a-la-ferme, one département after the other (they
# share baf_listings.csv and baf_listings_done.txt, so NOT in parallel).
# Pilot 2026-09-20, dept 06: 20 fiches -> 20 e-mails, 20 phones, 10 websites.
# Resume = relaunch (baf_listings_done.txt). Detached:
#   Start-Process "C:\Program Files\Git\usr\bin\sh.exe" -ArgumentList 'scripts/paca_baf.sh 84 04 05 83 13' `
#     -WorkingDirectory <repo> -WindowStyle Hidden
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:/c/Users/USER/AppData/Local/Programs/Python/Python311:$PATH"
export PYTHONUNBUFFERED=1
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || echo /c/Users/USER/AppData/Local/Programs/Python/Python311/python.exe)"
LOG="exports/france_agriculture/logs/paca_baf.log"
mkdir -p "$(dirname "$LOG")"
{
  echo "PACA BAF started $(date) — départements: $*"
  for D in "$@"; do
    echo "=== [$D] $(date)"
    "$PY" scripts/m3ag_s10_baf.py --departement "$D"
  done
  echo "PACA BAF finished $(date)"
} >> "$LOG" 2>&1
