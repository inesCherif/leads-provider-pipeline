"""
M7-S12 — Quality gate: read the producteurs deliverable BACK and refuse to ship on a defect
==========================================================================================
Copy of `m6_s12_check.py` (H1–H10, each mapped to a bug that shipped or nearly
shipped in this repo) plus the M7 checks:

  H11 Sous-segment never empty
  H12 no directory / aggregator host as website_final or e-mail domain
  H13 the Sans SIRET sheet is disjoint from the main sheet (phone, e-mail)
  H14 THE PRINCIPLE: no EXCLUDED_NAF, no wine / cider / beer / pork tag or word in
      sous_segment, no excluded word in a Sans SIRET row (loose rule)
  H15 descriptif_activite and source_contexte filled iff each other

Usage:
    python scripts/m7_s12_check.py --departement 63 --version v1 --strict
    python scripts/m7_s12_check.py --departement 63 --version v2 --baseline v1 --strict
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from m2lib_contact import FREE_MAIL, is_surtaxe, plausible_fr_number   # noqa: E402
from m3ag_s12_check import PHONE_RE, EMAIL_RE, MAIRIE_RE, EXTRA_FREE_MAIL   # noqa: E402
from m7_lib import (OUT_DIR, CHECK_DIR, INHERITED_AGRI, INHERITED_ELEVEURS, read_csv,   # noqa: E402
                    is_aggregator, root_domain, EXCLUDED_NAF, EXCLUDED_RE, EXCLUDED_LOOSE_RE,
                    host_hits, phone_digits)


def load_sheet(path: Path, name: str) -> list[dict]:
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True)
    ws = wb[name]
    it = ws.iter_rows(values_only=True)
    header = [str(h) for h in next(it)]
    return [{h: ("" if v is None else v) for h, v in zip(header, vals)} for vals in it]


def main() -> None:
    ap = argparse.ArgumentParser(description="Read the producteurs deliverable back and gate it")
    ap.add_argument("--departement", default="63")
    ap.add_argument("--version", required=True)
    ap.add_argument("--baseline", default="")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--phone-drop-allow", type=int, default=0)
    ap.add_argument("--email-drop-allow", type=int, default=0)
    ap.add_argument("--site-drop-allow", type=int, default=0)
    args = ap.parse_args()
    dept = args.departement

    path = OUT_DIR / f"producteurs_{dept}_{args.version}.xlsx"
    if not path.exists():
        sys.exit(f"{path} not found")
    rows = load_sheet(path, "Producteurs")
    sans = load_sheet(path, "Sans SIRET")
    ops = read_csv(CHECK_DIR / f"operateurs_{dept}.csv", delim=",")
    verified = {}
    for d in (INHERITED_AGRI, INHERITED_ELEVEURS, CHECK_DIR):
        for r in read_csv(d / "verified_emails.csv"):
            verified[r["email"].lower()] = r["verdict"]

    fails, warns = [], []

    def check(cond, label, warn=False):
        if cond:
            print(f"  ok    {label}")
        else:
            (warns if warn else fails).append(label)
            print(f"  {'WARN' if warn else 'FAIL'}  {label}")

    g = lambda r, k: "" if r.get(k) is None else str(r[k])
    print(f"{path.name}: {len(rows)} rows, {len(rows[0]) if rows else 0} columns; Sans SIRET {len(sans)} rows")
    reg = [r for r in rows if g(r, "population_source") == "registre"]
    ele = [r for r in rows if g(r, "population_source").startswith("eleveurs")]
    key = lambda r: g(r, "siret") or f"X{g(r,'siren')}|{g(r,'raisonSociale')}|{g(r,'codePostal')}"
    keys = Counter(key(r) for r in rows)
    check(len(reg) == len(ops), f"H1 registry rows == population ({len(reg)} vs {len(ops)}; + {len(ele)} éleveurs rows)")
    check(max(keys.values()) == 1, f"H1 row key unique (max repeat {max(keys.values())})")
    bad_cp = [r for r in rows if r["codePostal"] and (not isinstance(r["codePostal"], str) or len(r["codePostal"]) != 5)]
    src_sirets = {o["siret"] for o in ops if o["siret"]}
    bad_si = [r for r in reg if r["siret"] and (not isinstance(r["siret"], str) or len(str(r["siret"])) != 14
              or str(r["siret"]) not in src_sirets)]
    check(not bad_cp, f"H2 codePostal is 5-char text ({len(bad_cp)} bad)")
    check(not bad_si, f"H2 siret is 14-char text, identical to the registry ({len(bad_si)} altered)")

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

    by_dom, by_dom_declared = defaultdict(set), defaultdict(set)
    for r in mails:
        d = str(r["email_final"]).partition("@")[2].lower()
        if d in FREE_MAIL or d in EXTRA_FREE_MAIL:
            continue
        (by_dom_declared if r["source_email"] == "agencebio" else by_dom)[d].add(key(r))
    hq = {d: s for d, s in by_dom.items() if len(s) > 2}
    check(not hq, f"H7 no discovered corporate e-mail domain on > 2 operators ({len(hq)}: {list(hq)[:3]})")

    by_tel = defaultdict(set)
    for r in tels:
        by_tel[r["telephone_final"]].add(key(r))
    shared_tel = {t: s for t, s in by_tel.items() if len(s) > 2}
    shared_tel_dir = {t: s for t, s in shared_tel.items()
                      if any(r["telephone_final"] == t and r["source_telephone"] != "agencebio" for r in rows)}
    check(not shared_tel_dir, f"H8 no directory phone on > 2 operators ({len(shared_tel_dir)})")

    by_site = defaultdict(set)
    for r in rows:
        if r["website_final"] and r["source_website"] != "agencebio":
            by_site[root_domain(str(r["website_final"]).replace("https://", "").replace("http://", "").split("/")[0].lower())].add(key(r))
    shared_site = {d: s for d, s in by_site.items() if len(s) > 1}
    check(not shared_site, f"H9 no discovered website on > 1 operator ({len(shared_site)}: {list(shared_site)[:3]})")

    # ---- M7 checks ----
    empty_seg = [r for r in rows if not g(r, "sous_segment").strip()]
    check(not empty_seg, f"H11 Sous-segment never empty ({len(empty_seg)})")
    agg_site = [r for r in rows if r["website_final"] and is_aggregator(str(r["website_final"]))]
    check(not agg_site, f"H12 no directory / aggregator host as website_final ({len(agg_site)})")
    main_phones = {phone_digits(g(r, "telephone_final")) for r in tels}
    main_mails = {g(r, "email_final").lower() for r in mails}
    overlap_p = [r for r in sans if g(r, "phone") and phone_digits(g(r, "phone")) in main_phones]
    overlap_e = [r for r in sans if g(r, "email") and g(r, "email").lower() in main_mails]
    check(not overlap_p and not overlap_e, f"H13 Sans SIRET disjoint from the main sheet ({len(overlap_p)} phones, {len(overlap_e)} e-mails shared)")
    bad_naf = [r for r in rows if g(r, "codeNAF") in EXCLUDED_NAF]
    bad_seg = [r for r in rows if EXCLUDED_RE.search(g(r, "sous_segment")) or
               any(t in ("vigneron", "cidre", "brasseur", "porcins") for t in g(r, "sous_segment").split("|"))]
    bad_host = [r for r in rows if (r["website_final"] and host_hits(str(r["website_final"]).split("//")[-1].split("/")[0]))
                or (r["email_final"] and host_hits(str(r["email_final"]).rpartition("@")[2]))]
    bad_sans = [r for r in sans if EXCLUDED_LOOSE_RE.search(" ".join(g(r, k) for k in ("name", "sous_segment", "description")))]
    check(not bad_naf, f"H14 principle: no excluded NAF in the file ({len(bad_naf)})")
    check(not bad_seg, f"H14 principle: no excluded sous-segment ({len(bad_seg)})")
    check(not bad_host, f"H14 principle: no wine / beer / pork word in a shipped site or e-mail domain ({len(bad_host)})")
    check(not bad_sans, f"H14 principle: no excluded word in a Sans SIRET row ({len(bad_sans)})")
    ctx = [r for r in rows if bool(g(r, "descriptif_activite")) != bool(g(r, "source_contexte"))]
    check(not ctx, f"H15 descriptif_activite filled iff source_contexte ({len(ctx)} mismatches)")

    n_tel, n_mail = len(tels), len(mails)
    n_site = sum(1 for r in rows if r["website_final"])
    n_reach = sum(1 for r in rows if r["telephone_final"] or r["email_final"])
    print(f"  counts: phones {n_tel} | e-mails {n_mail} | sites {n_site} | joignables {n_reach} ({n_reach/len(rows):.1%}) "
          f"| Sans SIRET phones {sum(1 for r in sans if g(r,'phone'))} e-mails {sum(1 for r in sans if g(r,'email'))}")
    if args.baseline:
        bpath = OUT_DIR / f"producteurs_{dept}_{args.baseline}.xlsx"
        if bpath.exists():
            b = load_sheet(bpath, "Producteurs")
            b_tel = sum(1 for r in b if r.get("telephone_final"))
            b_mail = sum(1 for r in b if r.get("email_final"))
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
