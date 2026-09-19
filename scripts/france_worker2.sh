#!/bin/sh
# France worker 2 — Nouvelle-Aquitaine, Île-de-France, Pays de la Loire, Normandie.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:/c/Users/USER/AppData/Local/Programs/Python/Python311:$PATH"
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || echo /c/Users/USER/AppData/Local/Programs/Python/Python311/python.exe)"
D="16,17,19,23,24,33,40,47,64,79,86,87,75,77,78,91,92,93,94,95,44,49,53,72,85,14,27,50,61,76"
echo "WORKER2 started $(date)"
"$PY" scripts/france_run.py --departements "$D" --worker w2 --skip-global
echo "WORKER2 finished $(date)"
