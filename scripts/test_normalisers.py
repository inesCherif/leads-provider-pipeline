r"""
Unit tests for the pure normalisation functions
================================================
No database, no network — runs in milliseconds.

These functions are small, but they are where the pipeline decides what a value
MEANS, and every case below is a real defect seen in this data. They encode
judgements that are easy to "simplify" later and thereby silently break:

    clean_siret       reject, never pad     a wrong SIRET imports another
                                            company's identity
    normalize_status  unknown is not active falling back to True would mark
                                            every unrecognised value alive
    clean_postal_code restore leading zero  01250 arrives as the number 1250
    repair_email      refuse to guess       a plausible invented address is a
                                            silent bounce weeks later
    parse_name        precision over yield  a wrong split generates wrong
                                            addresses in S9-7

Usage:
    python scripts/test_normalisers.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from m1_s3_ingest import (                      # noqa: E402
    clean_postal_code, clean_siret, normalize_status,
    repair_email_domain, siret_to_siren,
)
from m1_s9d_nameparse import parse_name, tokenise   # noqa: E402
from m1_s9e_email_repair import repair as repair_email  # noqa: E402

FAILURES: list[str] = []


def check(label, got, expected):
    if got != expected:
        FAILURES.append(f"{label}\n      got      {got!r}\n      expected {expected!r}")
        print(f"  FAIL {label}")
    else:
        print(f"  ok   {label}")


# ── clean_siret ──────────────────────────────────────────────────────────────
print("\nclean_siret — validate, never repair")
check("strips the separators the source uses",
      clean_siret("123 456 789 00012"), "12345678900012")
check("accepts a clean 14-digit value",
      clean_siret("44795600012345"), "44795600012345")
check("13 digits -> NULL, NOT zero-padded (a wrong SIRET matches a real, "
      "different company and imports its NAF)",
      clean_siret("1234567890001"), None)
check("15 digits -> NULL, not truncated", clean_siret("123456789000123"), None)
check("letters -> NULL", clean_siret("1234567890001A"), None)
check("pandas 'nan' sentinel -> NULL", clean_siret("nan"), None)
check("empty -> NULL", clean_siret("   "), None)

print("\nsiret_to_siren — the identifier system encodes the hierarchy")
check("first 9 chars", siret_to_siren("44795600012345"), "447956000")
check("refuses a non-14-char input", siret_to_siren("447956000"), None)
check("None in, None out", siret_to_siren(None), None)

# ── normalize_status ─────────────────────────────────────────────────────────
print("\nnormalize_status — unknown is NOT active")
check("'Actif' -> True", normalize_status("Actif"), True)
check("case-insensitive", normalize_status("ACTIF"), True)
check("'Fermé' -> False", normalize_status("Fermé"), False)
check("accent-free stem catches FERMEE", normalize_status("FERMEE"), False)
check("'Radié' -> False", normalize_status("Radié"), False)
check("'Cessation' -> False", normalize_status("Cessation d'activite"), False)
check("unrecognised -> None, NOT True (the whole point: a fallback to True "
      "would silently mark every unknown value as a live business)",
      normalize_status("???"), None)
check("empty -> None", normalize_status(""), None)

# ── clean_postal_code ────────────────────────────────────────────────────────
print("\nclean_postal_code — restore what Excel ate")
check("5-digit passes through", clean_postal_code("59000"), "59000")
check("4-digit gets its leading zero back (01250 stored as the number 1250)",
      clean_postal_code("1250"), "01250")
check("float residue '59000.0'", clean_postal_code("59000.0"), "59000")
check("internal spaces removed", clean_postal_code("59 000"), "59000")
check("empty -> None", clean_postal_code(""), None)

# ── repair_email_domain (ingest-side) ────────────────────────────────────────
print("\nrepair_email_domain — deterministic, never a guess")
check("gmailcom", repair_email_domain("a@gmailcom"), "a@gmail.com")
check("orangefr", repair_email_domain("b@orangefr"), "b@orange.fr")
check("already dotted is untouched",
      repair_email_domain("c@gmail.com"), "c@gmail.com")
check("bzh is a real TLD", repair_email_domain("d@keltikfoodbzh"),
      "d@keltikfood.bzh")
check("'me' must NOT be a rule: fermelapomme stays intact",
      repair_email_domain("e@fermelapomme"), "e@fermelapomme")
check("no recognisable TLD -> unchanged, not invented",
      repair_email_domain("f@yahoo"), "f@yahoo")
check("longest TLD wins so 'com' beats 'om'",
      repair_email_domain("g@lapostenet"), "g@laposte.net")

# ── repair (S9-E, multi-defect) ──────────────────────────────────────────────
print("\nrepair — whitespace, two-in-one, dotless")
check("internal space removed",
      repair_email("hippodrome-carentan@orange .fr"),
      ["hippodrome-carentan@orange.fr"])
check("space before the @",
      repair_email("lpa.neufchatel @educagri.fr"), ["lpa.neufchatel@educagri.fr"])
check("two addresses in one field -> BOTH kept (staging.emails is "
      "multi-candidate by design)",
      repair_email("marie.dupont@wanadoo.fr - p.martin@gmail.com"),
      ["marie.dupont@wanadoo.fr", "p.martin@gmail.com"])
check("exact provider lookup reaches what the TLD rule safely cannot",
      repair_email("x@protonme"), ["x@proton.me"])
check("truncated TLD -> refuse (com? ch? co?)",
      repair_email("lafermedubos@gmail.c"), [])
check("no TLD at all -> refuse", repair_email("y@yahoo"), [])

# ── parse_name ───────────────────────────────────────────────────────────────
print("\nparse_name — precision over yield")
GAZ = {"PATRICK", "RICHARD", "JEROME", "GEORGES", "DIANE", "JEAN", "SERGE"}
check("surname-first order", parse_name("GOURDIN PATRICK", GAZ),
      ("PATRICK", "GOURDIN"))
check("forename-first order", parse_name("RICHARD GILLIS", GAZ),
      ("RICHARD", "GILLIS"))
check("legal form stripped before counting tokens",
      parse_name("SCEA LARROQUE JEROME", GAZ), ("JEROME", "LARROQUE"))
check("civility stripped", parse_name("M PATRICK GOURDIN", GAZ),
      ("PATRICK", "GOURDIN"))
check("saints rejected: Saint/Sainte names are also first names",
      parse_name("Domaine Saint Georges", GAZ), None)
check("non-adjacent tokens rejected: a preposition between them means the "
      "phrase is descriptive, not a name",
      parse_name("AUX ARMES DE DIANE", GAZ), None)
check("both tokens are first names -> ambiguous, rejected",
      parse_name("JEAN SERGE", GAZ), None)
check("initial as surname rejected (cannot build an address)",
      parse_name("PATRICK B", GAZ), None)
check("no first name present -> rejected",
      parse_name("FERME DE LA TONNELLERIE", GAZ), None)

print("\ntokenise — accents and digits")
check("accents folded", tokenise("ANDROUIN JéRôME"), ["ANDROUIN", "JEROME"])
check("digits dropped", tokenise("ECOUTE TON CHIEN 21 39"),
      ["ECOUTE", "TON", "CHIEN"])

# ── summary ──────────────────────────────────────────────────────────────────
print("\n" + "-" * 70)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):\n")
    for f in FAILURES:
        print("  " + f)
    sys.exit(1)
print("All normaliser tests passed.")
