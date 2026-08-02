r"""
M1-S9-H — Repair shifted / polluted address columns
====================================================
Two long-standing open items in CLAUDE.md turn out to be the same defect:

  * "38 rows fail the 5-digit postal_code assertion"  (the last standing gate
    warning)
  * "24 rows have shifted address columns"

All 38 failures are shifted rows: the house number landed in `postal_code` and
the real postcode is sitting inside `city`, glued to the town name.

    postal_code = '3   '   city = '- 59780Baisieux'
    postal_code = '103 '   city = 'Avenue De Flandres - 59650Villeneuve D Ascq'

Measured 2026-08-02: 38 rows with an unusable postal_code, 97 rows with digits
polluting `city`. That is 0.08% of 128,267 sites — cosmetic in aggregate, but
`Ville` is a column the client reads, and a postcode of '3' also costs the row
its department, which is what geographic filtering runs on.

SIX DEFECT CLASSES, all present in the real data and all handled deterministically:

  A shifted      cp='3'      city='- 59780Baisieux'          -> cp=59780, city='Baisieux'
  B phone glued  cp=62960    city='BEAUMETZ LES AIRE 0626930077'  -> strip the phone
                             also '06/82/90/56/83' and 'tel 0663269032'
  C cp prefix    cp=29250    city='29250 SANTEC'             -> strip the leading code
  D cp in parens cp=33520    city='BRUGES (33520)'           -> strip the parens
  E duplicated   cp=02290    city='AMBLENY 02290 AMBLENY'    -> keep the first town
  F junk cp      cp='36'     city='Azay le Ferron'           -> cp=NULL, city kept

WHAT IS DELIBERATELY NOT TOUCHED
    Rows where postal_code is a VALID 5-digit code but `city` carries a
    DIFFERENT one (cp=20054 / city='59330 HAUTMONT'). The city is probably
    right — 59330 really is Hautmont — but "probably" is not a standard for
    overwriting client data, and there are only two of them. They are reported
    and left alone.

    Marketing blurbs pasted into `city` ('ERQUINGHEM LE SEC CENTRE EQUESTRE
    ... stages galops ...') are left too: there is no deterministic boundary
    between the town name and the prose, and guessing one would truncate real
    town names.

A JUNK POSTCODE IS WORSE THAN NO POSTCODE
    '3' is not a postcode, it is a house number that drifted one column left.
    Setting it NULL is a correction, not a loss: `department_code` derivation
    already ignores it (no CASE branch matches a 1-3 digit value), so nothing
    downstream regresses, and the quality gate stops reporting a defect we have
    decided to keep.

IDEMPOTENT
    Every rule is a fixed point — running it twice changes nothing the second
    time, because a repaired row no longer matches the selection.

Usage:
    python scripts/m1_s9h_address_repair.py --self-test   # no DB, no network
    python scripts/m1_s9h_address_repair.py --dry-run     # show before/after
    python scripts/m1_s9h_address_repair.py

Reversal:
    Values are overwritten in place, so there is no automatic undo. The source
    of truth survives in raw.ingest_rows.raw_json — re-derive from there if
    ever needed. Nothing outside staging.sites.postal_code / city is touched.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

try:
    import psycopg2
    import psycopg2.extras
    from dotenv import load_dotenv
except ImportError as e:
    sys.exit(f"Missing dependency: {e}\nRun: pip install -r requirements.txt")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m1_s9h")

SCRIPT_NAME = "m1_s9h_address_repair.py"

CP5 = re.compile(r"(?<!\d)(\d{5})(?!\d)")
# A French mobile/landline as it appears pasted into these cells: 10 digits,
# optionally separated by / . - or spaces. Anchored to the END so a house
# number inside the town name is never eaten.
PHONE_TAIL = re.compile(
    r"(?:\btel\b|\btél\b|\bt\b)?\s*0[\d\s./-]{8,}\d\s*$", re.IGNORECASE)
# '59650Villeneuve D Ascq' — postcode welded to the town with no space.
GLUED = re.compile(r"(\d{5})\s*([A-Za-zÀ-ÿ].*)$")
# The unambiguous shift signature: a street name, a dash, then a postcode WELDED
# to the town — 'Rue De Lille - 59560Comines'. This is trusted even when
# postal_code already looks valid, because of a nasty interaction: the earlier
# 4-digit zero-pad repair turned house number '1027' into '01027', which is a
# perfectly well-formed Ain postcode and therefore invisible to every shape
# check. Only the glued town cell reveals that the real address is 59560
# Comines, in Nord. A well-formed value is not a correct one.
SHIFT_SIGNATURE = re.compile(r"\S\s+[-–]\s*(\d{5})([A-Za-zÀ-ÿ].*)$")
PARENS_CP = re.compile(r"\s*\((\d{5})\)\s*$")
LEAD_JUNK = re.compile(r"^[\s\-–,;.]+")


def _tidy(town: str) -> str:
    town = LEAD_JUNK.sub("", town)
    return re.sub(r"\s{2,}", " ", town).strip(" -,;.")


def repair(cp: str | None, city: str | None) -> tuple[str | None, str | None] | None:
    """Return (new_cp, new_city) when something improves, else None.

    Pure function: no DB, no network. Every branch below corresponds to a
    defect class observed in the real data — see the module docstring.
    """
    cp_in = (cp or "").strip()
    city_in = (city or "").strip()
    if not city_in and not cp_in:
        return None

    cp_ok = bool(re.fullmatch(r"\d{5}", cp_in))

    # A well-formed postcode is still wrong if the town cell carries the shift
    # signature. Checked BEFORE cp_ok is trusted — see SHIFT_SIGNATURE.
    sig = SHIFT_SIGNATURE.search(city_in)
    if sig:
        return sig.group(1), _tidy(sig.group(2))

    new_cp, new_city = (cp_in if cp_ok else None), city_in

    # --- D: trailing '(59260)' -------------------------------------------------
    m = PARENS_CP.search(new_city)
    if m:
        if not new_cp:
            new_cp = m.group(1)
        new_city = PARENS_CP.sub("", new_city)

    # --- A: postcode hidden in the town cell ----------------------------------
    if not cp_ok:
        m = GLUED.search(new_city)
        if m:
            new_cp, new_city = m.group(1), m.group(2)
        else:
            found = CP5.search(new_city)
            if found:
                new_cp = found.group(1)
                new_city = CP5.sub(" ", new_city, count=1)
            # else: F — nothing recoverable, new_cp stays None (junk dropped)

    # --- C: the town cell repeats its own postcode ----------------------------
    if new_cp and new_city.lstrip().startswith(new_cp):
        new_city = new_city.lstrip()[len(new_cp):]

    # --- B: a phone number pasted after the town ------------------------------
    stripped = PHONE_TAIL.sub("", new_city)
    if stripped.strip():           # never let the phone rule empty the cell
        new_city = stripped

    new_city = _tidy(new_city)

    # --- E: 'AMBLENY 02290 AMBLENY' -> 'AMBLENY' ------------------------------
    dup = re.match(r"^(.+?)\s+\d{5}\s+(.+)$", new_city)
    if dup and dup.group(1).casefold() == dup.group(2).casefold():
        new_city = dup.group(1)

    new_city = _tidy(new_city)
    if not new_city:
        new_city = city_in          # refuse to blank a cell we cannot improve

    changed = (new_cp or "") != cp_in or new_city != city_in
    return (new_cp, new_city) if changed else None


SELECT_SQL = r"""
SELECT id, postal_code, city
FROM staging.sites
WHERE (postal_code IS NOT NULL AND btrim(postal_code::text) !~ '^[0-9]{5}$')
   OR city ~ '[0-9]'
