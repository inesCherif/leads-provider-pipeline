"""
M7-S4 — Crawl the producteurs' candidate websites for e-mails and phones (wrap of m3ag_s4)
=========================================================================================
Thin wrapper over `m3ag_s4_crawl` (the m6_s7_pagesjaunes pattern: override
the module globals, call `core.main()`). Same crawler, same per-(operator,
domain) validation (`m2lib_validate.classify`, `score_fn=agri_score`),
same guards (3+ claims = réseau without a fetch; abort when >50 % of the
run fails at the network level; 12+ addresses = store list).

Targets = websites carried by the matched listings (`matched_<dept>.csv`:
acheteralasource, producteur.direct, fermes-locales, jours-de-marché, PJ,
OSM, Agence Bio…) plus `search_hits.csv` (m7_s3). This is Sam's "compléter
en scrapant les sites (email ou url page de contact)".

Output : checkpoints/site_verdicts.csv, checkpoints/site_contacts.csv
Done   : checkpoints/crawl_done.txt (<dept>:<row_id>:<domain>)

Usage:
    python scripts/m7_s4_crawl.py --departement 63 --pilot 10
    python scripts/m7_s4_crawl.py --departement 63
    python scripts/m7_s4_crawl.py --departement 03 --redo-mort
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import m3ag_s4_crawl as core                                   # noqa: E402
from m7_lib import (CHECK_DIR, load_operators, load_matches,  # noqa: E402
                    is_aggregator, name_tokens)

core.CHECK_DIR = CHECK_DIR
core.VERDICTS_PATH = CHECK_DIR / "site_verdicts.csv"
core.CONTACTS_PATH = CHECK_DIR / "site_contacts.csv"
core.DONE_PATH = CHECK_DIR / "crawl_done.txt"
core.load_operators = load_operators
core.load_matches = load_matches
core.is_aggregator = is_aggregator
core.name_tokens = name_tokens
core.log = logging.getLogger("m7_s4")

if __name__ == "__main__":
    core.main()
