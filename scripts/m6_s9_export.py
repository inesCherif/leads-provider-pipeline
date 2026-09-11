"""
M6-S9 — Merge every witness into the full éleveurs file (OUR audit trail, all columns)
=====================================================================================
Input : checkpoints/operateurs_<dept>.csv   (m6_s2 — the registry population)
        checkpoints/matched_<dept>.csv      (m6_s8 — PJ / OSM / BAF / Agence Bio / provider / DB claims)
        checkpoints/provider_agri.csv       (m6_s19 — provider rows, for the unmatched ones)
        site_contacts.csv + site_verdicts.csv + search_hits.csv + verified_emails.csv
            read from BOTH trees: M6's own (exports/eleveurs/checkpoints) and the
            inherited M3AG one (keyed by SIRET, so a crawl done for a bio
            operator transfers to the same SIRET here for free)
Output: exports/eleveurs/eleveurs_<dept>_<version>.xlsx  and .csv

Column contract = the M3AG layout (m3ag_s12 gates it), then the M6 columns.
Provenance is always written (`source_*`), never implied.

Precedence (measured on M2/M3AG, kept):
  phone   owner-declared (agencebio) > osm > bienvenue_ferme > pagesjaunes >
          site/confirme > corroborated. A one-witness number (snippet, DB
          piste, PROVIDER FILE) ships in `telephone_piste`, never in
          `telephone_final`; two independent witnesses naming the same
          number make it `corrobore(a+b)`. A dialable number the provider
          file also carries is noted in `telephone_confirme_par`.
  e-mail  agencebio > crawled own site (confirme/probable) > bienvenue_ferme
          > osm > pagesjaunes > provider > site/non verifie > snippet.
          Anything verified `invalide` (SMTP, or the DB verdict) is withheld.
  site    agencebio > osm > pagesjaunes > bienvenue_ferme > site/confirme > crawl-valide

M6 extras:
  * provider rows that match NO registry établissement are APPENDED
    (`population_source = provider`, `statut_sirene = non retrouvé`): Maha's
    Probable tab may still dial them (M5 precedent). A provider row whose
    SIRET exists but is not in the population is dropped (closed, or not a
    livestock NAF) and counted.
  * `deja_envoye` = siret / telephone when the row is already in one of the
    four files Maha holds (m6_lib.SENT_FILES) — the call sheet drops it.

Usage:
    python scripts/m6_s9_export.py --departement 63 --version v1
"""

import argparse
import csv
import logging
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m1_s8_export import ILLEGAL_XML                       # noqa: E402
from m2lib_contact import normalize_fr_phone, is_surtaxe, FREE_MAIL   # noqa: E402
from m3ag_s12_check import MAIRIE_RE, EXTRA_FREE_MAIL                 # noqa: E402
from m6_lib import (CHECK_DIR, OUT_DIR, INHERITED_DIR, read_csv, name_tokens, root_domain,  # noqa: E402
                    is_aggregator, is_junk_witness, strong_tokens, JUNK_MAILBOX,
                    classify_type, type_from_label, phone_digits, load_sent)

# m3ag_s9.COLUMNS verbatim (the m6_s12 gate reads these), then the M6 columns.
M3AG_COLUMNS = ["raisonSociale", "siret", "gerant", "telephone", "telephoneCommerciale",
                "codeNAF", "siteWebs", "categories", "productions",
                "adresse", "codePostal", "ville", "lat", "lon",
                "places_phone", "places_website", "places_name", "error",
                "telephone_final", "website_final",
                "email", "numeroBio", "activites", "flag_hors_agri",
                "pj_phone", "pj_mobile", "pj_name", "pj_method",
                "source_telephone", "source_website",
                "email_final", "source_email", "email_statut", "emails_autres",
                "telephone_piste", "osm_phone", "osm_email", "osm_website",
                "baf_phone", "baf_email", "baf_website", "baf_contact",
                "site_confiance", "facebook", "instagram"]
M6_COLUMNS = ["type_elevage", "bio", "productions_bio", "siren", "denomination_legale", "enseigne",
              "forme_juridique", "nature_juridique", "date_creation", "tranche_effectif", "est_siege",
              "prenom", "nom", "fonction", "flag_public", "flag_animaux_compagnie",
              "procedure_collective", "statut_sirene", "population_source",
              "provider_phone", "provider_email", "provider_nom", "provider_fichier",
              "telephone_confirme_par", "deja_envoye"]
COLUMNS = M3AG_COLUMNS + M6_COLUMNS

