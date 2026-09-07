"""
M3AG-S11 — SMTP/DNS verification of harvested e-mails (agri tree)
=================================================================
Thin wrapper over m2_s11_verify: same core (m1_s9g classify/probe, the
20%-dead sanity gate, KNOWN_BLOCKERS), only the checkpoint directory and
the list of files carrying e-mails differ. Output verdicts are French:
valide / invalide / risque / non verifie. `invalide` never ships (m3ag_s9).

Residential-IP wall (Orange/SFR/Outlook/Yahoo refuse probes) applies, so
most consumer mailboxes come back `non verifie` — that is our limit, not
a fact about the address.

Since 2026-09-07 the operators' own Agence Bio addresses are verified too
(operateurs_<dept>.csv, comma-delimited). Until then they shipped on the
strength of being owner-declared; the téléopératrice file needs the proven
bounces out, and m3ag_s9 already withholds any `invalide`.

Usage:
    python scripts/m3ag_s11_verify.py
    python scripts/m3ag_s11_verify.py --limit 30
"""

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(PROJECT_ROOT))
import m2_s11_verify as core                       # noqa: E402
from m3ag_lib import CHECK_DIR                     # noqa: E402

SOURCES = (("site_contacts.csv", "email"), ("matched_63.csv", "email"),
           ("matched_03.csv", "email"), ("search_hits.csv", "emails"),
           ("baf_listings.csv", "email"), ("osm_listings.csv", "email"),
           ("places_listings.csv", "email"))


def collect_emails() -> dict:
    out: dict = {}
    for fname, col in SOURCES:
        p = CHECK_DIR / fname
        if not p.exists():
            continue
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh, delimiter=";"):
                for e in (r.get(col) or "").split("|"):
                    e = e.strip().lower()
                    if "@" in e:
                        out.setdefault(e.rpartition("@")[2], set()).add(e)
    # operators' own Agence Bio declared addresses — comma-delimited, unlike
    # the harvest checkpoints above
    for p in sorted(CHECK_DIR.glob("operateurs_*.csv")):
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                e = (r.get("email") or "").strip().lower()
                if "@" in e:
                    out.setdefault(e.rpartition("@")[2], set()).add(e)
    return out


if __name__ == "__main__":
    core.CHECK_DIR = CHECK_DIR
    core.OUT_PATH = CHECK_DIR / "verified_emails.csv"
    core.collect_emails = collect_emails
    core.main()
