#!/bin/sh
# Generic detached runner for a per-département step, one département after the
# other (for steps that share ONE output file and must not run in parallel):
#   sh scripts/paca_run.sh <logname> <script.py> <dept> [<dept>...]
#   sh scripts/paca_run.sh aas scripts/m7_s5a_acheteralasource.py 84 13 83 04 05 06
# Detached (the Claude tool kills a background job after 10 min):
#   Start-Process "C:\Program Files\Git\usr\bin\sh.exe" `
#     -ArgumentList 'scripts/paca_run.sh aas scripts/m7_s5a_acheteralasource.py 84 13 83 04 05 06' `
#     -WorkingDirectory <repo> -WindowStyle Hidden
# Resume = relaunch: every step it runs keeps its own done-file.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:${PYTHON_DIR:+$PYTHON_DIR:}$PATH"
export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || echo python)"
NAME="$1"; SCRIPT="$2"; shift 2
LOG="exports/france_agriculture/logs/paca_$NAME.log"
mkdir -p "$(dirname "$LOG")"
{
  echo "PACA $NAME ($SCRIPT) started $(date) — départements: $*"
  for D in "$@"; do
    echo "=== [$D] $(date)"
    "$PY" "$SCRIPT" --departement "$D" $EXTRA      # EXTRA="--refresh" etc., from the environment
  done
  echo "PACA $NAME finished $(date)"
} >> "$LOG" 2>&1
