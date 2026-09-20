#!/bin/sh
# Rebuild some départements as a NEW version after an enrichment pass — no
# network, no DB. Per département: éleveurs match -> export -> gate, then
# producteurs match -> export (with the éleveurs of the same version) -> gate.
# Both gates run --strict against the previous version (H10: no phone / e-mail /
# site may be lost). A failed gate STOPS that département (the next one still
# runs) and is listed at the end; nothing is published here —
# `france_publish.py` does that, and only for what you saw pass.
#
#   sh scripts/paca_rebuild.sh v2 v1 04 05 06 13 83 84
#   sh scripts/paca_rebuild.sh v3 v2 84
# Log: exports/france_agriculture/logs/paca_rebuild_<version>.log
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:${PYTHON_DIR:+$PYTHON_DIR:}$PATH"
export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || echo python)"
V="$1"; BASE="$2"; shift 2
LOG="exports/france_agriculture/logs/paca_rebuild_$V.log"
mkdir -p "$(dirname "$LOG")"
FAILED=""
step() {   # step <dept> <label> <command...>
  D="$1"; L="$2"; shift 2
  echo "--- [$D] $L" >> "$LOG"
  if ! "$@" >> "$LOG" 2>&1; then
    echo "[$D] FAILED at: $L" | tee -a "$LOG"
    return 1
  fi
}
echo "REBUILD $V (baseline $BASE) started $(date) — $*" | tee -a "$LOG"
for D in "$@"; do
  step "$D" "m6_s8 match"  "$PY" scripts/m6_s8_match.py  --departement "$D" &&
  step "$D" "m6_s9 export" "$PY" scripts/m6_s9_export.py --departement "$D" --version "$V" &&
  step "$D" "m6_s12 gate"  "$PY" scripts/m6_s12_check.py --departement "$D" --version "$V" --baseline "$BASE" --strict &&
  step "$D" "m7_s8 match"  "$PY" scripts/m7_s8_match.py  --departement "$D" &&
  step "$D" "m7_s9 export" "$PY" scripts/m7_s9_export.py --departement "$D" --version "$V" --with-eleveurs --eleveurs-version "$V" &&
  step "$D" "m7_s12 gate"  "$PY" scripts/m7_s12_check.py --departement "$D" --version "$V" --baseline "$BASE" --strict &&
  echo "[$D] $V built and gated" | tee -a "$LOG" || FAILED="$FAILED $D"
done
echo "REBUILD $V finished $(date) — failed:${FAILED:- none}" | tee -a "$LOG"
