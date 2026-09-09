"""
M6-S7 — Pages Jaunes harvest for éleveurs (wrap of m3ag_s7)
===========================================================
`m3ag_s7_pagesjaunes.py` carries every rule Pages Jaunes taught this repo
(attach to the human's Chrome over CDP, navigate commune pages by URL,
result cards outrank block markers, flush per page, slug-scoped done keys).
Nothing in it is bio-specific except its paths and its default slug list —
this wrapper overrides exactly those and calls its `main()`.

Slugs: `agriculteurs` and `elevages` were harvested for dept 63 by M3AG
(1,796 listings, 100 % with a phone — PJ lists EVERY farmer, not only the
organic ones, which is why they are M6 witnesses). `eleveurs` is a
candidate slug: run it with `--pilot 3` first, the cards-vs-404 verdict
decides, never intuition.

Targets come from `operateurs_<dept>.csv` (m6_s2, the registry population,
so every commune holding an éleveur), busiest commune first.

Done keys are SEEDED from the M3AG file on the first run
(`exports/agriculteurs/checkpoints/pj_done.txt`, keys `<dept>:<slug>:<commune>|pN`)
so the 373 dept-63 communes already walked for `agriculteurs` / `elevages`
are not fetched again; their listings are read in place by m6_s8.

Outputs land in exports/eleveurs/checkpoints/ (pj_listings.csv, pj_done.txt,
pj_seen_cards.txt, pj_html/).

Usage (Chrome started with --remote-debugging-port=9222, pagesjaunes.fr open):
    python scripts/m6_s7_pagesjaunes.py --attach --departement 03 --what eleveurs --pilot 3
    python scripts/m6_s7_pagesjaunes.py --attach --departement 03
    python scripts/m6_s7_pagesjaunes.py --attach --departement 63
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import m3ag_s7_pagesjaunes as core          # noqa: E402
from m6_lib import CHECK_DIR, INHERITED_DIR  # noqa: E402

core.CHECK_DIR = CHECK_DIR
core.OUT_PATH = CHECK_DIR / "pj_listings.csv"
core.DONE_PATH = CHECK_DIR / "pj_done.txt"
core.HTML_DIR = CHECK_DIR / "pj_html"
core.SEEN_CARDS_PATH = CHECK_DIR / "pj_seen_cards.txt"
core.DEFAULT_WHATS = "agriculteurs,elevages"
core.log = logging.getLogger("m6_s7")


def seed_done() -> None:
    """First run only: inherit M3AG's done keys so dept 63's agri slugs are not refetched."""
    if core.DONE_PATH.exists():
        return
    src = INHERITED_DIR / "pj_done.txt"
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    if src.exists():
        keys = [k for k in src.read_text(encoding="utf-8").split() if k]
        core.DONE_PATH.write_text("\n".join(keys) + "\n", encoding="utf-8")
        print(f"pj_done.txt seeded with {len(keys)} keys from {src}")
    src_seen = INHERITED_DIR / "pj_seen_cards.txt"
    if src_seen.exists() and not core.SEEN_CARDS_PATH.exists():
        core.SEEN_CARDS_PATH.write_text(src_seen.read_text(encoding="utf-8"), encoding="utf-8")


if __name__ == "__main__":
    seed_done()
    core.main()
