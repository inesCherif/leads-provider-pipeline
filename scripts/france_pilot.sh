#!/bin/sh
# Pilot before the France run: a 63 neighbour (15 Cantal), the biggest cereal
# cell measured (32 Gers, 01.11Z = 8,816 legal units) and Corsica (2B), which
# is the case the in_dept() fix exists for.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:/c/Users/USER/AppData/Local/Programs/Python/Python311:$PATH"
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || echo /c/Users/USER/AppData/Local/Programs/Python/Python311/python.exe)"
mkdir -p exports/france_agriculture
echo "PILOT started $(date)"
"$PY" scripts/france_run.py --departements 15,32,2B --skip-supabase
echo "PILOT finished $(date)"
