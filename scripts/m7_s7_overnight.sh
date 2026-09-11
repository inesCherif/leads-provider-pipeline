#!/bin/sh
# Pages Jaunes harvest for the M7 producteurs trades, both départements, meant
# to be started DETACHED from the Claude tool (its background jobs die after 10 minutes):
#   powershell: Start-Process "C:\Program Files\Git\usr\bin\sh.exe" -ArgumentList 'scripts/m7_s7_overnight.sh' -WorkingDirectory <repo> -WindowStyle Hidden
# A detached sh has no PATH: Git tools and Python are added explicitly.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:/c/Users/USER/AppData/Local/Programs/Python/Python311:$PATH"
cd "$(dirname "$0")/.." || exit 1
sh scripts/m7_s7_run_until_done.sh 63
sh scripts/m7_s7_run_until_done.sh 03
echo "OVERNIGHT: both départements finished $(date)" | tee -a exports/producteurs/checkpoints/pj_run_63.log
