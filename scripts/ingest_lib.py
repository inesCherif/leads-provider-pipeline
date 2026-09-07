r"""
ingest_lib — the pure functions and the writers behind m1_s3_ingest.py
=======================================================================
Library, not a stage. Two halves:

  1. NORMALISERS (no DB, no network) — exactly the functions that used to live
     inline in m1_s3_ingest.py, moved here unchanged in behaviour, plus the
     ones the M4 provider files made necessary (float-mangled numbers, phones
     in five formats, glued e-mail domains). `scripts/test_normalisers.py` is
     their spec; every case there is a real defect seen in this data.

  2. WRITERS — one function per staging table, each idempotent (ON CONFLICT on
     the natural key or a look-up first), each meant to run in its OWN short
     transaction. The three data-loss incidents in this project all came from
     one long transaction on a pooled connection; the loader commits after
     every stage so a dropped socket costs a re-run, not a file.

Judgements encoded here (do not "simplify" them away):

  * clean_siret validates, it never pads. A 13-digit SIRET zero-padded into a
    Luhn-valid 14-digit one still has ~10% odds of being ANOTHER company, and
    m1_s4 would then import that company's NAF. Truncated values are kept as
    `siret_truncated` identifiers for a later name-checked recovery.
  * A float artefact (`30819741700014.0`, `2988.0`, `475591313.0`) is an
    Excel export defect, not data: the trailing `.0` is stripped BEFORE any
    other rule, and a 9-digit phone gets its leading zero back.
  * An e-mail whose domain fails the strict TLD test is `malformed` and is
    stored ONLY in staging.contact_points, never in staging.emails (which
    ships). 1,181 glued domains in the tourism file pass a naive regex.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from m2lib_contact import normalize_fr_phone, is_surtaxe            # noqa: E402
from m1_s9f_email_glued_repair import split_glued                     # noqa: E402

# ─── sentinels & float artefacts ─────────────────────────────────────────────

NULL_TOKENS = {"", "nan", "none", "null", "n/a", "na", "-", "0", "0.0"}
_FLOAT_TAIL = re.compile(r"\.0+$")


def strip_float_artefact(val) -> str | None:
    """'30819741700014.0' -> '30819741700014'. None for pandas sentinels."""
    if val is None:
        return None
    s = str(val).replace(" ", " ").strip()
    if s.lower() in ("", "nan", "none", "null"):
        return None
    return _FLOAT_TAIL.sub("", s)


def clean_str(val) -> str | None:
    """Strip whitespace and NBSP; pandas sentinels -> None."""
    if val is None:
        return None
    s = str(val).replace(" ", " ").strip()
    return None if s.lower() in ("", "nan", "none", "null") else s


# ─── identifiers ─────────────────────────────────────────────────────────────

def clean_siret(raw) -> str | None:
    """14 digits or None. Strips separators and the float `.0`; never pads."""
    s = strip_float_artefact(raw)
    if s is None:
        return None
    cleaned = re.sub(r"[\s.\-]", "", s)
    if len(cleaned) == 14 and cleaned.isdigit() and cleaned != "0" * 14:
        return cleaned
    return None


def clean_siren(raw) -> str | None:
    """9 digits or None. Same rules as clean_siret (no padding)."""
    s = strip_float_artefact(raw)
    if s is None:
        return None
    cleaned = re.sub(r"[\s.\-]", "", s)
    if len(cleaned) == 9 and cleaned.isdigit() and cleaned != "0" * 9:
        return cleaned
    return None


def siret_to_siren(siret: str | None) -> str | None:
    return siret[:9] if siret and len(siret) == 14 else None


def truncated_identifier(raw) -> str | None:
    """A digit string of 8, 12 or 13 digits: a SIREN/SIRET that lost its
    leading zero. Returned as-is (NOT padded) so it can be recorded as a
    `siret_truncated` identifier and recovered later with a name check."""
    s = strip_float_artefact(raw)
    if s is None:
        return None
    cleaned = re.sub(r"[\s.\-]", "", s)
    if cleaned.isdigit() and len(cleaned) in (8, 12, 13) and set(cleaned) != {"0"}:
        return cleaned
    return None


def luhn_ok(digits: str) -> bool:
    """Luhn check as used by SIREN/SIRET. Diagnostic only (La Poste's SIRETs
    legitimately fail it)."""
    if not digits or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ─── status / postal / department ────────────────────────────────────────────

def normalize_status(raw) -> bool | None:
    if raw is None or str(raw).strip() in ("", "nan", "None", "NaN"):
        return None
    s = str(raw).strip().lower()
    if "actif" in s:
        return True
    if any(k in s for k in ("ferm", "cess", "radié", "radie")):
        return False
    return None


def clean_postal_code(val) -> str | None:
    """5-digit French postcode or None.

    Order matters: the float artefact is stripped FIRST, otherwise '2988.0'
    became '29880' (dot removed, then truncated to 5) — a real Loire postcode
    for what is actually 02988, which is not even a postcode (it is a phone
    fragment in the camping file, but the leading-zero shape is the same bug
    as 01250 -> 1250)."""
    s = strip_float_artefact(val)
    if s is None:
        return None
    s = s.replace(" ", "").replace(".", "")
    if not s:
        return None
    s = s[:5]
    # Restore the leading zero Excel ate: 01250 stored as the number 1250.
    if s.isdigit() and len(s) == 4:
        s = s.zfill(5)
    return s


def clean_department(val) -> str | None:
    """'69' / '2A' / '974' or None. Anything else ('SO', a street) is noise."""
    s = clean_str(strip_float_artefact(val))
    if not s:
        return None
    s = s.upper()
    if re.fullmatch(r"[0-9]{2,3}|2A|2B", s):
        return s.zfill(2) if s.isdigit() and len(s) == 1 else s
    return None


# ─── phones ──────────────────────────────────────────────────────────────────

def clean_phone(raw) -> str | None:
    """Canonical `0X XX XX XX XX` or None.

    Handles every shape the provider files use: '33472897000' (11 digits,
    country code, no plus), '475591313.0' (float, leading zero lost),
    '05 57 74 63 40', '+33 4 ...', '0033...'. Junk ('33GUEUGNON', '333631')
    -> None. Premium-rate numbers are RETURNED (flagged by is_surtaxe), never
    silently dropped — the 12.5% fake-dedup lesson."""
    s = strip_float_artefact(raw)
    if s is None:
        return None
    d = re.sub(r"\D", "", s)
    if not d:
        return None
    if len(d) == 9 and d[0] != "0":
        d = "0" + d                   # bare 9-digit: leading zero lost
    return normalize_fr_phone(d) or None


def phone_digits(canonical: str | None) -> str | None:
    """'04 75 59 13 13' -> '0475591313' (the contact_points.value_norm)."""
    return re.sub(r"\D", "", canonical) if canonical else None


# ─── e-mails ─────────────────────────────────────────────────────────────────

# Longest TLD first: 'com' must beat 'om'/'m', or gmailcom becomes gmailc.om.
# 'me' is deliberately absent — any domain ending in the letters "me"
# (fermelapomme) would be corrupted into fermelapom.me.
KNOWN_TLDS = ("coop", "info", "biz", "bzh", "com", "net", "org", "pro", "eu",
              "fr", "be", "ch", "io")

# TLDs an address may END with and still be shipped. Broader than KNOWN_TLDS
# (which drives the dotless REPAIR): a domain ending in .paris is fine to
# mail, but we would never invent it.
SHIPPABLE_TLDS = frozenset("""
fr com net org eu info biz coop pro io ch be de es it uk lu ca us co re nc pf
pm yt wf tf gf gp mq bzh paris alsace corsica me tv cc mobi name eus cat gg je
im nl pt at dk se no fi ie pl cz ma tn dz sn ci cm mg mu
""".split())

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")
LOCAL_OK_RE = re.compile(r"^[a-z0-9._%+\-']+$")


def repair_email_domain(address: str) -> str:
    """Insert a missing dot before a recognised TLD. Returns input unchanged if
    the domain already has a dot or the ending is not recognised — never guess."""
    local, _, domain = address.partition("@")
    if not local or not domain or "." in domain:
        return address
    for tld in KNOWN_TLDS:
        if domain.endswith(tld) and len(domain) > len(tld):
            return f"{local}@{domain[:-len(tld)]}.{tld}"
    return address


def classify_email(raw) -> tuple[str | None, str, dict]:
    """(value_norm, verdict, evidence).

    verdict:
      'candidate'  well-formed, shippable (subject to later verification)
      'malformed'  must NOT reach staging.emails; evidence names the defect
                   and, when one exists, a deterministic repair candidate
      'empty'      nothing usable
    Rules, in order (each is a measured defect class):
      * whitespace inside the LOCAL part is removed (S9-E rule: 'orange .fr')
      * whitespace inside the DOMAIN is ambiguous ('systeme u.fr' is probably
        systeme-u.fr) -> malformed, candidate = hyphenated form
      * a dotless domain is repaired with KNOWN_TLDS ('gmailcom' -> gmail.com)
      * a glued domain ('gmail.coml.com', 'orange.frnadoo.fr') -> malformed,
        candidate = split_glued() (S9-F), to be promoted only after an MX check
      * the final label must be a shippable TLD ('orange.frfr' is not)
    """
    s = clean_str(raw)
    if not s:
        return None, "empty", {}
    s = s.lower().strip().strip(".;,")
    s = s.replace("mailto:", "")
    if s.count("@") != 1:
        return s, "malformed", {"defect": "at_count", "raw": s}
    local, domain = s.split("@")
    if " " in local:
        local = local.replace(" ", "")          # S9-E: deterministic
    domain = domain.strip().strip(".")
    if " " in domain:
        return f"{local}@{domain}", "malformed", {
            "defect": "space_in_domain",
            "candidate": f"{local}@{domain.replace(' ', '-')}"}
    if "." not in domain:
        repaired = repair_email_domain(f"{local}@{domain}")
        if "." not in repaired.partition("@")[2]:
            return f"{local}@{domain}", "malformed", {"defect": "no_tld"}
        domain = repaired.partition("@")[2]
    glued = split_glued(domain)
    if glued is not None:
        return f"{local}@{domain}", "malformed", {
            "defect": "glued_domain", "candidate": f"{local}@{glued}"}
    tld = domain.rsplit(".", 1)[-1]
    if tld not in SHIPPABLE_TLDS:
        return f"{local}@{domain}", "malformed", {"defect": "unknown_tld", "tld": tld}
    addr = f"{local}@{domain}"
    if not LOCAL_OK_RE.match(local) or not EMAIL_RE.match(addr) \
            or not re.fullmatch(r"[a-z0-9.\-]+", domain):
        return addr, "malformed", {"defect": "shape"}
    return addr, "candidate", {}


# ─── file & contract ─────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_column_contract(path: Path, df, col_map: dict, required: set) -> None:
    """Fail before writing if the file no longer has the shape we mapped.

      - REQUIRED missing  -> hard error.
      - MAPPED but absent -> hard error: a mapped column that is not in the
        file would import as NULL for every row without raising anything —
        that is exactly how 6,523 'Fermé' verdicts were lost on 2026-07-12.
      - present but UNMAPPED -> warn only (raw_json keeps them regardless).
    """
    actual = set(df.columns)
    problems = (set(required) | set(col_map)) - actual
    if problems:
        raise SystemExit(
            f"\nCOLUMN CONTRACT VIOLATION in {path.name}\n"
            f"  mapped/required but NOT in the file: {sorted(problems)}\n"
            f"  columns actually present           : {sorted(actual)}\n\n"
            "  Refusing to import. Fix the spec in config/ingest_specs.py, then re-run."
        )
    unmapped = sorted(actual - set(col_map))
    if unmapped:
        print(f"  [note] {len(unmapped)} source column(s) not mapped "
              f"(kept in raw_json): {unmapped[:8]}{' …' if len(unmapped) > 8 else ''}")


# ─── connection ──────────────────────────────────────────────────────────────

def get_conn(attempts: int = 4):
    """Connect with TCP keepalives and a bounded retry.

    Same recipe as m1_s8_export.connect: the session pooler intermittently
    rejects a fresh connection (EAUTHTIMEOUT) and drops idle-looking SSL
    sessions mid-execute ('SSL SYSCALL error: EOF detected'). Keepalives hold
    the socket; the retry makes the script re-runnable without babysitting.
    """
    import psycopg2
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    url = os.getenv("SUPABASE_DB_URL")
    if not url:
        sys.exit("ERROR: SUPABASE_DB_URL not found in .env")
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return psycopg2.connect(url, keepalives=1, keepalives_idle=30,
                                    keepalives_interval=10, keepalives_count=5)
        except psycopg2.OperationalError as exc:
            last = exc
            print(f"  [conn] attempt {attempt}/{attempts} failed: {str(exc).strip()[:120]}")
            time.sleep(3 * attempt)
    raise SystemExit(f"could not connect after {attempts} attempts: {last}")


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
