#!/bin/sh
# producteur.direct for the whole country, detached.
#
# NOT a new source — Sam asked us to concentrate on the existing ones, and
# this is one we already use; it had simply only ever been fetched for 03 and
# 63. Its sitemaps already list every fiche in France (17,355 in pd_index.csv,
# harvested 2026-09-11), so this only opens the fiches themselves.
#
# ~17,150 fiches left at LISTING_DELAY 0.8s = about 4 h. It resumes from
# pd_listings_done.txt, so a crash or a reboot costs at most one fiche, and
# it hits a different host from the registry, so it can run beside the France
# run without slowing it.
export PATH="/c/Program Files/Git/usr/bin:/c/Program Files/Git/bin:${PYTHON_DIR:+$PYTHON_DIR:}$PATH"
cd "$(dirname "$0")/.." || exit 1
PY="$(command -v python 2>/dev/null || command -v python3 2>/dev/null || echo python)"
LOG="exports/france_agriculture/pd_harvest.log"
mkdir -p exports/france_agriculture

echo "PD HARVEST started $(date)" >> "$LOG"
# Up to 20 relaunches: the site occasionally drops a connection, and each
# restart resumes from the done-file rather than re-reading 17k fiches.
i=1
while [ "$i" -le 20 ]; do
    before=$(wc -l < exports/producteurs/checkpoints/pd_listings_done.txt 2>/dev/null || echo 0)
    "$PY" scripts/m7_s5b_producteurdirect.py --all-depts >> "$LOG" 2>&1
    after=$(wc -l < exports/producteurs/checkpoints/pd_listings_done.txt 2>/dev/null || echo 0)
    echo "run $i: done-file $before -> $after ($(date))" >> "$LOG"
    [ "$after" -le "$before" ] && break      # no progress: finished, or blocked
    i=$((i + 1))
done
echo "PD HARVEST finished $(date), done-file $(wc -l < exports/producteurs/checkpoints/pd_listings_done.txt)" >> "$LOG"
