#!/bin/sh
# PACA lever A1 — crawl every website we ALREADY hold, per département:
# producteurs matched + Sans SIRET (m7_s4 --unmatched is a superset of the
# matched run), then éleveurs (m6_s4). No key, no browser, no DB; resumes from
# crawl_done.txt (<dept>:<row_id>:<domain>), so re-launching is the resume.
#
# Workers take DISJOINT département lists (they append to the same
# site_verdicts.csv / site_contacts.csv, one open-append-close per row):
#   sh scripts/paca_crawl.sh w1 13
#   sh scripts/paca_crawl.sh w2 84 04
#   sh scripts/paca_crawl.sh w3 83 06 05
# Detached (the Claude tool kills a background job after 10 min):
#   Start-Process "C:\Program Files\Git\usr\bin\sh.exe" -ArgumentList 'scripts/paca_crawl.sh w1 13' `
#     -WorkingDirectory <repo> -WindowStyle Hidden
# Pilot 2026-09-20, dept 84: 20 targets -> 9 valide -> 7 operators gained an e-mail.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:${PYTHON_DIR:+$PYTHON_DIR:}$PATH"
export PYTHONUNBUFFERED=1
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || echo python)"
W="$1"; shift
LOG="exports/france_agriculture/logs/paca_crawl_$W.log"
mkdir -p "$(dirname "$LOG")"
{
  echo "PACA crawl $W started $(date) — départements: $*"
  for D in "$@"; do
    echo "=== [$D] producteurs (matched + Sans SIRET) $(date)"
    "$PY" scripts/m7_s4_crawl.py --departement "$D" --unmatched
    echo "=== [$D] éleveurs $(date)"
    "$PY" scripts/m6_s4_crawl.py --departement "$D"
  done
  echo "PACA crawl $W finished $(date)"
} >> "$LOG" 2>&1
