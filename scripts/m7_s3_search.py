"""
M7-S3 — Search sweep per unreachable producteur (wrap of m3ag_s3)
=================================================================
Thin wrapper over `m3ag_s3_search`: one query per operator with no phone
and no e-mail ("<raisonSociale> [gérant] <ville> <CP>"), per-RESULT geo
gate + name-token gate, snippet phones as corroboration only, e-mails only
when they carry the farm's name. Quota file shared account-wide
(`search_quota.json`): ddgs 300/day, Tavily 1,000/month (resets Oct 1).

Output : checkpoints/search_hits.csv   Done: checkpoints/search_done.txt

Usage:
    python scripts/m7_s3_search.py --departement 63 --backend ddgs --pilot 10
    python scripts/m7_s3_search.py --departement 63 --backend ddgs
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import m3ag_s3_search as core                                  # noqa: E402
from m7_lib import (CHECK_DIR, load_operators, load_matches,  # noqa: E402
                    is_aggregator, name_tokens)

core.CHECK_DIR = CHECK_DIR
core.OUT_PATH = CHECK_DIR / "search_hits.csv"
core.DONE_PATH = CHECK_DIR / "search_done.txt"
core.load_operators = load_operators
core.load_matches = load_matches
core.is_aggregator = is_aggregator
core.name_tokens = name_tokens
core.log = logging.getLogger("m7_s3")

if __name__ == "__main__":
    core.main()
