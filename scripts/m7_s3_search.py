"""
M7-S3 — Search sweep per unreachable producteur (wrap of m3ag_s3)
=================================================================
Thin wrapper over `m3ag_s3_search`: one query per operator with no phone
and no e-mail ("<raisonSociale> [gérant] <ville> <CP>"), per-RESULT geo
gate + name-token gate, snippet phones as corroboration only, e-mails only
when they carry the farm's name. Quota file shared account-wide
(`search_quota.json`): ddgs 300/day, Tavily 1,000/month (resets Oct 1).

`--priority` (2026-09-20, the PACA e-mail push): search ONLY the operators most
likely to own a website — a société (anything but an entrepreneur individuel),
a trade name (`enseigne`), or a shop-type sous-segment. Measured on PACA:
15,637 producteurs without an e-mail, 4,697 of them sociétés, for ~3,800
searches in hand — the quota goes where a site can exist. The core sorts its
own to-do list, so the wrapper narrows the population instead of re-ordering
it; a later run WITHOUT the flag covers the rest (done-keys skip the searched).

Output : checkpoints/search_hits.csv   Done: checkpoints/search_done.txt

Usage:
    python scripts/m7_s3_search.py --departement 63 --backend ddgs --pilot 10
    python scripts/m7_s3_search.py --departement 63 --backend ddgs
    python scripts/m7_s3_search.py --departement 84 --backend ddgs --targets emailless --priority --pilot 20
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import m3ag_s3_search as core                                  # noqa: E402
from m7_lib import (CHECK_DIR, load_operators, load_matches,  # noqa: E402
                    is_aggregator, name_tokens)

PRIORITY = "--priority" in sys.argv
if PRIORITY:
    sys.argv.remove("--priority")
SHOP_SEGMENTS = ("pépiniériste", "plantes aromatiques", "transformation", "huilerie", "fromager",
                 "laiterie", "meunerie", "aquaculture")


def likely_has_site(op: dict) -> bool:
    forme = (op.get("forme_juridique") or "").strip().lower()
    return (forme not in ("", "entrepreneur individuel")
            or bool((op.get("enseigne") or "").strip())
            or (op.get("sous_segment") or "").startswith(SHOP_SEGMENTS))


def load_operators_priority(dept: str) -> list[dict]:
    ops = load_operators(dept)
    if not PRIORITY:
        return ops
    kept = [op for op in ops if likely_has_site(op)]
    core.log.info(f"[{dept}] --priority: {len(kept)} of {len(ops)} operators kept "
                  f"(société, enseigne or shop-type sous-segment)")
    return kept


core.CHECK_DIR = CHECK_DIR
core.OUT_PATH = CHECK_DIR / "search_hits.csv"
core.DONE_PATH = CHECK_DIR / "search_done.txt"
core.load_operators = load_operators_priority
core.load_matches = load_matches
core.is_aggregator = is_aggregator
core.name_tokens = name_tokens
core.log = logging.getLogger("m7_s3")

if __name__ == "__main__":
    core.main()
