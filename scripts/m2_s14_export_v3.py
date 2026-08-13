"""
M2-S14 — Build the enriched deliverable (V3)
=============================================
V2 shipped 105 phones on 1,704 bakeries — a list nobody can call. V3 adds the
Google-Maps sweep (m2_s13), the API-backed website discovery (m2_s8) and the
phones/socials read off the sites themselves (m2_s9), and writes

    exports/boulangerie/boulangerie_13_v3.xlsx    <- the enriched deliverable
    exports/boulangerie/boulangerie_13_v3.csv

V2's files are left untouched, so the previous deliverable stays reproducible.

Inputs (each optional — the export degrades, it never crashes on a missing
harvest):
    etablissements.csv   the V1 population, one row per établissement
    matched.csv          phone/website/facebook/email from OSM, Maps, PJ
    site_emails.csv      e-mails read off the businesses' own websites
    site_contacts.csv    phones + facebook/instagram/linkedin off those sites
    discovered_sites.csv candidate sites + geo-gated snippet phones (m2_s8)
    verified_emails.csv  SMTP verdict per address

Rules carried over from agriculture and V2, all bought with incidents:

  * AN ADDRESS VERIFIED `invalide` NEVER SHIPS (agriculture migration 015:
    marking an address invalid did not stop it exporting, so verification
    silently accomplished nothing).
  * A `faible`-confidence address ships only with its confidence stated.
  * EVERY PHONE IS NORMALIZED to `0X XX XX XX XX` and carries its source, so
    a disagreement between sources is auditable instead of invisible.
  * A SURTAXÉ number (089/081/082) is flagged, never silently shipped and
    never silently dropped. Agriculture once collapsed 12.5% of its base as
    "duplicates" because thousands of rows shared one premium-rate hotline
    printed by a directory — the number was real, it just was not the
    business's own line. Ranked last, disclosed in its own column.

Phone precedence — set by MEASUREMENT, not by intuition (2026-08-13):

    osm > serper_places > pagesjaunes > site/confirme

  OSM and Google Maps agree with each other on 45 of the 47 businesses both
  describe (**95.7%**) — two independent sources corroborating one number.
  Nothing else here has that. Intuition said a business's own website should
  outrank a directory; the measurement disagreed, so the directory wins.

  Two sources are EXCLUDED from the dialled column entirely, and ship in
  `Telephone piste (non confirme)` instead:

  * SEARCH SNIPPETS — 76% agreement with Maps (43 overlapping businesses).
    One disagreement was an 01 (Paris) switchboard on a Marseille bakery.
  * UNCONFIRMED SITES (`site/faible`) — 17% agreement, rising to only 50%
    after the switchboard guard. `faible` means the domain was never proved
    to belong to this business, and the phone on a stranger's website is a
    stranger's phone. This is the `EARL DU VIEUX CHENE -> vieuxchene.fr`
    lesson from agriculture, in phone form.

  `site/confirme` (SIRET/SIREN/CP+name proved on the page) agrees with Maps
  62% of the time. That disagreement does not prove the site wrong — a shop's
  own page is arguably more current than a directory — so it is kept, but
  ranked last so it only ever fills a gap no corroborated source can.

  One wrong number in four means a salesperson calls a stranger. Everything
  uncertain is disclosed in its own column, not discarded and not oversold.

Usage:
    python scripts/m2_s14_export_v3.py
    python scripts/m2_s14_export_v3.py --only-reachable
"""

import argparse
import csv
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts.m1_s8_export import ILLEGAL_XML            # noqa: E402
from m2lib_contact import normalize_fr_phone, is_surtaxe  # noqa: E402

CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OUT_DIR   = PROJECT_ROOT / "exports" / "boulangerie"
BASENAME  = "boulangerie_13_v3"