# The M7 direct-sales directories (2026-09-11) are owner-declared listings,
# ranked with bienvenue-à-la-ferme, above Pages Jaunes (m7_s9 measures the
# pairwise agreement; acheteralasource 90 % of 10, producteur.direct 77 % of 13).
DIRECTORIES = ["jours_de_marche", "denosfermes63", "acheteralasource", "producteur_direct", "fermes_locales"]
PHONE_RANK = ["agencebio", "osm", "bienvenue_ferme"] + DIRECTORIES + ["pagesjaunes", "google_panel",
              "site/confirme", "corrobore"]
EMAIL_RANK = ["agencebio", "jours_de_marche", "producteur_direct", "fermes_locales", "bonfromager",
              "denosfermes63", "acheteralasource", "site/confirme", "site/probable", "bienvenue_ferme",
              "osm", "pagesjaunes", "provider", "site/non verifie", "snippet"]
SITE_RANK = ["agencebio", "osm", "producteur_direct", "acheteralasource", "fermes_locales", "jours_de_marche",
             "pagesjaunes", "bienvenue_ferme", "site/confirme", "crawl"]
DIALABLE = set(PHONE_RANK)
DB_VERDICT_FR = {"valid": "valide", "invalid": "invalide", "risky": "risque", "malformed": "invalide",
                 "valide": "valide", "invalide": "invalide", "risque": "risque"}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m6_s9")


def clean(v) -> str:
    return ILLEGAL_XML.sub("", str(v if v is not None else ""))


def nphone(p: str) -> str:
    return normalize_fr_phone(p or "") or ""


def rank(order: list, key: str) -> int:
    return order.index(key) if key in order else len(order)


