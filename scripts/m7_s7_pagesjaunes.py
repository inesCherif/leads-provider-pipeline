"""
M7-S7 — Pages Jaunes harvest for the PRODUCTEURS trades (wrap of m3ag_s7)
=========================================================================
`m3ag_s7_pagesjaunes.py` carries every rule Pages Jaunes taught this repo
(attach to the human's Chrome over CDP, navigate by URL, result cards
outrank block markers, flush per page, slug-scoped done keys). This wrapper
overrides its paths and its slug list, and runs it in the NEW `--dept-level`
mode: one DÉPARTEMENT listing per slug (`/annuaire/departement/<dept>/<slug>`,
≤ 20 pages of 20 cards) instead of one page per commune — 1 URL instead of
373 for every slug. A slug whose p1 advertises more than 400 résultats is
reported at the end: only those need the commune-by-commune run
(`--no-dept-level`), everything else is complete after one pass.

Only 7 slugs were ever harvested in this repo (agriculteurs, eleveurs,
elevages, maraichers, producteurs-de-fruits-et-legumes, apiculteurs,
fromagers — M7 V2, 2026-09-11). The trades of the M7
sub-segments were never opened; PJ prints a phone on 100 % of its cards.
No alcohol / pork slug is in the list (the principle); every listing is
still re-tested by `m7_lib.listing_excluded` in m7_s8.

Card ids already harvested in the M3AG and M6 trees are SEEDED as seen, so
a card known from `agriculteurs` is not re-revealed (the reveal clicks were
the cost of the earlier runs); its row is read from the other tree by m7_s8.

Outputs: exports/producteurs/checkpoints/ (pj_listings.csv, pj_done.txt,
pj_seen_cards.txt, pj_html/). Done keys `<dept>:<slug>:departement|pN`.

Usage (Chrome started with --remote-debugging-port=9222, pagesjaunes.fr open):
    python scripts/m7_s7_pagesjaunes.py --attach --departement 63 --what travaux-agricoles --dump-html
    python scripts/m7_s7_pagesjaunes.py --attach --departement 63
    python scripts/m7_s7_pagesjaunes.py --attach --departement 03
    python scripts/m7_s7_pagesjaunes.py --attach --departement 63 --what pepinieristes --no-dept-level   # commune fallback
"""

import csv
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import m3ag_s7_pagesjaunes as core                                   # noqa: E402
from m7_lib import CHECK_DIR, INHERITED_AGRI, INHERITED_ELEVEURS     # noqa: E402

# Trades of the M7 sub-segments, never run before. A slug that does not exist
# answers 404 once and is recorded, never retried (the m2-s25 lesson: the
# cards-vs-404 verdict decides, not intuition). No vins / cidre / brasserie /
# charcuterie slug — the principle.
#
# Measured on dept 63 (2026-09-11 18:30): a slug PJ does not know is NOT a 404 —
# PJ runs it as a free-text search and returns whatever carries the word
# (`moulins` → nurses at "Les Moulins", the paper-mill museum, gîtes, 666
# advertised; `huilerie` / `miel` → épiceries fines). Those three are out;
# m7_s8 also keeps only agricultural PJ categories (PJ_CATEGORY_OK_RE).
M7_WHATS = ("travaux-agricoles,exploitation-agricole,produits-fermiers-vente-directe,"
            "pepinieristes,horticulteurs,laiteries,fromageries,fabrication-de-fromages,"
            "cooperatives-laitieres,apiculture,industrie-agroalimentaire,minoteries,"
            "pisciculture,producteur-de-fruits,maraicher-bio,apiculteurs-bio,"
            "plantes-aromatiques,"
            # the four M3AG slugs were run for dept 63 only; dept 03 never got them
            "maraichers,producteurs-de-fruits-et-legumes,apiculteurs,fromagers")

core.CHECK_DIR = CHECK_DIR
core.OUT_PATH = CHECK_DIR / "pj_listings.csv"
core.DONE_PATH = CHECK_DIR / "pj_done.txt"
core.HTML_DIR = CHECK_DIR / "pj_html"
core.SEEN_CARDS_PATH = CHECK_DIR / "pj_seen_cards.txt"
core.DEFAULT_WHATS = M7_WHATS
core.log = logging.getLogger("m7_s7")

_load_seen_ids = core.load_seen_ids


def load_seen_ids_plus() -> None:
    """M7's own file, then the M3AG and M6 harvests: a known card is never re-revealed."""
    _load_seen_ids()
    n0, c0 = len(core.SEEN_IDS), len(core.SEEN_CARDS)
    for d in (INHERITED_AGRI, INHERITED_ELEVEURS):
        p = d / "pj_listings.csv"
        if p.exists():
            with p.open(encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh, delimiter=";"):
                    if r.get("listing_id"):
                        core.SEEN_IDS.add(r["listing_id"])
        # cards INSPECTED by the earlier runs (written or not): a page made only of
        # them skips the reveal clicks (~15 s a page, measured 2026-09-11 17:58)
        p = d / "pj_seen_cards.txt"
        if p.exists():
            core.SEEN_CARDS.update(p.read_text(encoding="utf-8").split())
    core.log.info(f"seen listing ids: {n0} own + {len(core.SEEN_IDS) - n0} inherited (M3AG + M6); "
                  f"inspected cards: {c0} own + {len(core.SEEN_CARDS) - c0} inherited")


core.load_seen_ids = load_seen_ids_plus

if __name__ == "__main__":
    # dept-level is the default here; `--no-dept-level` restores the commune loop
    if "--no-dept-level" in sys.argv:
        sys.argv.remove("--no-dept-level")
    elif "--dept-level" not in sys.argv:
        sys.argv.append("--dept-level")
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    core.main()
