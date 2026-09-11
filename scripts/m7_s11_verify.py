"""
M7-S11 — SMTP/DNS verification of the producteurs' e-mails (wrap of m2_s11)
===========================================================================
Copy of `m6_s11_verify.py`: same core (m1_s9g classify/probe, the 20%-dead
sanity gate, KNOWN_BLOCKERS), only the checkpoint directory and the files
carrying e-mails differ. Verdicts are French: valide / invalide / risque /
non verifie. `invalide` never ships (m7_s9).

Addresses already carrying a verdict in the M3AG or M6 trees are NOT
probed again. Files probed: matched_<dept>.csv (the directory e-mails),
unmatched_<dept>.csv (the Sans SIRET tab), site_contacts.csv (m7_s4),
search_hits.csv (m7_s3).

Residential-IP wall (Orange/SFR/Outlook/Yahoo refuse probes): many
consumer mailboxes come back `non verifie` — our limit, not the address's.

Usage:
    python scripts/m7_s11_verify.py
    python scripts/m7_s11_verify.py --limit 30
"""

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))
import m2_s11_verify as core                                        # noqa: E402
from m7_lib import CHECK_DIR, INHERITED_AGRI, INHERITED_ELEVEURS, DEPARTEMENTS   # noqa: E402

SOURCES = [("site_contacts.csv", "email"), ("search_hits.csv", "emails"),
           ("provider_agri.csv", "email"), ("db_claims.csv", "email")]       # m7_s19 (V2)
SOURCES += [(f"matched_{d}.csv", "email") for d in DEPARTEMENTS]
SOURCES += [(f"unmatched_{d}.csv", "email") for d in DEPARTEMENTS]


def already_verified() -> set:
    done = set()
    for p in (INHERITED_AGRI / "verified_emails.csv", INHERITED_ELEVEURS / "verified_emails.csv",
              CHECK_DIR / "verified_emails.csv"):
        if p.exists():
            with p.open(encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh, delimiter=";"):
                    if r.get("verdict") in ("valide", "invalide", "risque"):
                        done.add((r.get("email") or "").strip().lower())
    p = CHECK_DIR / "db_claims.csv"          # S9-G verdicts already in the DB (m7_s19)
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
                for e in (r.get(col) or "").split("|"):
                    e = e.strip().lower()
                    if "@" not in e:
                        continue
                    if e in done:
                        n_skipped += 1
                        continue
                    out.setdefault(e.rpartition("@")[2], set()).add(e)
    print(f"collect_emails: {sum(len(v) for v in out.values())} new address(es) over {len(out)} domain(s); "
          f"{n_skipped} already carry a verdict (M3AG / M6 / earlier M7 run), not re-probed")
    return out


def merge_previous(out_path: Path, previous: list[dict]) -> None:
    """m2_s11 OVERWRITES its output; keep earlier verdicts (a re-run only probes new addresses)."""
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
