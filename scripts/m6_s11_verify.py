"""
M6-S11 — SMTP/DNS verification of the éleveurs e-mails (wrap of m2_s11)
=======================================================================
Thin wrapper over m2_s11_verify: same core (m1_s9g classify/probe, the
20%-dead sanity gate, KNOWN_BLOCKERS), only the checkpoint directory and
the list of files carrying e-mails differ. Output verdicts are French:
valide / invalide / risque / non verifie. `invalide` never ships (m6_s9).

Addresses already carrying a verdict in the inherited M3AG file
(exports/agriculteurs/checkpoints/verified_emails.csv) or in a DB claim
(db_claims.csv, verdict valid/invalid/risky) are NOT probed again — m6_s9
reads both trees and the DB verdicts. Only the new ones go to the wire:
provider e-mails without an S9-G verdict, Agence Bio declarations never
verified, matched-listing e-mails, and M6's own crawl/search finds.

Residential-IP wall (Orange/SFR/Outlook/Yahoo refuse probes) applies, so
many consumer mailboxes come back `non verifie` — that is our limit, not a
fact about the address.

`--departements` (a list, a région name, or `all` — france_lib) scopes the run:
matched_ files of THOSE départements, and the shared files (agencebio_listings,
provider_agri, db_claims, site_contacts, search_hits) filtered to them — they
have been NATIONAL since 2026-09-19 (27k Agence Bio rows), so an unscoped run
probes the whole country. Without the flag nothing changes.

Usage:
    python scripts/m6_s11_verify.py
    python scripts/m6_s11_verify.py --limit 30
    python scripts/m6_s11_verify.py --departements Provence-Alpes-Cote-d-Azur
"""

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))
import m2_s11_verify as core                       # noqa: E402
from m6_lib import CHECK_DIR, INHERITED_DIR, DEPARTEMENTS   # noqa: E402
from m7_lib import dept_of_cp                      # noqa: E402  (single source of truth cp -> dept)
from france_lib import parse_departements          # noqa: E402


def pop_departements() -> list[str]:
    """--departements is ours, not the core's: strip it before m2_s11 parses argv."""
    if "--departements" not in sys.argv:
        return []
    i = sys.argv.index("--departements")
    if i + 1 >= len(sys.argv):
        sys.exit("--departements needs a value (a list, a région name, or all)")
    spec = sys.argv[i + 1]
    del sys.argv[i:i + 2]
    return parse_departements(spec)


SCOPE = pop_departements()                 # [] = the historical behaviour, untouched
DEPTS = SCOPE or list(DEPARTEMENTS)

SOURCES = [("site_contacts.csv", "email"), ("search_hits.csv", "emails"),
           ("agencebio_listings.csv", "email"), ("provider_agri.csv", "email"),
           ("db_claims.csv", "email")]
SOURCES += [(f"matched_{d}.csv", "email") for d in DEPTS]


def out_of_scope(r: dict) -> bool:
    """A row of a shared (national) file that belongs to another département."""
    if not SCOPE:
        return False
    d = (r.get("dept") or "").strip() or dept_of_cp(r.get("codePostal") or r.get("postcode") or "")
    return bool(d) and d not in SCOPE


def already_verified() -> set:
    done = set()
    for p in (INHERITED_DIR / "verified_emails.csv", CHECK_DIR / "verified_emails.csv"):
        if p.exists():
            with p.open(encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh, delimiter=";"):
                    if r.get("verdict") in ("valide", "invalide", "risque"):
                        done.add((r.get("email") or "").strip().lower())
    p = CHECK_DIR / "db_claims.csv"
    if p.exists():
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                if r.get("kind") == "email" and r.get("verdict") in ("valid", "invalid", "risky"):
                    done.add((r.get("email") or "").strip().lower())
    return done


def collect_emails() -> dict:
    done = already_verified()
    out: dict = {}
    n_skipped = 0
    for fname, col in SOURCES:
        p = CHECK_DIR / fname
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                if fname == "provider_agri.csv" and r.get("email_verified") == "1":
                    continue                      # S9-G already said valid
                if out_of_scope(r):
                    continue
                for e in (r.get(col) or "").split("|"):
                    e = e.strip().lower()
                    if "@" not in e:
                        continue
                    if e in done:
                        n_skipped += 1
                        continue
                    out.setdefault(e.rpartition("@")[2], set()).add(e)
    print(f"collect_emails: {sum(len(v) for v in out.values())} new address(es) over {len(out)} domain(s); "
          f"{n_skipped} already carry a verdict (inherited / DB), not re-probed")
    return out


def merge_previous(out_path: Path, previous: list[dict]) -> None:
    """m2_s11 OVERWRITES its output; keep earlier M6 verdicts (a re-run only probes new addresses)."""
    if not previous:
        return
    new = {}
    if out_path.exists():
        with out_path.open(encoding="utf-8-sig", newline="") as fh:
            new = {r["email"]: r for r in csv.DictReader(fh, delimiter=";")}
    merged = {r["email"]: r for r in previous}
    merged.update(new)
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["email", "domain", "verdict", "detail"], delimiter=";",
                           quoting=csv.QUOTE_MINIMAL)
        w.writeheader()
        w.writerows(sorted(merged.values(), key=lambda r: r["email"]))
    print(f"verified_emails.csv: {len(previous)} earlier verdict(s) kept + {len(new)} from this run = {len(merged)}")


if __name__ == "__main__":
    core.CHECK_DIR = CHECK_DIR
    core.OUT_PATH = CHECK_DIR / "verified_emails.csv"
    core.collect_emails = collect_emails
    previous = []
    if core.OUT_PATH.exists():
        with core.OUT_PATH.open(encoding="utf-8-sig", newline="") as fh:
            previous = list(csv.DictReader(fh, delimiter=";"))
    try:
        core.main()
    finally:
        merge_previous(core.OUT_PATH, previous)
