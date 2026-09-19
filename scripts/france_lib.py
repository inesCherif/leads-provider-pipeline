"""
FRANCE-LIB — the 96 départements of métropole, their région, and a postcode
test that does not lie about Corsica
===========================================================================
Every sector pipeline (M5, M6, M7) filters its population with

    cp.startswith(dept)

which is right for "63" and "03" and SILENTLY WRONG for Corsica: the
département codes are "2A" / "2B" but the postcodes are 200xx / 201xx /
202xx / 206xx, so `"20190".startswith("2A")` is False for every row and the
département comes out EMPTY — no crash, no warning, just a file with zero
lines. `m7_lib.dept_of_cp` already knows the rule; it was simply never used
at the filtering sites. `in_dept()` below is that function turned into the
test, and it is identical to `startswith` for the other 94 départements.

DOM (971-976) are deliberately NOT here: `dept_of_cp` collapses them to
"97", so they need their own pass (phase 2).

Usage:
    from france_lib import METRO_DEPARTEMENTS, REGION_OF, in_dept, region_dir
    python scripts/france_lib.py --selftest
    python scripts/france_lib.py --list            # dept -> région, one per line
"""

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m7_lib import dept_of_cp   # noqa: E402  (the single source of truth for cp -> dept)

# Région -> its départements. Métropole only, 13 régions, 96 départements.
REGIONS: dict[str, tuple[str, ...]] = {
    "Auvergne-Rhone-Alpes":       ("01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"),
    "Bourgogne-Franche-Comte":    ("21", "25", "39", "58", "70", "71", "89", "90"),
    "Bretagne":                   ("22", "29", "35", "56"),
    "Centre-Val-de-Loire":        ("18", "28", "36", "37", "41", "45"),
    "Corse":                      ("2A", "2B"),
    "Grand-Est":                  ("08", "10", "51", "52", "54", "55", "57", "67", "68", "88"),
    "Hauts-de-France":            ("02", "59", "60", "62", "80"),
    "Ile-de-France":              ("75", "77", "78", "91", "92", "93", "94", "95"),
    "Normandie":                  ("14", "27", "50", "61", "76"),
    "Nouvelle-Aquitaine":         ("16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"),
    "Occitanie":                  ("09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"),
    "Pays-de-la-Loire":           ("44", "49", "53", "72", "85"),
    "Provence-Alpes-Cote-d-Azur": ("04", "05", "06", "13", "83", "84"),
}

REGION_OF: dict[str, str] = {d: r for r, ds in REGIONS.items() for d in ds}

# Auvergne-Rhône-Alpes first on purpose: it holds 03 and 63, whose harvests
# already carry cross-border listings, so it is the region that validates the
# run fastest. Then the other big farming regions, then the rest.
REGION_ORDER: tuple[str, ...] = (
    "Auvergne-Rhone-Alpes", "Occitanie", "Nouvelle-Aquitaine", "Bretagne",
    "Pays-de-la-Loire", "Normandie", "Centre-Val-de-Loire", "Bourgogne-Franche-Comte",
    "Grand-Est", "Hauts-de-France", "Provence-Alpes-Cote-d-Azur", "Corse",
    "Ile-de-France",
)
assert set(REGION_ORDER) == set(REGIONS), "REGION_ORDER must list every région exactly once"

# Run order: région by région, département ascending inside a région.
METRO_DEPARTEMENTS: tuple[str, ...] = tuple(
    d for r in REGION_ORDER for d in sorted(REGIONS[r])
)

# The two already delivered (V2, gated, sent to Sam) — a France run rebuilds
# every OTHER département and leaves these alone unless asked.
ALREADY_DONE: tuple[str, ...] = ("03", "63")


def in_dept(cp: str, dept: str) -> bool:
    """Does this postcode belong to this département?

    Identical to `cp.startswith(dept)` for the 94 two-digit départements;
    the only difference is Corsica, where it is the difference between a
    population and an empty file."""
    return dept_of_cp(cp) == dept


def region_dir(dept: str) -> str:
    """The delivery folder for a département ('15' -> 'Auvergne-Rhone-Alpes')."""
    return REGION_OF.get(dept, "Hors-region")


