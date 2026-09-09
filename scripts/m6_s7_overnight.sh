#!/bin/sh
# Overnight Pages Jaunes harvest for the éleveurs sector, meant to be started
# DETACHED from the Claude tool (its background jobs die after 10 minutes):
#   powershell: Start-Process "C:\Program Files\Git\usr\bin\sh.exe" -ArgumentList 'scripts/m6_s7_overnight.sh' -WorkingDirectory <repo> -WindowStyle Hidden
# A detached sh has no PATH: Git tools and Python are added explicitly.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:/c/Users/USER/AppData/Local/Programs/Python/Python311:$PATH"
cd "$(dirname "$0")/.." || exit 1
sh scripts/m6_s7_run_until_done.sh 03 eleveurs,agriculteurs,elevages
sh scripts/m6_s7_run_until_done.sh 63 eleveurs,agriculteurs,elevages
echo "OVERNIGHT: both départements finished $(date)"
