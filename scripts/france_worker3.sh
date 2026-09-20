#!/bin/sh
# France worker 3 — Grand Est, Auvergne-Rhône-Alpes (minus 03/63/15), Centre-Val
# de Loire, Hauts-de-France.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:${PYTHON_DIR:+$PYTHON_DIR:}$PATH"
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || echo python)"
D="08,10,51,52,54,55,57,67,68,88,01,07,26,38,42,43,69,73,74,18,28,36,37,41,45,02,59,60,62,80"
echo "WORKER3 started $(date)"
"$PY" scripts/france_run.py --departements "$D" --worker w3 --skip-global
echo "WORKER3 finished $(date)"
