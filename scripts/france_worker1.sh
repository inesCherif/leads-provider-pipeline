#!/bin/sh
# France worker 1 — Occitanie, Bourgogne-Franche-Comté, PACA, Bretagne, 2A.
# Whole régions per worker, so a région finishes together and can be sent.
# The three workers hold DISJOINT département lists: they share the checkpoint
# directories, and two processes pulling the same dept would fight over one JSONL.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:/c/Users/USER/AppData/Local/Programs/Python/Python311:$PATH"
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || echo /c/Users/USER/AppData/Local/Programs/Python/Python311/python.exe)"
D="09,11,12,30,31,34,46,48,65,66,81,82,21,25,39,58,70,71,89,90,04,05,06,13,83,84,22,29,35,56,2A"
echo "WORKER1 started $(date)"
"$PY" scripts/france_run.py --departements "$D" --worker w1 --skip-global
echo "WORKER1 finished $(date)"