def read_both(fname: str, dept: str, delim: str = ";") -> list[dict]:
    """Rows of `fname` from the inherited tree then M6's own, filtered on dept when the file carries it."""
    out = []
    for d in (INHERITED_DIR, CHECK_DIR):
        p = d / fname
        if p.exists():
            for r in read_csv(p, delim=delim):
                if r.get("dept") and r["dept"] != dept:
                    continue
                out.append(r)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departement", default="63")
    ap.add_argument("--version", default="v1")
    args = ap.parse_args()
    dept = args.departement

    ours_path = CHECK_DIR / f"operateurs_{dept}.csv"
    if not ours_path.exists():
        sys.exit(f"{ours_path} missing — run m6_s2_transform.py first.")
    with ours_path.open(encoding="utf-8-sig", newline="") as fh:
        ops = list(csv.DictReader(fh))
    pop_sirets = {op["siret"] for op in ops if op["siret"]}
    pop_sirens = Counter(op["siren"] for op in ops if op.get("siren"))

    # ---- sources, all keyed by row_id --------------------------------------
    matches = defaultdict(lambda: defaultdict(list))        # rid -> source -> [rows]
    matched_provider_ids = set()
    for m in read_csv(CHECK_DIR / f"matched_{dept}.csv"):
        matches[m["row_id"]][m["source"]].append(m)
        if m["source"] == "provider":
            matched_provider_ids.add(m["listing_id"])
    site_contacts = defaultdict(list)
    for r in read_both("site_contacts.csv", dept):
        site_contacts[r["row_id"]].append(r)
    last_verdict = {}
    for r in read_both("site_verdicts.csv", dept):
        last_verdict[(r["row_id"], r["domain"])] = r      # redo runs append: last wins
    site_verdicts = defaultdict(list)
    for r in last_verdict.values():
        if r["verdict"] == "valide":
            site_verdicts[r["row_id"]].append(r)
    hits = defaultdict(list)
    for r in read_both("search_hits.csv", dept):
        hits[r["row_id"]].append(r)
    verified = {}
    for r in read_both("verified_emails.csv", dept):          # M6's own verdicts win (read last)
        verified[r["email"].lower()] = r["verdict"]
    sent_sirets, sent_phones = load_sent()
    log.info(f"exclusion list from the files Maha holds: {len(sent_sirets)} SIRET, {len(sent_phones)} phones")

    rows = []
    stats = Counter()

    def build_row(op: dict, rid: str, population_source: str) -> dict:
        ms = matches.get(rid, {})
        toks = name_tokens(op["raisonSociale"], op.get("denomination_legale", ""), op.get("gerant", ""))
        provider = (ms.get("provider") or [{}])[0]
        provider_phone = nphone(provider.get("phone") or provider.get("mobile") or op.get("_provider_phone", ""))
        provider_email = (provider.get("email") or op.get("_provider_email", "")).lower().strip()
        provider_verified = (provider.get("verdict") or op.get("_provider_verdict", "")) in ("valid", "valide")

        # ---------------- phones: (phone, source) candidates ----------------
        cands = []          # dialable
        for src, mlist in ms.items():
            base = src.split("(")[0]
            if base in DIALABLE:
                for m in mlist:
                    for p in (m.get("phone"), m.get("mobile")):
                        if nphone(p) and not is_surtaxe(nphone(p)):
                            cands.append((nphone(p), src))
        for c in site_contacts.get(rid, []):
            if nphone(c.get("phone")) and c.get("confiance") == "confirme":
                cands.append((nphone(c["phone"]), "site/confirme"))
        # one-witness claims: snippet hosts, DB pistes, the provider file
        witnesses = defaultdict(set)
        for h in hits.get(rid, []):
            if is_junk_witness(h["host"]):
                continue
            for p in (h.get("phones") or "").split("|"):
                if nphone(p) and not is_surtaxe(nphone(p)):
                    witnesses[nphone(p)].add(root_domain(h["host"]))
        for src in ("piste", "site/probable", "snippet"):
            for m in ms.get(src, []):
                for p in (m.get("phone"), m.get("mobile")):
                    if nphone(p) and not is_surtaxe(nphone(p)):
                        witnesses[nphone(p)].add(f"db_{src.replace('/', '_')}")
        if provider_phone and not is_surtaxe(provider_phone):
            witnesses[provider_phone].add("fichier_fournisseur")
        listed = {p for p, _ in cands}
        pistes = []
        for p, ws in witnesses.items():
            if p in listed:
                continue
            if len(ws) >= 2:
                cands.append((p, f"corrobore({'+'.join(sorted(ws)[:3])})"))
            else:
                w = next(iter(ws))
                label = "fichier fournisseur" if w == "fichier_fournisseur" else w
                pistes.append((0 if w == "fichier_fournisseur" else 1, f"{p} ({label})"))
        pistes = [t for _, t in sorted(pistes)]
        cands.sort(key=lambda x: rank(PHONE_RANK, x[1].split("(")[0]))
        tel_final, tel_src = (cands[0] if cands else ("", ""))
        confirme_par = ""
        if tel_final and provider_phone == tel_final and "fichier_fournisseur" not in tel_src:
            confirme_par = "fichier fournisseur"
            stats["dialable phone also in the provider file"] += 1
        elif tel_final and witnesses.get(tel_final):
            confirme_par = "+".join(sorted(w for w in witnesses[tel_final] if w != "fichier_fournisseur")[:2])

        # ---------------- e-mails --------------------------------------------
        ecands = []         # (email, source, db_verdict)
        for src, mlist in ms.items():
            base = src.split("(")[0]
            for m in mlist:
                e = (m.get("email") or "").lower().strip()
                if not e:
                    continue
                lbl = "provider" if base == "provider" else base
                ecands.append((e, lbl, DB_VERDICT_FR.get((m.get("verdict") or "").lower(), "")))
        if provider_email and "@" in provider_email and not ms.get("provider"):
            ecands.append((provider_email, "provider", "valide" if provider_verified else ""))

        def name_in(*texts) -> bool:
            flat = " ".join(texts).lower().replace("-", "").replace(".", "").replace("_", "")
            return any(t.lower() in flat for t in toks if len(t) >= 4)

        for c in site_contacts.get(rid, []):
            e = (c.get("email") or "").lower().strip()
            if e:
                conf = c.get("confiance") or "non verifie"
                if conf == "probable" and not name_in(c["domain"], e):
                    stats["e-mail withheld: probable site, name not in domain/address"] += 1
                    continue
                ecands.append((e, f"site/{conf}", ""))
        for h in hits.get(rid, []):
            for e in (h.get("emails") or "").split("|"):
                if e.strip():
                    ecands.append((e.strip().lower(), "snippet", ""))
        seen_e, uniq = set(), []
        for e, s, dbv in sorted(ecands, key=lambda x: rank(EMAIL_RANK, x[1])):
            if e in seen_e:
                continue
            if dbv:
                verified.setdefault(e, dbv)
            if set(re.split(r"[._\-+]", e.partition("@")[0])) & JUNK_MAILBOX or e.partition("@")[0] in JUNK_MAILBOX:
                stats["e-mail withheld: junk mailbox (dpo, lorem.ipsum…)"] += 1
                continue
            if verified.get(e) == "invalide" or dbv == "invalide":
                stats["e-mail withheld: invalide"] += 1
                continue
            if not re.match(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$", e):
                stats["e-mail withheld: malformed"] += 1
                continue
            if is_aggregator(e.partition("@")[2]) and e.partition("@")[2] not in ("gmail.com",):
                stats["e-mail withheld: aggregator domain"] += 1
                continue
            if MAIRIE_RE.search(e.partition("@")[2]):
                stats["e-mail withheld: mairie / collectivité mailbox"] += 1
                continue
            seen_e.add(e)
            uniq.append((e, s))
        strong = strong_tokens(toks)

        def named(e):
            flat = e.replace(".", "").replace("-", "").replace("_", "").replace("@", "")
            return any(t.lower() in flat for t in strong)
        before = len(uniq)
        uniq = [(e, s) for e, s in uniq if s not in ("site/non verifie", "snippet") or named(e)]
        stats["e-mail withheld: one witness, no farm name in address"] += before - len(uniq)
        uniq.sort(key=lambda x: (rank(EMAIL_RANK, x[1]), 0 if verified.get(x[0]) == "valide" else 1,
                                 0 if named(x[0]) else 1))
        email_final, email_src = (uniq[0] if uniq else ("", ""))
        email_statut = verified.get(email_final, "") if email_final else ""
        if email_final and email_src == "agencebio" and not email_statut:
            email_statut = "declare"
        emails_autres = "|".join(e for e, _ in uniq[1:4])

        # ---------------- sites --------------------------------------------
        scands = []
        for src, mlist in ms.items():
            base = src.split("(")[0]
            if base in SITE_RANK:
                for m in mlist:
                    if m.get("website") and not is_aggregator(m["website"]):
                        scands.append((m["website"], base))
        # a `valide` crawl verdict counts whatever listing brought the domain
        # (a later re-crawl of the same domain from a directory listing used
        # to overwrite the search verdict and lose the site - V3 2026-09-11)
        for v in site_verdicts.get(rid, []):
            if v["own"] in ("cp+nom", "nom_domaine", "cp") and not name_in(v["domain"]):
                stats["site withheld: probable, name not in domain"] += 1
                continue
            scands.append((f"https://{v['domain']}", "crawl"))
        scands.sort(key=lambda x: rank(SITE_RANK, x[1]))
        site_final, site_src = (scands[0] if scands else ("", ""))
        conf = ""
        for c in site_contacts.get(rid, []):
            if site_final and c["domain"] in site_final:
                conf = c.get("confiance") or conf
        for v in site_verdicts.get(rid, []):
            if site_final and v["domain"] in site_final and not conf:
                conf = "probable"

        # ---------------- socials --------------------------------------------
        fb = ig = ""
        for c in site_contacts.get(rid, []):
            fb = fb or c.get("facebook", "")
            ig = ig or c.get("instagram", "")
        for h in hits.get(rid, []):
            if h.get("kind") == "social" and h.get("geo") == "cp":
                if "facebook.com" in h["host"] and not fb:
                    fb = h["url"]
                if "instagram.com" in h["host"] and not ig:
                    ig = h["url"]

        pj = (ms.get("pagesjaunes") or [{}])[0]
        osm = (ms.get("osm") or [{}])[0]
        baf = (ms.get("bienvenue_ferme") or [{}])[0]
        ab = (ms.get("agencebio") or [{}])[0]
        numero_bio = ab.get("listing_id", "")[2:] if ab.get("listing_id", "").startswith("NB") else ""
        if not numero_bio:
            for mlist in ms.values():
                for m in mlist:
                    mm = re.search(r"numero_bio=(\d+)", m.get("detail") or "")
                    if mm:
                        numero_bio = mm.group(1)
        prod_bio = ""
        mm = re.search(r"productions=([^;]*)", ab.get("detail") or "")
        if mm:
            prod_bio = mm.group(1)

        deja = ""
        if op.get("siret") and op["siret"] in sent_sirets:
            deja = "siret"
        elif any(phone_digits(p) in sent_phones for p in (tel_final, provider_phone) if p):
            deja = "telephone"

        row = {c: "" for c in COLUMNS}
        for c in ("raisonSociale", "siret", "gerant", "codeNAF", "categories", "productions",
                  "adresse", "codePostal", "ville", "lat", "lon", "activites", "flag_hors_agri",
                  "siren", "denomination_legale", "enseigne", "forme_juridique", "nature_juridique",
                  "date_creation", "tranche_effectif", "est_siege", "prenom", "nom", "fonction",
                  "flag_public", "flag_animaux_compagnie", "procedure_collective", "type_elevage"):
            row[c] = clean(op.get(c, ""))
        row.update({
            "telephone": clean(ab.get("phone", "")), "telephoneCommerciale": clean(ab.get("mobile", "")),
            "siteWebs": clean(ab.get("website", "")), "email": clean(ab.get("email", "")),
            "numeroBio": numero_bio, "bio": "1" if numero_bio else "", "productions_bio": clean(prod_bio),
            "telephone_final": tel_final, "website_final": clean(site_final),
            "pj_phone": clean(pj.get("phone", "")), "pj_mobile": clean(pj.get("mobile", "")),
            "pj_name": clean(pj.get("listing_name", "")), "pj_method": clean(pj.get("method", "")),
            "source_telephone": tel_src, "source_website": site_src,
            "email_final": email_final, "source_email": email_src,
            "email_statut": email_statut, "emails_autres": emails_autres,
            "telephone_piste": " | ".join(pistes[:3]),
            "osm_phone": clean(osm.get("phone", "")), "osm_email": clean(osm.get("email", "")),
            "osm_website": clean(osm.get("website", "")),
            "baf_phone": clean(baf.get("phone", "")), "baf_email": clean(baf.get("email", "")),
            "baf_website": clean(baf.get("website", "")), "baf_contact": clean(baf.get("listing_name", "")),
            "site_confiance": conf, "facebook": clean(fb), "instagram": clean(ig),
            "statut_sirene": op.get("_statut_sirene") or ("actif (liquidateur nommé)" if op.get("procedure_collective") else "actif"),
            "population_source": population_source,
            "provider_phone": provider_phone, "provider_email": provider_email if "@" in provider_email else "",
            "provider_nom": clean(provider.get("listing_name") or op.get("_provider_nom", "")),
            "provider_fichier": clean(re.search(r"file=([^;]*)", provider.get("detail") or "").group(1)
                                      if provider.get("detail") and "file=" in provider["detail"] else op.get("_provider_fichier", "")),
            "telephone_confirme_par": confirme_par, "deja_envoye": deja,
        })
        row["_uniq"] = uniq
        return row

    for op in ops:
        rid = op["siret"] or f"X{op.get('siren', '')}"
        rows.append(build_row(op, rid, "registre"))

    # ---- provider rows that matched nothing: appended, Probable at best -----
    prov_path = CHECK_DIR / "provider_agri.csv"
    if prov_path.exists():
        for pr in read_csv(prov_path):
            if pr["dept"] != dept or pr["listing_id"] in matched_provider_ids:
                continue
            if len(pr["siret"]) == 14 and pr["siret"] not in pop_sirets:
                stats["provider dropped: SIRET not in the registry population (closed / other NAF)"] += 1
                continue
            if len(pr["siren"]) == 9 and pop_sirens.get(pr["siren"], 0) > 1:
                stats["provider dropped: SIREN ambiguous in the population (several établissements)"] += 1
                continue
            if not (pr["phone"] or pr["mobile"] or pr["email"]):
                stats["provider dropped: no contact data"] += 1
                continue
            op = {
                "raisonSociale": pr["name"], "siret": pr["siret"] if len(pr["siret"]) == 14 else "",
                "gerant": pr["dirigeant"], "codeNAF": pr["naf"], "categories": pr["label"],
                "productions": classify_type(pr["naf"]) if pr["naf"] else type_from_label(pr["label"]),
                "type_elevage": classify_type(pr["naf"]) if pr["naf"] else type_from_label(pr["label"]),
                "adresse": pr["address"], "codePostal": pr["postcode"], "ville": pr["city"],
                "lat": "", "lon": "", "activites": "", "flag_hors_agri": "0",
                "siren": pr["siren"], "denomination_legale": pr["name"], "enseigne": "",
                "forme_juridique": "", "nature_juridique": "", "date_creation": "", "tranche_effectif": "",
                "est_siege": "", "prenom": "", "nom": "", "fonction": "", "flag_public": "0",
                "flag_animaux_compagnie": "0", "procedure_collective": "Liquidation" if pr["in_liquidation"] else "",
                "_statut_sirene": "non retrouvé",
                "_provider_phone": pr["phone"] or pr["mobile"], "_provider_email": pr["email"],
                "_provider_verdict": "valid" if pr["email_verified"] == "1" else "",
                "_provider_nom": pr["dirigeant"], "_provider_fichier": pr["source_file"],
            }
            rows.append(build_row(op, pr["listing_id"], "provider"))
            stats["provider rows appended (SIRENE non retrouvé)"] += 1

    # ---- a corporate domain on > 2 farms is a third party (an advisor, a
    # cooperative, a breeding service), never the farms' own mailbox (H26) ----
    free = FREE_MAIL | EXTRA_FREE_MAIL
    dom_rows = Counter(r["email_final"].rpartition("@")[2] for r in rows
                       if r["email_final"] and r["email_final"].rpartition("@")[2] not in free)
    shared = {d for d, n in dom_rows.items() if n > 2}
    for r in rows:
        if r["email_final"] and r["email_final"].rpartition("@")[2] in shared:
            alt = [(e, s) for e, s in r["_uniq"] if e.rpartition("@")[2] not in shared]
            stats[f"e-mail withheld: domain shared by > 2 farms ({', '.join(sorted(shared)[:3])})"] += 1
            e, s = alt[0] if alt else ("", "")
            r["email_final"], r["source_email"] = e, s
            r["email_statut"] = (verified.get(e, "") or ("declare" if s == "agencebio" else "")) if e else ""
            r["emails_autres"] = "|".join(x for x, _ in alt[1:4])
    for r in rows:
        r.pop("_uniq", None)
    # ---- a discovered website on two different SIRENs is a shared / network
    # site, not either farm's own (H9); two établissements of one legal unit
    # may share it; hosted platforms are keyed on the full host (m3ag_lib.site_key) ----
    from m3ag_lib import site_key
    site_sirens = defaultdict(set)
    for r in rows:
        if r["website_final"] and r["source_website"] != "agencebio":
            site_sirens[site_key(r["website_final"])].add(r["siren"] or r["siret"] or r["raisonSociale"])
    for r in rows:
        if r["website_final"] and r["source_website"] != "agencebio":
            d = site_key(r["website_final"])
            if len(site_sirens[d]) > 1:
                stats[f"site withheld: shared by {len(site_sirens[d])} legal units ({d})"] += 1
                r["website_final"], r["source_website"], r["site_confiance"] = "", "", ""

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / f"eleveurs_{dept}_{args.version}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)

    from openpyxl import Workbook
    xlsx_path = OUT_DIR / f"eleveurs_{dept}_{args.version}.xlsx"
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Eleveurs")
    ws.append(COLUMNS)
    for r in rows:
        ws.append([str(r[c]) if c in ("siret", "codePostal", "siren") and r[c] else r[c] for c in COLUMNS])
    wb.save(xlsx_path)

    n = len(rows)
    reg = [r for r in rows if r["population_source"] == "registre"]
    tel = sum(1 for r in rows if r["telephone_final"])
    mail = sum(1 for r in rows if r["email_final"])
    site = sum(1 for r in rows if r["website_final"])
    prov_only = sum(1 for r in rows if not r["telephone_final"] and r["provider_phone"])
    reach = sum(1 for r in rows if r["telephone_final"] or r["email_final"])
    log.info("─" * 62)
    log.info(f"[{dept}] {n} rows ({len(reg)} registry + {n - len(reg)} provider-only) -> {xlsx_path.name} + .csv")
    log.info(f"  telephone_final   {tel:>5}  ({tel/n:.1%})  by source: "
             f"{dict(Counter(r['source_telephone'].split('(')[0] for r in rows if r['telephone_final']))}")
    log.info(f"  provider-only tel {prov_only:>5}  (Probable at best)   confirmed by provider: "
             f"{sum(1 for r in rows if r['telephone_confirme_par'] == 'fichier fournisseur')}")
    log.info(f"  email_final       {mail:>5}  ({mail/n:.1%})  by source: "
             f"{dict(Counter(r['source_email'] for r in rows if r['email_final']))}  statut: "
             f"{dict(Counter(r['email_statut'] or 'non verifie' for r in rows if r['email_final']))}")
    log.info(f"  website_final     {site:>5}  ({site/n:.1%})")
    log.info(f"  joignables        {reach:>5}  ({reach/n:.1%})  (dialable phone OR e-mail)")
    log.info(f"  bio {sum(1 for r in rows if r['bio'])}   deja_envoye {dict(Counter(r['deja_envoye'] for r in rows if r['deja_envoye']))}   "
             f"types {dict(Counter(r['type_elevage'] for r in rows).most_common())}")
    for k, v in stats.most_common():
        log.info(f"  {k:<70} {v}")


if __name__ == "__main__":
    main()
