#!/bin/sh
# PACA Track B — Pages Jaunes, département level, through Ines's Chrome (CDP :9222).
# PHONES, not e-mails (measured: phone on ~100 % of cards, website 1 in 1,013,
# e-mail never). Per département: the 21 M7 trade slugs, then the three éleveurs
# slugs — m6_s8_match reads M7's pj_listings.csv, so one harvest serves both.
# ONE harvester on :9222 at a time. Resume = relaunch (pj_done.txt).
#
# Before: close Chrome, start it with
#   --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\pj_cdp_profile"
# open pagesjaunes.fr, solve the challenge if one shows. Then, detached:
#   Start-Process "C:\Program Files\Git\usr\bin\sh.exe" -ArgumentList 'scripts/paca_pj.sh' `
#     -WorkingDirectory <repo> -WindowStyle Hidden
# Logs: exports/producteurs/checkpoints/pj_run_<dept>.log
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:${PYTHON_DIR:+$PYTHON_DIR:}$PATH"
cd "$(dirname "$0")/.." || exit 1
DEPTS="${*:-13 84 83 04 05 06}"
for D in $DEPTS; do
  sh scripts/m7_s7_run_until_done.sh "$D"
  sh scripts/m7_s7_run_until_done.sh "$D" eleveurs,agriculteurs,elevages
done
echo "PACA PJ: finished $(date) — $DEPTS" | tee -a exports/producteurs/checkpoints/pj_run_paca.log