COLUMNS = [
    ("siret",                "SIRET"),
    ("siren",                "SIREN"),
    ("raison_sociale",       "Raison sociale"),
    ("enseigne",             "Enseigne"),
    ("activite",             "Activite"),
    ("naf_code",             "Code NAF"),
    ("adresse",              "Adresse"),
    ("code_postal",          "Code postal"),
    ("commune",              "Ville"),
    ("prenom",               "Prenom"),
    ("nom",                  "Nom"),
    ("fonction",             "Fonction"),
    ("telephone",            "Telephone"),
    ("telephone_source",     "Telephone source"),
    ("telephone_surtaxe",    "Telephone surtaxe"),
    ("telephone_piste",      "Telephone piste (non confirme)"),
    ("email",                "Email"),
    ("email_statut",         "Email verifie"),
    ("email_confiance",      "Email confiance"),
    ("autres_emails",        "Autres emails"),
    ("site_web",             "Site web"),
    ("facebook",             "Facebook"),
    ("instagram",            "Instagram"),
    ("linkedin",             "LinkedIn"),
    ("source_contact",       "Source contact"),
    ("contact",              "Contact (personne morale)"),
    ("forme_juridique",      "Forme juridique"),
    ("date_creation",        "Date de creation"),
    ("anciennete_ans",       "Anciennete (ans)"),
    ("tranche_effectif",     "Tranche effectif"),
    ("est_siege",            "Siege social"),
    ("procedure_collective", "Procedure collective"),
]
TEXT_COLUMNS = {"siret", "siren", "code_postal", "naf_code", "date_creation", "telephone"}

# Best-first. A confirmed domain with an SMTP accept is a fact; a franchise
# mailbox on an unconfirmed site is a guess. They must not sort equally.
RANK = {("confirme", "valide"): 0, ("confirme", "non verifie"): 1,
        ("confirme", "risque"): 2, ("faible", "valide"): 3,
        ("faible", "non verifie"): 4, ("faible", "risque"): 5}

# Phone provenance ranking. Lower is better; surtaxé adds +100 so a premium
# number always loses to any ordinary one, whatever its source. "snippet" and
# "site/faible" are deliberately absent — they are not candidates for this
# column at all (see the docstring for the measurements that decided it).
PHONE_RANK = {"osm": 0, "serper_places": 1, "pagesjaunes": 2,
              "site/confirme": 3}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m2_s14")


def read(name: str) -> list[dict]:
    p = CHECK_DIR / name
    if not p.exists():
        log.info(f"(skip) {name} not present — that enrichment did not run")
        return []
    with p.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh, delimiter=";"))


def cell(row: dict, col: str) -> str:
    return ILLEGAL_XML.sub("", str(row.get(col) or "")).strip()


