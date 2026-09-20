"""
M6-S4 — Crawl the éleveurs' candidate websites for e-mails and phones (wrap of m3ag_s4)
======================================================================================
Thin wrapper over `m3ag_s4_crawl` (the m6_s7_pagesjaunes pattern). Same
crawler and per-(operator, domain) validation; `score_fn` stays
`agri_score` (the validator only needs "is this a farm's own page").
Targets = websites on `matched_<dept>.csv` (PJ, OSM, BAF, Agence Bio,
provider, DB claims, and since 2026-09-11 the M7 directories) plus
`search_hits.csv` (m6_s3). The V3 lever (docs/RUNBOOK.md, M6).

Usage:
    python scripts/m6_s4_crawl.py --departement 63 --pilot 10
    python scripts/m6_s4_crawl.py --departement 63
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import m3ag_s4_crawl as core                                   # noqa: E402
from m6_lib import (CHECK_DIR, load_operators, load_matches,  # noqa: E402
                    is_aggregator, name_tokens)

core.CHECK_DIR = CHECK_DIR
core.VERDICTS_PATH = CHECK_DIR / "site_verdicts.csv"
core.CONTACTS_PATH = CHECK_DIR / "site_contacts.csv"
core.DONE_PATH = CHECK_DIR / "crawl_done.txt"
core.load_operators = load_operators
core.load_matches = load_matches
core.is_aggregator = is_aggregator
core.name_tokens = name_tokens
core.log = logging.getLogger("m6_s4")

if __name__ == "__main__":
    core.main()
