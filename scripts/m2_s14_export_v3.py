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
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scripts.m1_s8_export import ILLEGAL_XML            # noqa: E402
from m2lib_contact import (normalize_fr_phone, is_surtaxe,  # noqa: E402
                           is_third_party_email)
from m2_s8_websites import AGGREGATORS                  # noqa: E402
from m2lib_validate import (VERDICT_SHIPS, email_belongs_to,  # noqa: E402
                            site_confiance)


def host(url: str) -> str:
    return urllib.parse.urlparse(url or "").netloc.lower().replace("www.", "")


def usable_site(url: str) -> bool:
    """A directory, registry or social URL is never the shop's own site.
    Maps lets a shop register its Instagram as its website, and OSM tags
    sometimes carry a mappy POI link — V5's H18 gate caught 7 shipping."""
    low = (url or "").lower()
    return bool(low) and not any(a in low for a in AGGREGATORS)

CHECK_DIR = PROJECT_ROOT / "exports" / "boulangerie" / "checkpoints"
OUT_DIR   = PROJECT_ROOT / "exports" / "boulangerie"
BASENAME  = "boulangerie_13_v7"

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
    ("site_confiance",       "Site confiance"),
    ("page_contact",         "Page contact"),
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
# Two independent weak sources naming the same number. Ranked below every
# corroborated single source, above nothing — it only ever fills a gap.
CORROBORATED_RANK = 4

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

    # M2-S21's verdicts. Sam's ask: "valider le site web (boulangerie) ou
    # supprimer le site web si incorrect". Keyed on (siret, domain) because a
    # chain domain is right for one of our rows and wrong for the next six.
    verdicts, junk_domains, shop_of, contact_of, conf_of = {}, {}, {}, {}, {}
    for r in read("site_verdicts.csv"):
        key = (r["siret"], r["domain"])
        verdicts[key] = r["verdict"]
        if r["verdict"] in VERDICT_SHIPS:
            conf_of[key] = site_confiance(r["verdict"], r.get("ownership", ""))
            if r.get("shop_url"):
                shop_of[key] = r["shop_url"]
            if r.get("contact_url"):
                contact_of[key] = r["contact_url"]
        else:
            # A domain that is junk for EVERY row it touches is junk full
            # stop — that is what kills info@mapquest.com even when the
            # address arrives by a different route than the site did.
            junk_domains.setdefault(r["domain"], True)
    for (_, dom), v in verdicts.items():
        if v in VERDICT_SHIPS:
            junk_domains[dom] = False
    junk_domains = {d for d, bad in junk_domains.items() if bad}

    def site_ok(siret: str, url: str) -> bool:
        """Does this (siret, url) survive validation?

        Fail-CLOSED once the checkpoint exists: a pair nobody judged is a
        pair nobody proved, and V6's real defect was shipping 492 sites that
        had never been verified at all. With no checkpoint on disk the
        function is transparent, and the H19/H21 gates then fail the build
        rather than letting an unvalidated file ship quietly.
        """
        if not usable_site(url):
            return False
        if not verdicts:
            return True
        return verdicts.get((siret, host(url)), "") in VERDICT_SHIPS

    # siret -> [(rank, phone, source)] — every claim kept, best one exported.
    phones: dict = defaultdict(list)
    # EVERY untrusted claim is kept, not just the first. Keeping one per siret
    # would discard the second witness — which is precisely the agreement
    # signal the corroborator below needs.
    claims: dict = defaultdict(list)          # siret -> [(phone, source)]
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
            claims[siret].append((p, source))
            return
        rank = PHONE_RANK[source] + (100 if is_surtaxe(p) else 0)
        phones[siret].append((rank, p, source))

    for m in matched:
        s, src = m["siret"], m["source"]
        add_phone(s, m.get("phone", ""), src)
        if m.get("phone"):
            srcs[s].add(src)
        if m.get("website") and s not in website:
            low = m["website"].lower()
            if "instagram.com" in low:
                instagram.setdefault(s, m["website"])
            elif "facebook.com" in low:
                facebook.setdefault(s, m["website"])
            elif site_ok(s, m["website"]):
                website[s] = shop_of.get((s, host(m["website"])), m["website"])
                srcs[s].add(src)
        if m.get("facebook") and s not in facebook:
            facebook[s] = m["facebook"]
        if m.get("email"):
            e = m["email"].lower()
            v = verified.get(e, "non verifie")
            cand[s].append((RANK.get(("confirme", v), 9), e, v, "confirme", src))
            srcs[s].add(src)

    # SMTP-PROVEN generated addresses (m2_s10). These are the only guessed
    # addresses that ship, and only because a mail server accepted them; a
    # catch-all domain's 250 proves nothing and never reaches this file.
    for r in read("pattern_candidates.csv"):
        if r.get("status") != "valid":
            continue
        s, e = r["siret"], r["candidate"].lower()
        cand[s].append((RANK[("confirme", "valide")], e, "valide",
                        "pattern/verifie", "pattern"))
        srcs[s].add("pattern")

    n_third_party = n_junk_site_email = n_named_recovered = 0
    for r in site_emails:
        s, e = r["siret"], r["email"].lower()
        # Defence in depth: m2_s9 drops these at extraction, but checkpoints
        # written before that guard existed are still on disk. One shared
        # function, so the two call sites cannot drift apart.
        if is_third_party_email(e, r.get("domain", "")):
            n_third_party += 1
            continue
        # An address harvested from a page that is not this bakery's page is
        # not this bakery's address, however well-formed it looks. Two
        # independent tests, because the address and the site can arrive by
        # different routes: the page it came FROM, and the domain it lives ON
        # (contact@autour-de-moi.pro fails the second even if the first is
        # somehow clean).
        # The address's own domain being junk is fatal, always — that is
        # contact@autour-de-moi.pro, the aggregator's own mailbox.
        if e.partition("@")[2] in junk_domains:
            n_junk_site_email += 1
            continue
        if not site_ok(s, "https://" + r.get("domain", "")):
            # The PAGE failed validation. That condemns the site, and it
            # condemns an anonymous `contact@` found there — but not an
            # address that names this very business. V7 deleted 264
            # businesses' only e-mail on this rule, including
            # lapatisseriedesmarseillais@gmail.com found on
            # lapatisseriedesmarseillais.fr. The attribution of the PAGE was
            # wrong; the address was not. See email_belongs_to().
            if not email_belongs_to(e, r.get("raison_sociale", ""),
                                    r.get("enseigne", "")):
                n_junk_site_email += 1
                continue
            conf_named = True
        else:
            conf_named = False
        v = verified.get(e, "non verifie")
        # An address recovered by its name never claims `confirme`: what we
        # proved is whose NAME it carries, not that the page was theirs.
        conf = "faible" if conf_named else (r.get("confiance") or "faible")
        cand[s].append((RANK.get((conf, v), 9), e, v, conf, "site"))
        srcs[s].add("site")
        n_named_recovered += 1 if conf_named else 0
        # Sam asked for "les urls des pages contact". m2_s21 records the one it
        # reached at /contact, but plenty of small sites (the eatbu template in
        # his own example) put their details at a #contact anchor on the home
        # page. found_on is where the address ACTUALLY was, so it fills the gap
        # whenever it points somewhere more specific than the bare domain.
        fo = r.get("found_on", "")
        key = (s, r.get("domain", ""))
        if fo and key not in contact_of and urllib.parse.urlparse(fo).path.strip("/"):
            contact_of[key] = fo
        if r.get("domain") and s not in website and site_ok(s, "https://" + r["domain"]):
            website[s] = shop_of.get((s, r["domain"]), "https://" + r["domain"])

    n_junk_site_phone = 0
    for r in site_contacts:
        s = r["siret"]
        ok = site_ok(s, "https://" + r.get("domain", ""))
        # A directory PRINTS our SIRET, so m2_s9 scored several of them
        # `confirme` — which is rank 3 in PHONE_RANK, a dialled number. The
        # switchboard on a hygiene-inspection site is not this bakery's line.
        if not ok:
            if r.get("phone"):
                n_junk_site_phone += 1
        else:
            # site/faible is not in PHONE_RANK, so add_phone routes it to `piste`.
            src = "site/confirme" if r.get("confiance") == "confirme" else "site/faible"
            add_phone(s, r.get("phone", ""), src)
            if r.get("phone") and r.get("confiance") == "confirme":
                srcs[s].add("site")
        for key, store in (("facebook", facebook), ("instagram", instagram),
                           ("linkedin", linkedin)):
            if r.get(key) and s not in store:
                store[s] = r[key]
        if r.get("domain") and s not in website and ok:
            website[s] = shop_of.get((s, r["domain"]), "https://" + r["domain"])

    for r in discovered:
        s = r["siret"]
        # Snippet phones are geo-gated at collection but still measured at only
        # 76% agreement with Maps — they go to the `piste` column, never to
        # `Telephone`. See the module docstring.
        if r.get("phone") and r.get("snippet_geo_ok"):
            # When m2_s8 recorded WHICH result the phone came from (V5+), the
            # witness is that domain — so a m2_s16 snippet from the same page
            # correctly collapses to ONE witness instead of fake-corroborating.
            # Old rows without phone_domain stay the opaque "snippet" witness.
            src = f"serp/{r['phone_domain']}" if r.get("phone_domain") else "snippet"
            add_phone(s, r["phone"], src)           # -> piste, never dialled
        if r.get("website") and s not in website and site_ok(s, r["website"]):
            website[s] = shop_of.get((s, host(r["website"])), r["website"])
            srcs[s].add("recherche")
        for key, store in (("facebook", facebook), ("instagram", instagram)):
            if r.get(key) and s not in store:
                store[s] = r[key]

    # CORROBORATION. A single untrusted claim is a lead (snippet 76%,
    # site/faible 17%). But two INDEPENDENT sources naming the same number is
    # a different kind of evidence: for them to agree by chance, two unrelated
    # publishers would have to make the same mistake about the same shop. That
    # is the same logic that made OSM×Maps agreement (95.7%) the strongest
    # signal in this project — applied to the weak sources instead.
    # Independence is judged by SOURCE, not by row: two snippets from the same
    # directory are one page read twice, not two witnesses.
    for r in read("serp_snippets.csv"):
        p = normalize_fr_phone(r.get("phone", ""))
        if p:
            claims[r["siret"]].append((p, f"serp/{r.get('snippet_domain', '?')}"))

    n_corroborated = 0
    for s, cl in claims.items():
        by_number = defaultdict(set)
        for p, src in cl:
            by_number[p].add(src)
        for p, sources in by_number.items():
            if len(sources) >= 2:
                label = "corrobore(" + "+".join(sorted(sources)[:2]) + ")"
                phones[s].append((CORROBORATED_RANK + (100 if is_surtaxe(p) else 0),
                                  p, label))
                n_corroborated += 1
    if n_corroborated:
        log.info(f"corroboration: {n_corroborated} phone(s) promoted — two "
                 f"independent sources named the same number")

    # SWITCHBOARD GUARD. `04 42 56 68 46` was found on 19 different companies'
    # pages and `04 42 07 88 15` on 5 — a franchise head office or the web
    # agency's own line in a shared footer, not any shop's number. Selling one
    # switchboard as 19 bakeries' direct line is the network-domain bug in
    # phone form. Keyed on SIREN, so a real multi-site company keeping one line
    # across its own établissements is untouched.
    by_number = defaultdict(set)
    siren_of = {b["siret"]: b["siren"] for b in base}
    for s, phone_claims in phones.items():
        for _, p, _ in phone_claims:
            by_number[p].add(siren_of.get(s, s))
    # V5: count and filter the `piste` claims too — franceboulangerie.fr
    # prints its network line (09 86 23 49 09) in three different shops'
    # snippets, and without this the switchboard shipped as their piste.
    for s, cl in claims.items():
        for p, _ in cl:
            by_number[p].add(siren_of.get(s, s))
    switchboards = {p for p, sirens in by_number.items() if len(sirens) > 2}
    if switchboards:
        for s in list(phones):
            phones[s] = [c for c in phones[s] if c[1] not in switchboards]
        for s in list(claims):
            claims[s] = [c for c in claims[s] if c[0] not in switchboards]
        log.info(f"switchboard guard: {len(switchboards)} number(s) claimed by "
                 f">2 companies dropped, e.g. {sorted(switchboards)[:3]}")

    # SHARED-URL GUARD, the switchboard guard applied to websites. A shop page
    # that several of our companies claim identifies none of them: three
    # bakeries in Gardanne all matched the same Pétrin Ribeïrou boutique page,
    # and three more the same Eguilles site. m2_s7's rule for listings is that
    # ambiguity is REJECTION, not a coin toss, and the same holds here. Keyed
    # on SIREN, so one company's several établissements may share their site.
    siren_of_siret = {b["siret"]: b["siren"] for b in base}
    url_sirens: dict = defaultdict(set)
    for s, u in website.items():
        url_sirens[u.rstrip("/").lower()].add(siren_of_siret.get(s, s))
    ambiguous_urls = {u for u, sr in url_sirens.items() if len(sr) > 1}
    if ambiguous_urls:
        website = {s: u for s, u in website.items()
                   if u.rstrip("/").lower() not in ambiguous_urls}
        log.info(f"shared-URL guard: {len(ambiguous_urls)} URL(s) claimed by >1 "
                 f"company dropped, e.g. {sorted(ambiguous_urls)[:2]}")

    # A junk domain's own mailbox is never a prospect's address, whichever
    # route it arrived by. The site loop already refuses them; this catches
    # the ones that come from a Maps listing or a generated pattern instead —
    # info@mapquest.com reached V6 that way, and V7's first build too.
    n_junk_mail = 0
    for s in list(cand):
        keep = [c for c in cand[s] if c[1].partition("@")[2] not in junk_domains]
        n_junk_mail += len(cand[s]) - len(keep)
        cand[s] = keep

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
            "telephone_piste": ("" if best_ph or not claims[s]
                                else claims[s][0][0]),
            "email": best[1] if best else "",
            "email_statut": best[2] if best else "",
            "email_confiance": best[3] if best else "",
            "autres_emails": str(len(usable) - 1) if len(usable) > 1 else "",
            "site_web": website.get(s, ""),
            "site_confiance": conf_of.get((s, host(website.get(s, ""))), ""),
            "page_contact": contact_of.get((s, host(website.get(s, ""))), ""),
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
    log.info(f"  third-party addresses dropped (suppliers/aggregators): {n_third_party}")
    if verdicts:
        n_conf = Counter(r["site_confiance"] for r in rows if r["site_web"])
        n_shop = sum(1 for r in rows if r["site_web"] in set(shop_of.values()))
        log.info(f"  site validation (m2_s21): {len(verdicts)} (siret,domain) pairs "
                 f"judged, {len(junk_domains)} domains junk for every row")
        log.info(f"    site confiance: {dict(n_conf)}")
        log.info(f"    shop-specific pages shipped instead of a chain home: {n_shop}")
        log.info(f"    e-mails dropped (harvested from an unvalidated site): "
                 f"{n_junk_site_email} + {n_junk_mail} on a junk domain by another route")
        log.info(f"    e-mails KEPT because the address names the business "
                 f"(shipped `faible`): {n_named_recovered}")
        log.info(f"    phones dropped (same reason): {n_junk_site_phone}")
        log.info(f"  with a contact page URL   "
                 f"{sum(1 for r in rows if r['page_contact']):>6}")
    else:
        log.warning("site_verdicts.csv absent — sites ship UNVALIDATED. "
                    "Run scripts/m2_s21_validate_sites.py; H19/H21 will fail.")
    log.info(f"written -> {xlsx}")
    log.info(f"written -> {csv_path}")
    log.info("Next: python scripts/m2_s19_check_v5.py --version v7 --baseline v6 --strict")


if __name__ == "__main__":
    main()