def write_xlsx(path: Path, rows: list[dict]) -> None:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title="Boulangeries 13")
    ws.freeze_panes = "A2"
    head = []
    for _, h in COLUMNS:
        c = WriteOnlyCell(ws, value=h)
        c.font = Font(bold=True)
        head.append(c)
    ws.append(head)
    for r in rows:
        out = []
        for col, _ in COLUMNS:
            c = WriteOnlyCell(ws, value=cell(r, col))
            if col in TEXT_COLUMNS:
                c.number_format = "@"
            out.append(c)
        ws.append(out)
    wb.save(path)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the V3 boulangerie deliverable")
    ap.add_argument("--only-reachable", action="store_true",
                    help="keep only rows with a phone or an email")
    args = ap.parse_args()

    base = read("etablissements.csv")
    if not base:
        sys.exit("etablissements.csv missing — run scripts/m2_s2_transform.py first.")
    matched = read("matched.csv")
    site_emails = read("site_emails.csv")
    site_contacts = read("site_contacts.csv")
    discovered = read("discovered_sites.csv")
    verified = {r["email"].lower(): r["verdict"] for r in read("verified_emails.csv")}

    # siret -> [(rank, phone, source)] — every claim kept, best one exported.
    phones: dict = defaultdict(list)
    piste: dict = {}       # search-snippet phones — 76% precise, own column
    website, facebook, instagram, linkedin = {}, {}, {}, {}
    srcs = defaultdict(set)
    cand: dict = defaultdict(list)   # siret -> [(rank, email, verdict, conf, src)]

    def add_phone(siret, raw, source):
        """Route a phone to the dialled column or the `piste` column.

        A source absent from PHONE_RANK is not trusted enough to dial; it is
        kept as a lead rather than thrown away.
        """
        p = normalize_fr_phone(raw)
        if not p:
            return
        if source not in PHONE_RANK:
            piste.setdefault(siret, p)
            return
        rank = PHONE_RANK[source] + (100 if is_surtaxe(p) else 0)
        phones[siret].append((rank, p, source))

    for m in matched:
        s, src = m["siret"], m["source"]
        add_phone(s, m.get("phone", ""), src)
        if m.get("phone"):
            srcs[s].add(src)
        if m.get("website") and s not in website:
            website[s] = m["website"]
            srcs[s].add(src)
        if m.get("facebook") and s not in facebook:
            facebook[s] = m["facebook"]
        if m.get("email"):
            e = m["email"].lower()
            v = verified.get(e, "non verifie")
            cand[s].append((RANK.get(("confirme", v), 9), e, v, "confirme", src))
            srcs[s].add(src)

    for r in site_emails:
        s, e = r["siret"], r["email"].lower()
        v = verified.get(e, "non verifie")
        conf = r.get("confiance") or "faible"
        cand[s].append((RANK.get((conf, v), 9), e, v, conf, "site"))
        srcs[s].add("site")
        if r.get("domain") and s not in website:
            website[s] = "https://" + r["domain"]

    for r in site_contacts:
        s = r["siret"]
        # site/faible is not in PHONE_RANK, so add_phone routes it to `piste`.
        src = "site/confirme" if r.get("confiance") == "confirme" else "site/faible"
        add_phone(s, r.get("phone", ""), src)
        if r.get("phone") and r.get("confiance") == "confirme":
            srcs[s].add("site")
        for key, store in (("facebook", facebook), ("instagram", instagram),
                           ("linkedin", linkedin)):
            if r.get(key) and s not in store:
                store[s] = r[key]
        if r.get("domain") and s not in website:
            website[s] = "https://" + r["domain"]

    for r in discovered:
        s = r["siret"]
        # Snippet phones are geo-gated at collection but still measured at only
        # 76% agreement with Maps — they go to the `piste` column, never to
        # `Telephone`. See the module docstring.
        if r.get("phone") and r.get("snippet_geo_ok"):
            add_phone(s, r["phone"], "snippet")     # -> piste, never dialled
        if r.get("website") and s not in website:
            website[s] = r["website"]
            srcs[s].add("recherche")
        for key, store in (("facebook", facebook), ("instagram", instagram)):
            if r.get(key) and s not in store:
                store[s] = r[key]

    # SWITCHBOARD GUARD. `04 42 56 68 46` was found on 19 different companies'
    # pages and `04 42 07 88 15` on 5 — a franchise head office or the web
    # agency's own line in a shared footer, not any shop's number. Selling one
    # switchboard as 19 bakeries' direct line is the network-domain bug in
    # phone form. Keyed on SIREN, so a real multi-site company keeping one line
    # across its own établissements is untouched.
    by_number = defaultdict(set)
    siren_of = {b["siret"]: b["siren"] for b in base}
    for s, claims in phones.items():
        for _, p, _ in claims:
            by_number[p].add(siren_of.get(s, s))
    switchboards = {p for p, sirens in by_number.items() if len(sirens) > 2}
    if switchboards:
        for s in list(phones):
            phones[s] = [c for c in phones[s] if c[1] not in switchboards]
        log.info(f"switchboard guard: {len(switchboards)} number(s) claimed by "
                 f">2 companies dropped, e.g. {sorted(switchboards)[:3]}")

    n_blocked = 0
    rows = []
    for b in base:
        s = b["siret"]
        # An address proven undeliverable must not ship. Before agriculture's
        # migration 015 this filter did not exist and verification bought
        # nothing at all.
        usable = sorted([c for c in cand.get(s, []) if c[2] != "invalide"])
        n_blocked += len(cand.get(s, [])) - len(usable)
        best = usable[0] if usable else None
        ph = sorted(phones.get(s, []))
        best_ph = ph[0] if ph else None
        r = dict(b)
        r.update({
            "telephone": best_ph[1] if best_ph else "",
            "telephone_source": best_ph[2] if best_ph else "",
            "telephone_surtaxe": "oui" if best_ph and is_surtaxe(best_ph[1]) else "",
            # Only shown where there is no confirmed phone — otherwise it is
            # noise next to a better number.
            "telephone_piste": "" if best_ph else piste.get(s, ""),
            "email": best[1] if best else "",
            "email_statut": best[2] if best else "",
            "email_confiance": best[3] if best else "",
            "autres_emails": str(len(usable) - 1) if len(usable) > 1 else "",
            "site_web": website.get(s, ""),
            "facebook": facebook.get(s, ""),
            "instagram": instagram.get(s, ""),
            "linkedin": linkedin.get(s, ""),
            "source_contact": ", ".join(sorted(srcs.get(s, ()))),
        })
        rows.append(r)

    if args.only_reachable:
        before = len(rows)
        rows = [r for r in rows if r["telephone"] or r["email"]]
        log.info(f"--only-reachable: {before - len(rows)} rows without any contact dropped")

    xlsx = OUT_DIR / f"{BASENAME}.xlsx"
    write_xlsx(xlsx, rows)
    csv_path = OUT_DIR / f"{BASENAME}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        w.writerow([h for _, h in COLUMNS])
        for r in rows:
            w.writerow([cell(r, c) for c, _ in COLUMNS])

    n = len(rows)
    n_ph = sum(1 for r in rows if r["telephone"])
    n_em = sum(1 for r in rows if r["email"])
    n_web = sum(1 for r in rows if r["site_web"])
    n_soc = sum(1 for r in rows if r["facebook"] or r["instagram"])
    n_reach = sum(1 for r in rows if r["telephone"] or r["email"])
    n_surt = sum(1 for r in rows if r["telephone_surtaxe"])
    log.info("─" * 62)
    log.info(f"Rows                       {n:>6}")
    log.info(f"  with a phone             {n_ph:>6} ({n_ph/max(1,n):.1%})")
    log.info(f"  with an EMAIL            {n_em:>6} ({n_em/max(1,n):.1%})")
    log.info(f"  with a website           {n_web:>6}")
    log.info(f"  with facebook/instagram  {n_soc:>6}")
    log.info(f"  reachable (phone|email)  {n_reach:>6} ({n_reach/max(1,n):.1%})")
    log.info(f"  phone sources: {dict(Counter(r['telephone_source'] for r in rows if r['telephone']))}")
    log.info(f"  surtaxe phones (flagged, not dropped): {n_surt}")
    log.info(f"  snippet 'piste' phones (76% precise, own column): "
             f"{sum(1 for r in rows if r['telephone_piste'])}")
    log.info(f"  email verdicts: {dict(Counter(r['email_statut'] for r in rows if r['email']))}")
    log.info(f"  email confiance: {dict(Counter(r['email_confiance'] for r in rows if r['email']))}")
    log.info(f"  addresses withheld as proven-invalid: {n_blocked}")
    log.info(f"written -> {xlsx}")
    log.info(f"written -> {csv_path}")
    log.info("Next: python scripts/m2_s15_check_v3.py --strict")


if __name__ == "__main__":
    main()
