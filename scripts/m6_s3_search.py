"""
M6-S3 — Search sweep per unreachable éleveur (wrap of m3ag_s3)
==============================================================
Thin wrapper over `m3ag_s3_search` pointed at the éleveurs tree. Same
gates (geo per result, name token, snippet phones = corroboration only).
Quota shared account-wide (`search_quota.json`): ddgs 300/day, Tavily
1,000/month. The daily-drip V3 lever listed in docs/m6_progress.md.

Usage:
    python scripts/m6_s3_search.py --departement 63 --backend ddgs --pilot 10
    python scripts/m6_s3_search.py --departement 63 --backend ddgs
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import m3ag_s3_search as core                                  # noqa: E402
from m6_lib import (CHECK_DIR, load_operators, load_matches,  # noqa: E402
                    is_aggregator, name_tokens)

core.CHECK_DIR = CHECK_DIR
core.OUT_PATH = CHECK_DIR / "search_hits.csv"
core.DONE_PATH = CHECK_DIR / "search_done.txt"
core.load_operators = load_operators
core.load_matches = load_matches
core.is_aggregator = is_aggregator
core.name_tokens = name_tokens
core.log = logging.getLogger("m6_s3")

if __name__ == "__main__":
    core.main()
