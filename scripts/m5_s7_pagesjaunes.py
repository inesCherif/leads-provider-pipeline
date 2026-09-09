"""
M5-S7 — Pages Jaunes harvest for gîtes and campings (wrap of m3ag_s7)
=====================================================================
`m3ag_s7_pagesjaunes.py` carries every rule Pages Jaunes taught this repo
(attach to the human's Chrome over CDP, navigate commune pages by URL,
result cards outrank block markers, flush per page, slug-scoped done keys).
Nothing in it is agricultural except its paths and its default slug list —
this wrapper overrides exactly those and calls its `main()`.

Slugs (seen live on pagesjaunes.fr 2026-09-09; the pilot's cards-vs-404
verdict prunes the list — never intuition):
    campings, gites, chambres-d-hotes, gite-detape, villages-et-clubs-de-vacances

Targets come from `operateurs_<dept>.csv` (m5_s2), busiest commune first.
Outputs land in exports/hebergement/checkpoints/ (pj_listings.csv,
pj_done.txt, pj_seen_cards.txt, pj_html/).

Usage (Chrome started with --remote-debugging-port=9222, pagesjaunes.fr open):
    python scripts/m5_s7_pagesjaunes.py --attach --departement 63 --pilot 3 --dump-html
    python scripts/m5_s7_pagesjaunes.py --attach --departement 63
    python scripts/m5_s7_pagesjaunes.py --attach --departement 03
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import m3ag_s7_pagesjaunes as core   # noqa: E402
from m5_lib import CHECK_DIR         # noqa: E402

core.CHECK_DIR = CHECK_DIR
core.OUT_PATH = CHECK_DIR / "pj_listings.csv"
core.DONE_PATH = CHECK_DIR / "pj_done.txt"
core.HTML_DIR = CHECK_DIR / "pj_html"
core.SEEN_CARDS_PATH = CHECK_DIR / "pj_seen_cards.txt"
core.DEFAULT_WHATS = "campings,gites,chambres-d-hotes,gite-detape,villages-et-clubs-de-vacances"
core.log = logging.getLogger("m5_s7")

if __name__ == "__main__":
    core.main()
