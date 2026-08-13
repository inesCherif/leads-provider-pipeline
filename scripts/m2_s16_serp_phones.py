"""
M2-S16 — Mine phone numbers out of search-result SNIPPETS (free, keyless)
=========================================================================
Pages Jaunes, 118000 and hoodspot all publish the phone number of nearly
every French shop — and all of them block a residential IP at the edge
(MEASURED 2026-08-13: PJ is behind **Cloudflare**, not DataDome as the V2
notes said; a human-solved `cf_clearance` cookie does NOT survive automated
navigation, so the site itself is closed to us for free).

But a search engine has already visited those pages, and it prints the phone
number **in the result snippet**. Reading the snippet needs no access to the
blocked site at all. That is what this script does, through `ddgs` — keyless,
free, no account.

WHAT THIS SCRIPT DOES NOT DO: decide. It writes candidate (siret, phone,
source) rows to `serp_snippets.csv` and stops. A snippet phone was measured at
only **76% agreement** with Google Maps, so it is never a dialable number on
its own — `m2_s14_export_v3.py` ships it in the `Telephone piste` column, and
promotes it only when a second INDEPENDENT source states the same number.
This file is one of those independent sources.

Why per-engine provenance is recorded: two snippets from the same engine for
the same query are not independent evidence, they are the same page read
twice. The corroborator needs to know which directory each number came from.

Quota: `m2lib_search` hard-stops ddgs at 300/day (our own politeness cap, not
theirs). ~1,114 phoneless businesses therefore take ~4 mornings. The done-file
makes that a one-command daily habit:

    python scripts/m2_s16_serp_phones.py            # run again each day
    python scripts/m2_s16_serp_phones.py --pilot 20 # measure first
"""

import argparse
import csv
import logging
import re
import sys
import unicodedata
import urllib.parse
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from m2lib_search import search, quota_state, QuotaExceeded, SearchAuthError  # noqa: E402
from m2lib_contact import extract_phones, normalize_fr_phone, is_surtaxe      # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OURS_PATH = CHECK_DIR / "etablissements.csv"
MATCHED_PATH = CHECK_DIR / "matched.csv"
CONTACTS_PATH = CHECK_DIR / "site_contacts.csv"
OUT_PATH = CHECK_DIR / "serp_snippets.csv"
DONE_PATH = CHECK_DIR / "serp_done.txt"

# Directories that actually print a phone in their snippet. A hit on one of
# these is worth more than a hit on a random blog, and the domain is recorded
# so the corroborator can tell two directories apart from one directory twice.
PHONE_DIRECTORIES = (
    "pagesjaunes", "118000", "118712", "hoodspot", "justacote", "cylex",
    "infobel", "societe.com", "pappers", "annuaire-entreprises", "yelp",
    "tripadvisor", "petitfute", "mappy", "kompass", "manageo", "verif.com",
    "b-reputation", "nomao", "topboulangerie", "boulangeriespatisseries",
    "restaurantguru", "resto.fr", "lannuaire", "telephone-annuaire",
)

FIELDNAMES = ["siret", "siren", "raison_sociale", "commune", "code_postal",
              "phone", "surtaxe", "engine", "snippet_domain", "geo_marker",
              "query"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s16")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9 ]+", " ", s).upper()


def load_done() -> set:
    return set(DONE_PATH.read_text(encoding="utf-8").split()) if DONE_PATH.exists() else set()


def flush(rows: list) -> None:
    if not rows:
        return
    new = not OUT_PATH.exists()
    with OUT_PATH.open("a", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, delimiter=";",
                           quoting=csv.QUOTE_MINIMAL)
        if new:
            w.writeheader()
        w.writerows(rows)
        fh.flush()