def parse_departements(spec: str) -> list[str]:
    """'15,32,2b' -> ['15','32','2B'] · 'all' -> every métropole dept ·
    'Occitanie' or 'region:Occitanie' -> that région's départements.
    Unknown codes are refused loudly: a typo'd dept would pull 0 rows and
    look like an empty département."""
    spec = (spec or "").strip()
    if not spec:
        return []
    if spec.lower() == "all":
        return list(METRO_DEPARTEMENTS)
    bare = spec[7:] if spec.lower().startswith("region:") else spec
    for r in REGIONS:
        if bare.lower().replace(" ", "-") == r.lower():
            return sorted(REGIONS[r])
    out: list[str] = []
    for tok in spec.split(","):
        tok = tok.strip().upper()
        if not tok:
            continue
        if tok.isdigit() and len(tok) == 1:
            tok = "0" + tok
        if tok not in REGION_OF:
            sys.exit(f"unknown département '{tok}' — not one of the 96 of métropole "
                     f"(DOM 971-976 are phase 2). See: python scripts/france_lib.py --list")
        if tok not in out:
            out.append(tok)
    return out


def _selftest() -> int:
    fails = 0

    def ok(cond, label):
        nonlocal fails
        if not cond:
            fails += 1
            print(f"  FAIL  {label}")
        else:
            print(f"  ok    {label}")

    print("france_lib selftest")
    ok(len(METRO_DEPARTEMENTS) == 96, f"96 départements ({len(METRO_DEPARTEMENTS)})")
    ok(len(set(METRO_DEPARTEMENTS)) == 96, "no duplicate département")
    ok("20" not in METRO_DEPARTEMENTS, "'20' is not a département (it is 2A/2B)")
    ok(all(d in REGION_OF for d in ALREADY_DONE), "03 and 63 are in the map")
    ok(REGION_OF["63"] == "Auvergne-Rhone-Alpes", "63 -> Auvergne-Rhone-Alpes")
    ok(METRO_DEPARTEMENTS[:3] == ("01", "03", "07"), "AuRA runs first")

    # The bug this module exists for.
    ok(in_dept("20190", "2A") and not in_dept("20190", "2B"), "20190 -> 2A (Ajaccio side)")
    ok(in_dept("20200", "2B") and not in_dept("20200", "2A"), "20200 -> 2B (Bastia side)")
    ok(in_dept("20600", "2B"), "20600 -> 2B (Furiani)")
    ok(not "20190".startswith("2A"), "startswith() would have dropped it — the whole point")

    # Identical to startswith everywhere else, including a leading zero.
    for cp, d in (("63000", "63"), ("03100", "03"), ("32000", "32"), ("75015", "75")):
        ok(in_dept(cp, d) is cp.startswith(d), f"{cp} in {d} == startswith")
    ok(not in_dept("63000", "03"), "63000 is not in 03")
    ok(not in_dept("", "63") and not in_dept("6300", "63"), "empty / short postcode is nobody's")
    # DOM: dept_of_cp collapses 971-976 to "97", so in_dept("97400","97") is
    # True but "97" is not a département. They are kept out at the parse gate,
    # which is why a France run can never silently mix Guadeloupe into "97".
    ok(in_dept("97400", "97"), "97400 -> '97' (dept_of_cp collapses the DOM)")
    ok("97" not in REGION_OF, "'97' is not a département of the run (phase 2)")

    ok(parse_departements("15,32,2b") == ["15", "32", "2B"], "parse '15,32,2b'")
    ok(parse_departements("Corse") == ["2A", "2B"], "parse a région name")
    ok(len(parse_departements("all")) == 96, "parse 'all'")

    print(f"\n{'PASS' if not fails else str(fails) + ' FAILED'}")
    return 1 if fails else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--list", action="store_true", help="dept -> région, in run order")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(_selftest())
    if args.list:
        for d in METRO_DEPARTEMENTS:
            print(f"{d}  {REGION_OF[d]}" + ("   (already delivered V2)" if d in ALREADY_DONE else ""))
        print(f"\n{len(METRO_DEPARTEMENTS)} départements, {len(REGIONS)} régions")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