ORDER BY id
"""

UPDATE_SQL = """
UPDATE staging.sites AS s
SET postal_code = v.cp,
    city        = v.city,
    updated_at  = NOW()
FROM (VALUES %s) AS v(id, cp, city)
WHERE s.id = v.id::uuid
"""

AUDIT_SQL = """
INSERT INTO audit.audit_log
    (table_name, record_id, field_changed, old_value, new_value, changed_by, reason)
VALUES ('staging.sites', NULL, 'postal_code,city', NULL, %s, %s, %s)
"""


def run(args) -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("SUPABASE_DB_URL not set in .env")
    conn = psycopg2.connect(url, keepalives=1, keepalives_idle=30,
                            keepalives_interval=10, keepalives_count=5)
    conn.autocommit = False
    cur = conn.cursor()

    cur.execute(SELECT_SQL)
    rows = cur.fetchall()
    log.info("Rows with a suspect postal_code or a digit in city: %d", len(rows))

    writes, skipped, mismatch = [], 0, []
    for sid, cp, city in rows:
        cp_in = (cp or "").strip()
        out = repair(cp, city)
        # Reported, never rewritten: a valid postcode contradicted by another
        # one inside the town cell, WHERE NO RULE FIRED. Rows the shift
        # signature already resolves are repaired, not reported — listing them
        # here as well made the output read as if they had been left broken.
        if out is None and re.fullmatch(r"\d{5}", cp_in):
            other = CP5.search(city or "")
            if other and other.group(1) != cp_in:
                mismatch.append((cp_in, city))
        if out is None:
            skipped += 1
            continue
        writes.append((str(sid), out[0], out[1]))

    log.info("  repairable   %5d", len(writes))
    log.info("  unchanged    %5d", skipped)
    log.info("  cp dropped as junk %d",
             sum(1 for _, c, _ in writes if c is None))
    log.info("")
    for _, c, t in writes[:25]:
        log.info("    -> cp=%-6s city=%s", c or "NULL", t)
    if mismatch:
        log.warning("")
        log.warning("  %d rows have a VALID postcode contradicted by the town "
                    "cell — reported, NOT rewritten:", len(mismatch))
        for c, t in mismatch[:10]:
            log.warning("      cp=%s  city=%r", c, t)

    if args.dry_run:
        log.info("DRY-RUN — nothing written.")
        conn.rollback(); conn.close(); return

    if writes:
        CHUNK = 500
        written = 0
        for i in range(0, len(writes), CHUNK):
            psycopg2.extras.execute_values(
                cur, UPDATE_SQL, writes[i:i + CHUNK], page_size=CHUNK)
            written += cur.rowcount
        log.info("  rows updated %d", written)
        cur.execute(AUDIT_SQL, (
            json.dumps({"step": "S9-H", "script": SCRIPT_NAME,
                        "selected": len(rows), "written": written,
                        "cp_dropped": sum(1 for _, c, _ in writes if c is None),
                        "mismatch_reported": len(mismatch)}, ensure_ascii=False),
            SCRIPT_NAME,
            "Repair shifted/polluted address columns: recover the postcode from "
            "the town cell, strip pasted phone numbers and duplicated postcodes",
        ))
        conn.commit()

    cur.execute("""SELECT count(*) FROM staging.sites
                   WHERE postal_code IS NOT NULL
                     AND btrim(postal_code::text) !~ '^[0-9]{5}$'""")
    log.info("Verified — sites with an unusable postal_code: %d", cur.fetchone()[0])
    conn.close()


def _self_test() -> None:
    """Every case below is a real string taken from the live table."""
    cases = [
        # (cp, city)                                     -> (cp, city)
        (("3   ", "- 59780Baisieux"),                    ("59780", "Baisieux")),
        (("103 ", "Avenue De Flandres - 59650Villeneuve D Ascq"),
                                                         ("59650", "Villeneuve D Ascq")),
        (("1   ", "Chemin Des Prieures - 80260St Gratien"),
                                                         ("80260", "St Gratien")),
        (("62960", "BEAUMETZ LES AIRE 0626930077"),      ("62960", "BEAUMETZ LES AIRE")),
        (("59145", "BERLAIMONT 06/82/90/56/83"),         ("59145", "BERLAIMONT")),
        (("62112", "GOUY SOUS BELLONNE tel 0663269032"), ("62112", "GOUY SOUS BELLONNE")),
        (("29250", "29250 SANTEC"),                      ("29250", "SANTEC")),
        (("17400", "17400  LA BENATE"),                  ("17400", "LA BENATE")),
        (("33520", "BRUGES (33520)"),                    ("33520", "BRUGES")),
        (("59260", "HELLEMMES LILLE (59260)"),           ("59260", "HELLEMMES LILLE")),
        (("02290", "AMBLENY 02290 AMBLENY"),             ("02290", "AMBLENY")),
        (("36   ", "Azay le Ferron"),                    (None,    "Azay le Ferron")),
        (("0    ", "Chamoy"),                            (None,    "Chamoy")),
        # A well-formed but WRONG postcode: '1027' zero-padded to '01027' by the
        # earlier 4-digit repair. Only the glued town cell exposes it.
        (("01027", "Rue De Lille - 59560Comines"),       ("59560", "Comines")),
        # already clean -> no change proposed
        (("59000", "LILLE"),                             None),
        (("75000", "PARIS"),                             None),
    ]
    bad = 0
    for (cp, city), want in cases:
        got = repair(cp, city)
        ok = got == want
        bad += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {cp!r:9s} {city!r:46s} -> {got!r}")
        if not ok:
            print(f"       wanted {want!r}")
    print(f"\n{len(cases) - bad}/{len(cases)} passed")
    sys.exit(1 if bad else 0)


def main() -> None:
    ap = argparse.ArgumentParser(description="Repair shifted address columns")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        _self_test()
    run(args)


if __name__ == "__main__":
    main()
