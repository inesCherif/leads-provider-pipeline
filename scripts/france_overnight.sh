#!/bin/sh
# France agriculture run, meant to be started DETACHED from the Claude tool
# (its background jobs die after 10 minutes):
#
#   powershell: Start-Process "C:\Program Files\Git\usr\bin\sh.exe" \
#       -ArgumentList 'scripts/france_overnight.sh' \
#       -WorkingDirectory "<repo>" -WindowStyle Hidden
#
# A detached sh has no PATH: Git tools and Python are added explicitly
# (the lesson from m6_s7_overnight.sh).
#
# Everything here is resumable: re-running this script after a crash, a
# reboot or a CTRL-C picks up where it stopped. Départements already marked
# ok in france_status.csv are skipped unless --redo is passed.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:${PYTHON_DIR:+$PYTHON_DIR:}$PATH"
cd "$(dirname "$0")/.." || exit 1

PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || echo python)"
LOG="exports/france_agriculture/france_run.log"
mkdir -p exports/france_agriculture

echo "FRANCE RUN started $(date)" | tee -a "$LOG"

# 1. The whole country. --skip-supabase: the two SELECTs were already run for
#    all 96 départements, and they are the only steps that touch the DB.
"$PY" scripts/france_run.py --all --skip-supabase 2>&1 | tee -a "$LOG"

# 2. Second pass, no network: any harvest that finished while the run was
#    going (producteur.direct) reaches the départements built before it.
"$PY" scripts/france_run.py --all --rebuild-only --redo 2>&1 | tee -a "$LOG"

# 3. The recap, counted by reading every xlsx back.
"$PY" scripts/france_run.py --recap 2>&1 | tee -a "$LOG"

echo "FRANCE RUN finished $(date)" | tee -a "$LOG"
