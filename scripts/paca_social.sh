#!/bin/sh
# PACA lever A2 — contact e-mails off the farms' PUBLIC Facebook pages
# (m7_s18_social: its OWN empty headless browser, never Ines's Chrome; no login).
# The pool is rebuilt at each run from what the crawl (site_contacts.csv) and the
# search (search_hits.csv) found, so RE-RUN it after every crawl pass: done
# businesses are skipped (social_done.txt).
# Pilot 2026-09-20: 20 pages -> 16 e-mails, 14 phones (80 %), 0 walled; pool 161.
#   Start-Process "C:\Program Files\Git\usr\bin\sh.exe" -ArgumentList 'scripts/paca_social.sh' `
#     -WorkingDirectory <repo> -WindowStyle Hidden
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:${PYTHON_DIR:+$PYTHON_DIR:}$PATH"
export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || echo python)"
SCOPE="${1:-Provence-Alpes-Cote-d-Azur}"
LOG="exports/france_agriculture/logs/paca_social.log"
mkdir -p "$(dirname "$LOG")"
{
  echo "PACA social started $(date) — $SCOPE"
  "$PY" scripts/m7_s18_social.py --departements "$SCOPE"
  echo "PACA social finished $(date)"
} >> "$LOG" 2>&1