def read(path: Path) -> list:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Mine phones from search snippets")
    ap.add_argument("--pilot", type=int, default=0, help="only N businesses")
    ap.add_argument("--backend", default="ddgs",
                    choices=["ddgs", "tavily", "serper_web"])
    args = ap.parse_args()

    ours = read(OURS_PATH)
    if not ours:
        sys.exit(f"{OURS_PATH} not found — run scripts/m2_s2_transform.py first.")

    # Target the businesses that still have NO trustworthy phone. A row that
    # already ships a corroborated number needs nothing from a snippet.
    have_phone = {r["siret"] for r in read(MATCHED_PATH) if r.get("phone")}
    have_phone |= {r["siret"] for r in read(CONTACTS_PATH)
                   if r.get("phone") and r.get("confiance") == "confirme"}
    done = load_done()
    todo = [r for r in ours if r["siret"] not in have_phone and r["siret"] not in done]

    # Biggest first, same rule as m2_s8: an interrupted run should have spent
    # its queries on the most valuable rows.
    EFF = {"": 0, "0 salarie": 1, "1 a 2 salaries": 2, "3 a 5 salaries": 3,
           "6 a 9 salaries": 4, "10 a 19 salaries": 5, "20 a 49 salaries": 6,
           "50 a 99 salaries": 7, "100 a 199 salaries": 8}
    todo.sort(key=lambda r: (-EFF.get(r["tranche_effectif"], 9),
                             0 if r["enseigne"] else 1))
    if args.pilot:
        todo = todo[:args.pilot]

    log.info(f"{len(ours)} businesses | {len(have_phone)} already have a trusted "
             f"phone | {len(done)} already searched -> {len(todo)} to do")
    log.info(f"quota before: {quota_state()}")

    written = hits = 0
    stats = Counter()
    try:
        for i, r in enumerate(todo, 1):
            label = r["enseigne"] or r["raison_sociale"]
            q = f'{label} {r["commune"]} boulangerie telephone'
            try:
                results = search(q, args.backend, max_results=8)
            except (QuotaExceeded, SearchAuthError):
                raise
            except Exception as exc:
                log.warning(f"[{i}/{len(todo)}] {label[:26]}: {type(exc).__name__}")
                with DONE_PATH.open("a", encoding="utf-8") as f:
                    f.write(r["siret"] + "\n")
                continue

            rows = []
            seen_here = set()
            cp = r["code_postal"]
            commune_up = norm(r["commune"])
            for res in results:
                # GEO GATE, per result — not across the whole page. A snippet
                # for a same-named bakery in another town is exactly the error
                # this catches, and testing the merged blob would not catch it.
                blob = f"{res['title']} {res['snippet']}"
                geo = ""
                if cp and cp in blob:
                    geo = "cp"
                elif commune_up and commune_up in norm(blob):
                    geo = "commune"
                if not geo:
                    stats["result rejected: no geo marker"] += 1
                    continue
                dom = urllib.parse.urlparse(res["url"]).netloc.lower().replace("www.", "")
                for p in extract_phones(blob):
                    if p in seen_here:
                        continue
                    seen_here.add(p)
                    rows.append({
                        "siret": r["siret"], "siren": r["siren"],
                        "raison_sociale": r["raison_sociale"],
                        "commune": r["commune"], "code_postal": cp,
                        "phone": p, "surtaxe": "oui" if is_surtaxe(p) else "",
                        "engine": args.backend, "snippet_domain": dom,
                        "geo_marker": geo, "query": q,
                    })
                    stats["directory hit" if any(d in dom for d in PHONE_DIRECTORIES)
                          else "other-site hit"] += 1
            if rows:
                flush(rows)                    # on disk before the next query
                written += len(rows)
                hits += 1
            with DONE_PATH.open("a", encoding="utf-8") as f:
                f.write(r["siret"] + "\n")
            if rows or i % 25 == 0:
                log.info(f"[{i}/{len(todo)}] {label[:24]:24.24} -> "
                         f"{len(rows)} phone(s) | businesses hit {hits} | rows {written}")
    except QuotaExceeded as exc:
        log.warning(f"STOP (daily cap): {exc}")
        log.warning("Re-run this same command tomorrow — it resumes from serp_done.txt.")
    except SearchAuthError as exc:
        sys.exit(f"auth: {exc}")

    log.info("─" * 62)
    log.info(f"businesses yielding a phone: {hits}/{len(todo)} | rows written: {written}")
    for k, v in stats.most_common():
        log.info(f"  {k:<34} {v}")
    log.info(f"quota after: {quota_state()}")
    log.info("These are LEADS, not numbers. m2_s14 ships them in 'Telephone "
             "piste' and only promotes one corroborated by a second source.")


if __name__ == "__main__":
    main()
