"""
M3AG-S12 — Quality gate: read the deliverable BACK and refuse to ship on a defect
==============================================================================
Reads the xlsx (not the logs, not the CSV) the way the client will. Every
check maps to a bug that actually shipped or nearly shipped in this repo:

  H1  one row per operator, row key unique            (DISTINCT ON incident)
  H2  SIRET / CP stored as text, CP 5 chars           (4,47956E+13, 01250)
  H3  telephone_final normalised, plausible, not surtaxé
  H4  source_* filled iff value filled                (provenance never implied)
  H5  corrobore(...) names >= 2 distinct witnesses    (H15 on boulangerie)
  H6  e-mail shape, dotted domain, no aggregator/mairie domain, no 'invalide'
  H7  a non-free-mail domain on > 2 SIRETs as email_final = franchise HQ (H26)
  H8  a phone on > 2 SIRETs = switchboard (warn when owner-declared)
  H9  a website domain on > 1 SIRET unless owner-declared (H19)
  H10 counts never drop vs the baseline version       (--*-drop-allow N)

Usage:
    python scripts/m3ag_s12_check.py --departement 63 --version v2 --baseline v1 --strict
    python scripts/m3ag_s12_check.py --departement 03 --version v1 --strict
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m2lib_contact import FREE_MAIL, is_surtaxe, plausible_fr_number   # noqa: E402
from m3ag_lib import OUT_DIR, CHECK_DIR, read_csv, is_aggregator, root_domain  # noqa: E402

PHONE_RE = re.compile(r"^0\d \d\d \d\d \d\d \d\d$")
EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
# consumer mail providers m2lib_contact.FREE_MAIL does not list (seen in the
# Agence Bio declarations): sharing one is not a franchise signal.
EXTRA_FREE_MAIL = {"ecomail.fr", "lilo.org", "mailo.com", "posteo.net", "posteo.de",
                   "tutanota.com", "tuta.io", "zaclys.net", "ouvaton.org",
                   "riseup.net", "netcourrier.com", "net-c.com", "nordnet.fr",
                   "cegetel.net", "9online.fr", "hotmail.de", "yahoo.de",
                   "mailoo.org", "infomaniak.com", "ik.me", "ymail.com",
                   "live.com", "orange.com", "sfr.com", "free.com"}
MAIRIE_RE = re.compile(r"(^|[.\-])(mairie|ville|cc|cdc|agglo|communaute)[.\-]|\.gouv\.fr$")


def load_xlsx(path: Path) -> list[dict]:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = [str(h) for h in next(it)]
    rows = []
    for vals in it:
        rows.append({h: ("" if v is None else v) for h, v in zip(header, vals)})
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Read the agri deliverable back and gate it")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--version", required=True)
    ap.add_argument("--baseline", default="")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--phone-drop-allow", type=int, default=0)
    ap.add_argument("--email-drop-allow", type=int, default=0)
    ap.add_argument("--site-drop-allow", type=int, default=0)
    args = ap.parse_args()
    dept = args.departement

    path = OUT_DIR / f"agriculteurs_{dept}_{args.version}.xlsx"
    if not path.exists():
        sys.exit(f"{path} not found")
    rows = load_xlsx(path)
    ops = read_csv(CHECK_DIR / f"operateurs_{dept}.csv", delim=",")
    verified = {r["email"].lower(): r["verdict"] for r in read_csv(CHECK_DIR / "verified_emails.csv")}

    fails, warns = [], []

    def check(cond, label, warn=False):
        if cond:
            print(f"  ok    {label}")
        else:
            (warns if warn else fails).append(label)
            print(f"  {'WARN' if warn else 'FAIL'}  {label}")

    print(f"{path.name}: {len(rows)} rows, {len(rows[0]) if rows else 0} columns")
    # Row identity is the Agence Bio operator (numeroBio): 13 dept-63 SIRETs
    # carry two certifications (DAY BY DAY / VRAP AU VRAC share one legal
    # entity). They ship as two rows, as in Sam's sample; the count is
    # reported so a phone campaign can dedupe on SIRET.
    key = lambda r: str(r["siret"]) or f"NB{r['numeroBio']}"
    nbs = Counter(str(r["numeroBio"]) for r in rows)
    sirets = Counter(str(r["siret"]) for r in rows if r["siret"])
    dup_siret = sum(1 for s, c in sirets.items() if c > 1)
    check(len(rows) == len(ops), f"H1 row count == operators ({len(rows)} vs {len(ops)})")
    check(max(nbs.values()) == 1, f"H1 numeroBio unique (max repeat {max(nbs.values())})")
    print(f"  info  H1 SIRETs holding 2+ certifications (rows): {dup_siret}")
    bad_cp = [r for r in rows if r["codePostal"] and (not isinstance(r["codePostal"], str) or len(r["codePostal"]) != 5)]
    # H2 guards OUR handling (Excel turning text into 4,47956E+13), not the
    # registry's: Agence Bio itself prints two 13-digit SIRETs in dept 03.
    src_siret = {o["numeroBio"]: o["siret"] for o in ops}
    bad_si = [r for r in rows if r["siret"] and (not isinstance(r["siret"], str)
              or str(r["siret"]) != src_siret.get(str(r["numeroBio"]), str(r["siret"])))]
    src_short = sum(1 for o in ops if o["siret"] and len(o["siret"]) != 14)
    check(not bad_cp, f"H2 codePostal is 5-char text ({len(bad_cp)} bad)")
    check(not bad_si, f"H2 siret is text, identical to the source ({len(bad_si)} altered)")
    if src_short:
        print(f"  info  H2 SIRETs malformed IN THE SOURCE (kept as-is): {src_short}")

    tels = [r for r in rows if r["telephone_final"]]
    bad_shape = [r for r in tels if not PHONE_RE.match(str(r["telephone_final"]))]
    bad_plaus = [r for r in tels if PHONE_RE.match(str(r["telephone_final"])) and
                 (not plausible_fr_number(r["telephone_final"]) or is_surtaxe(r["telephone_final"]))]
    check(not bad_shape, f"H3 phones normalised ({len(bad_shape)} bad shape)")
    check(not bad_plaus, f"H3 phones plausible & not surtaxé ({len(bad_plaus)} bad)")

    for val, src in (("telephone_final", "source_telephone"), ("email_final", "source_email"),
                     ("website_final", "source_website")):
        mism = [r for r in rows if bool(r[val]) != bool(r[src])]
        check(not mism, f"H4 {src} filled iff {val} filled ({len(mism)} mismatches)")

    corr = [r for r in tels if str(r["source_telephone"]).startswith("corrobore")]
    bad_corr = [r for r in corr if len(re.findall(r"[^+()]+", str(r["source_telephone"])[10:].strip("()"))) < 2]
    check(not bad_corr, f"H5 corrobore labels name >= 2 witnesses ({len(corr)} corroborated, {len(bad_corr)} bad)")

    mails = [r for r in rows if r["email_final"]]
    bad_mail = [r for r in mails if not EMAIL_RE.match(str(r["email_final"]).lower())]
    agg_mail = [r for r in mails if is_aggregator(str(r["email_final"]).partition("@")[2])
                and str(r["email_final"]).partition("@")[2] not in FREE_MAIL]
    mairie = [r for r in mails if MAIRIE_RE.search(str(r["email_final"]).partition("@")[2])]
    inval = [r for r in mails if verified.get(str(r["email_final"]).lower()) == "invalide"]
    check(not bad_mail, f"H6 e-mail shape ({len(bad_mail)} bad)")
    check(not agg_mail, f"H6 no aggregator-domain e-mail ({len(agg_mail)})")
    check(not mairie, f"H6 no town-hall e-mail ({len(mairie)})")
    check(not inval, f"H6 no e-mail verified invalide ({len(inval)})")

    # H7 applies to DISCOVERED addresses. An owner-declared (agencebio) address
    # shared by several operators is their choice (Carrefour bio outlets all
    # declare carrefour.com) and is reported, not refused.
    by_dom = defaultdict(set)
    by_dom_declared = defaultdict(set)
    for r in mails:
        d = str(r["email_final"]).partition("@")[2].lower()
        if d in FREE_MAIL or d in EXTRA_FREE_MAIL:
            continue
        (by_dom_declared if r["source_email"] == "agencebio" else by_dom)[d].add(key(r))
    hq = {d: s for d, s in by_dom.items() if len(s) > 2}
    hq_decl = {d: len(s) for d, s in by_dom_declared.items() if len(s) > 2}
    check(not hq, f"H7 no discovered corporate e-mail domain on > 2 operators ({len(hq)}: {list(hq)[:3]})")
    if hq_decl:
        print(f"  info  H7 owner-declared domains shared by > 2 operators: {hq_decl}")

    by_tel = defaultdict(set)
    for r in tels:
        by_tel[r["telephone_final"]].add(key(r))
    shared_tel = {t: s for t, s in by_tel.items() if len(s) > 2}
    shared_tel_dir = {t: s for t, s in shared_tel.items()
                      if any(r["telephone_final"] == t and r["source_telephone"] != "agencebio" for r in rows)}
    check(not shared_tel_dir, f"H8 no directory phone on > 2 operators ({len(shared_tel_dir)})")
    if shared_tel:
        print(f"  info  H8 owner-declared phone on > 2 operators: "
              f"{[(t, len(s)) for t, s in list(shared_tel.items())[:3]]}")

    by_site = defaultdict(set)
    for r in rows:
        if r["website_final"] and r["source_website"] != "agencebio":
            by_site[root_domain(str(r["website_final"]).replace("https://", "").replace("http://", "").split("/")[0].lower())].add(key(r))
    shared_site = {d: s for d, s in by_site.items() if len(s) > 1}
    check(not shared_site, f"H9 no discovered website on > 1 operator ({len(shared_site)}: {list(shared_site)[:3]})")

    n_tel, n_mail = len(tels), len(mails)
    n_site = sum(1 for r in rows if r["website_final"])
    n_reach = sum(1 for r in rows if r["telephone_final"] or r["email_final"])
    print(f"  counts: phones {n_tel} | e-mails {n_mail} | sites {n_site} | joignables {n_reach} "
          f"({n_reach/len(rows):.1%})")
    if args.baseline:
        bpath = OUT_DIR / f"agriculteurs_{dept}_{args.baseline}.xlsx"
        if bpath.exists():
            b = load_xlsx(bpath)
            b_tel = sum(1 for r in b if r.get("telephone_final"))
            b_mail = sum(1 for r in b if r.get("email_final") or (not any("email_final" in x for x in b[:1]) and r.get("email")))
            b_site = sum(1 for r in b if r.get("website_final"))
            check(n_tel >= b_tel - args.phone_drop_allow, f"H10 phones {b_tel} -> {n_tel}")
            check(n_mail >= b_mail - args.email_drop_allow, f"H10 e-mails {b_mail} -> {n_mail}")
            check(n_site >= b_site - args.site_drop_allow, f"H10 sites {b_site} -> {n_site}")
        else:
            check(False, f"H10 baseline {bpath.name} not found", warn=True)

    print("-" * 62)
    print(f"{len(fails)} failed, {len(warns)} warning(s)")
    if fails or (args.strict and warns):
        sys.exit(1)


if __name__ == "__main__":
    main()
