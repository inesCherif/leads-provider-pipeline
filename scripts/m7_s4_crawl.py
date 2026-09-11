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

`--unmatched` (Ines 2026-09-11: "crawl all websites we have"): the
websites of the UNMATCHED listings (`unmatched_<dept>.csv`, the Sans SIRET
tab) are crawled too. Each listing becomes a pseudo-operator
(row_id `U<source>:<listing_id>`, name = the listing's name, gérant = its
contact person, CP / ville from the fiche, no SIRET) so the validator
tests ownership exactly as for a registry row — by name tokens, postcode
and the phone the listing already declares. m7_s9 reads the verdicts and
contacts for those ids into the Sans SIRET sheet.

Output : checkpoints/site_verdicts.csv, checkpoints/site_contacts.csv
Done   : checkpoints/crawl_done.txt (<dept>:<row_id>:<domain>)

Usage:
    python scripts/m7_s4_crawl.py --departement 63 --pilot 10
    python scripts/m7_s4_crawl.py --departement 63
    python scripts/m7_s4_crawl.py --departement 63 --unmatched
    python scripts/m7_s4_crawl.py --departement 03 --redo-mort
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import m3ag_s4_crawl as core                                   # noqa: E402
from m7_lib import (CHECK_DIR, load_operators, load_matches,  # noqa: E402
                    is_aggregator, name_tokens, read_csv)

UNMATCHED = "--unmatched" in sys.argv
if UNMATCHED:
    sys.argv.remove("--unmatched")


def pseudo_id(u: dict) -> str:
    return f"U{u['source']}:{u['listing_id']}"


def load_operators_plus(dept: str) -> list[dict]:
    ops = load_operators(dept)
    if not UNMATCHED:
        return ops
    n = 0
    for u in read_csv(CHECK_DIR / f"unmatched_{dept}.csv"):
        if not (u.get("website") or "").strip():
            continue
        op = {"raisonSociale": u["name"], "siret": "", "gerant": u.get("alt_name", ""),
              "telephone": u.get("phone", ""), "telephoneCommerciale": u.get("mobile", ""),
              "codeNAF": "", "siteWebs": "", "categories": u.get("categorie", ""),
              "productions": u.get("productions", ""), "adresse": u.get("address", ""),
              "codePostal": u.get("postcode", ""), "ville": u.get("city", ""),
              "lat": "", "lon": "", "email": u.get("email", ""), "numeroBio": "", "activites": "",
              "flag_hors_agri": "0", "flag_hors_dept": "0", "siren": "", "denomination_legale": "",
              "enseigne": "", "sous_segment": ""}
        op["_id"] = pseudo_id(u)
        op["_has_phone"] = bool(op["telephone"])
        op["_has_email"] = bool(op["email"])
        op["_has_site"] = False
        op["_tokens"] = name_tokens(op["raisonSociale"], op["gerant"])
        ops.append(op)
        n += 1
    core.log.info(f"[{dept}] --unmatched: {n} Sans SIRET listings with a website added as pseudo-operators")
    return ops


def load_matches_plus(dept: str) -> dict:
    m = load_matches(dept)
    if not UNMATCHED:
        return m
    for u in read_csv(CHECK_DIR / f"unmatched_{dept}.csv"):
        if (u.get("website") or "").strip():
            m.setdefault(pseudo_id(u), []).append({"website": u["website"], "source": u["source"],
                                                   "phone": u.get("phone", ""), "mobile": u.get("mobile", ""),
                                                   "email": u.get("email", "")})
    return m


core.CHECK_DIR = CHECK_DIR
core.VERDICTS_PATH = CHECK_DIR / "site_verdicts.csv"
core.CONTACTS_PATH = CHECK_DIR / "site_contacts.csv"
core.DONE_PATH = CHECK_DIR / "crawl_done.txt"
core.load_operators = load_operators_plus
core.load_matches = load_matches_plus
core.is_aggregator = is_aggregator
core.name_tokens = name_tokens
core.log = logging.getLogger("m7_s4")

if __name__ == "__main__":
    core.main()
