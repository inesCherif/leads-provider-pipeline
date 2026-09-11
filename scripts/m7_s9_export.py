"""
M7-S9 — Merge every witness into the full producteurs file (Sam's file: audit trail, all columns)
================================================================================================
Input : checkpoints/operateurs_<dept>.csv   (m7_s2 — the registry population, agriculture minus livestock)
        checkpoints/matched_<dept>.csv      (m7_s8 — directories + PJ / OSM / BAF / Agence Bio)
        checkpoints/unmatched_<dept>.csv    (m7_s8 — listings with a contact and no SIRET → sheet "Sans SIRET")
        site_contacts.csv + site_verdicts.csv + search_hits.csv + verified_emails.csv
            read from THREE trees (M3AG, M6, M7), keyed by SIRET, so a crawl or a
            verdict done for the same SIRET in an earlier sector transfers for free
        --with-eleveurs : the M6 full export (exports/eleveurs/eleveurs_<dept>_<ver>.csv)
            appended with Sous-segment = éleveur|<type>, MINUS porcins (01.46Z) and pet
            trades — so Sam's file per département is the whole agriculture family,
            one row per SIRET (Ines 2026-09-11).
Output: exports/producteurs/producteurs_<dept>_<version>.xlsx (sheets Producteurs + Sans SIRET) and .csv

Column contract = the M3AG layout (gated by m7_s12), then the M7 columns:
  sous_segment (from the NAF, then every matched directory's own category — '|'-joined),
  descriptif_activite + source_contexte (the activity CONTEXT Sam asked for: the
  directory's description, else the Agence Bio productions, else the NAF label),
  contact_directory (the person named by a directory when the registry names nobody),
  statut_sirene, population_source, telephone_confirme_par, deja_envoye.

Precedence (measured on M2 / M3AG / M6, kept; the directories are OWNER-DECLARED
listings — the producer registers himself — so they rank with bienvenue-à-la-ferme,
above Pages Jaunes; the agreement between witnesses is printed at the end):
  phone   agencebio > osm > bienvenue_ferme > jours_de_marche > denosfermes63 >
          acheteralasource > producteur_direct > fermes_locales > pagesjaunes >
          site/confirme > corrobore. One-witness snippets ship in `telephone_piste`.
  e-mail  agencebio > jours_de_marche > producteur_direct > fermes_locales > bonfromager >
          denosfermes63 > acheteralasource > own site (confirme/probable) > bienvenue_ferme
          > osm > pagesjaunes > site/non verifie > snippet. `invalide` never ships.
  site    agencebio > osm > producteur_direct > acheteralasource > fermes_locales >
          jours_de_marche > pagesjaunes > bienvenue_ferme > site/confirme > crawl-valide

The principle is asserted here too: a row carrying an EXCLUDED_NAF or an excluded
tag aborts the export.

Usage:
    python scripts/m7_s9_export.py --departement 63 --version v1 --with-eleveurs --eleveurs-version v2
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
from m3ag_lib import site_key                                          # noqa: E402
from m7_lib import (CHECK_DIR, OUT_DIR, INHERITED_AGRI, INHERITED_ELEVEURS, read_csv,  # noqa: E402
                    name_tokens, root_domain, is_aggregator, is_junk_witness, strong_tokens,
                    JUNK_MAILBOX, phone_digits, load_sent, EXCLUDED_NAF, EXCLUDED_RE,
                    tags_from_category, merge_tags, listing_excluded, NAF_LABELS)

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
M7_COLUMNS = ["sous_segment", "descriptif_activite", "source_contexte", "contact_directory",
              "bio", "productions_bio", "siren", "denomination_legale", "enseigne",
              "forme_juridique", "nature_juridique", "date_creation", "tranche_effectif", "est_siege",
              "prenom", "nom", "fonction", "flag_public", "procedure_collective",
              "provider_phone", "provider_email", "provider_nom", "provider_fichier",
              "statut_sirene", "population_source", "telephone_confirme_par", "deja_envoye"]
COLUMNS = M3AG_COLUMNS + M7_COLUMNS
SANS_SIRET_COLUMNS = ["name", "contact", "phone", "mobile", "email", "email_statut", "website",
                      "address", "postcode", "city", "sous_segment", "description", "source", "url"]

DIRECTORIES = ["jours_de_marche", "denosfermes63", "acheteralasource", "producteur_direct",
               "fermes_locales", "bonfromager"]
PHONE_RANK = ["agencebio", "osm", "bienvenue_ferme", "jours_de_marche", "denosfermes63",
              "acheteralasource", "producteur_direct", "fermes_locales", "pagesjaunes",
              "google_panel", "site/confirme", "corrobore"]
EMAIL_RANK = ["agencebio", "jours_de_marche", "producteur_direct", "fermes_locales", "bonfromager",
              "denosfermes63", "acheteralasource", "site/confirme", "site/probable", "bienvenue_ferme",
              "osm", "pagesjaunes", "provider", "site/non verifie", "snippet"]
SITE_RANK = ["agencebio", "osm", "producteur_direct", "acheteralasource", "fermes_locales",
             "jours_de_marche", "pagesjaunes", "bienvenue_ferme", "site/confirme", "crawl"]
CONTEXT_RANK = ["producteur_direct", "acheteralasource", "fermes_locales", "jours_de_marche",
                "denosfermes63", "bonfromager"]
DIALABLE = set(PHONE_RANK)          # the provider file is NOT here: 73 % measured, a witness only
DB_VERDICT_FR = {"valid": "valide", "invalid": "invalide", "risky": "risque", "malformed": "invalide",
                 "valide": "valide", "invalide": "invalide", "risque": "risque"}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m7_s9")


def clean(v) -> str:
    return ILLEGAL_XML.sub("", str(v if v is not None else ""))


def nphone(p: str) -> str:
    return normalize_fr_phone(p or "") or ""


def rank(order: list, key: str) -> int:
    return order.index(key) if key in order else len(order)


def read_all(fname: str, dept: str, delim: str = ";") -> list[dict]:
    """Rows of `fname` from the M3AG tree, then M6, then M7's own (last wins on conflicts)."""
    out = []
    for d in (INHERITED_AGRI, INHERITED_ELEVEURS, CHECK_DIR):
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
    ap.add_argument("--with-eleveurs", action="store_true", help="append the M6 full export (minus porcins / pets)")
    ap.add_argument("--eleveurs-version", default="v2")
    args = ap.parse_args()
    dept = args.departement

    ours_path = CHECK_DIR / f"operateurs_{dept}.csv"
    if not ours_path.exists():
        sys.exit(f"{ours_path} missing — run m7_s2_transform.py first.")
    with ours_path.open(encoding="utf-8-sig", newline="") as fh:
        ops = list(csv.DictReader(fh))

    matches = defaultdict(lambda: defaultdict(list))        # rid -> source -> [rows]
    for m in read_csv(CHECK_DIR / f"matched_{dept}.csv"):
        matches[m["row_id"]][m["source"]].append(m)
    site_contacts = defaultdict(list)
    for r in read_all("site_contacts.csv", dept):
        site_contacts[r["row_id"]].append(r)
    last_verdict = {}
    for r in read_all("site_verdicts.csv", dept):
        last_verdict[(r["row_id"], r["domain"])] = r
    site_verdicts = defaultdict(list)
    for r in last_verdict.values():
        if r["verdict"] == "valide":
            site_verdicts[r["row_id"]].append(r)
    hits = defaultdict(list)
    for r in read_all("search_hits.csv", dept):
        hits[r["row_id"]].append(r)
    verified = {}
    for r in read_all("verified_emails.csv", dept):
        verified[r["email"].lower()] = r["verdict"]
    sent_sirets, sent_phones = load_sent()
    log.info(f"exclusion list from the files Maha holds: {len(sent_sirets)} SIRET, {len(sent_phones)} phones")

    rows = []
    stats = Counter()
    agreement = Counter()          # (source, 'agree'|'disagree') against another witness on the same row

    def build_row(op: dict, rid: str) -> dict:
        ms = matches.get(rid, {})
        toks = name_tokens(op["raisonSociale"], op.get("denomination_legale", ""), op.get("gerant", ""))
        # the client's own file (m7_s19): a witness, never dialled alone on this file
        provider = next((m for m in ms.get("provider", []) if nphone(m.get("phone") or m.get("mobile"))),
                        (ms.get("provider") or [{}])[0])
        provider_phone = nphone(provider.get("phone") or provider.get("mobile") or "")
        provider_email = next(((m.get("email") or "").lower().strip() for m in ms.get("provider", []) if m.get("email")), "")
        provider_verified = any("email_verified=1" in (m.get("detail") or "") and (m.get("email") or "").lower().strip() == provider_email
                                for m in ms.get("provider", []))
        db_verdict = {}
        for mlist in ms.values():
            for m in mlist:
                if m.get("email") and m.get("verdict"):
                    db_verdict[m["email"].lower().strip()] = DB_VERDICT_FR.get(m["verdict"].lower(), "")
        if provider_email and provider_verified:
            db_verdict[provider_email] = "valide"

        # ---------------- phones ----------------
        cands = []
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
        # pairwise agreement measurement between sources naming a phone on this row
        by_src = defaultdict(set)
        for p, s in cands:
            by_src[s.split("(")[0]].add(p)
        for s, ps in by_src.items():
            others = set().union(*(v for k, v in by_src.items() if k != s)) if len(by_src) > 1 else set()
            if others:
                agreement[(s, "agree" if ps & others else "disagree")] += 1
        witnesses = defaultdict(set)
        for h in hits.get(rid, []):
            if is_junk_witness(h["host"]):
                continue
            for p in (h.get("phones") or "").split("|"):
                if nphone(p) and not is_surtaxe(nphone(p)):
                    witnesses[nphone(p)].add(root_domain(h["host"]))
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
                pistes.append((0 if w == "fichier_fournisseur" else 1,
                               f"{p} ({'fichier fournisseur' if w == 'fichier_fournisseur' else w})"))
        pistes = [t for _, t in sorted(pistes)]
        cands.sort(key=lambda x: rank(PHONE_RANK, x[1].split("(")[0]))
        tel_final, tel_src = (cands[0] if cands else ("", ""))
        confirme_par = ""
        if tel_final:
            agreeing = sorted({s.split("(")[0] for p, s in cands if p == tel_final and s != tel_src})
            if agreeing:
                confirme_par = "+".join(agreeing[:2])
            elif provider_phone == tel_final and "fichier_fournisseur" not in tel_src:
                confirme_par = "fichier fournisseur"
            elif witnesses.get(tel_final):
                confirme_par = "+".join(sorted(w for w in witnesses[tel_final] if w != "fichier_fournisseur")[:2])
            if provider_phone == tel_final:
                stats["dialable phone also in the provider file"] += 1
            elif provider_phone:
                stats["dialable phone differs from the provider file"] += 1

        # ---------------- e-mails ----------------
        ecands = []
        for src, mlist in ms.items():
            base = src.split("(")[0]
            for m in mlist:
                e = (m.get("email") or "").lower().strip()
                if e:
                    ecands.append((e, base))

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
                ecands.append((e, f"site/{conf}"))
        for h in hits.get(rid, []):
            for e in (h.get("emails") or "").split("|"):
                if e.strip():
                    ecands.append((e.strip().lower(), "snippet"))
        seen_e, uniq = set(), []
        for e, s in sorted(ecands, key=lambda x: rank(EMAIL_RANK, x[1])):
            if e in seen_e:
                continue
            if set(re.split(r"[._\-+]", e.partition("@")[0])) & JUNK_MAILBOX or e.partition("@")[0] in JUNK_MAILBOX:
                stats["e-mail withheld: junk mailbox"] += 1
                continue
            if verified.get(e) == "invalide" or db_verdict.get(e) == "invalide":
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
        email_statut = (verified.get(email_final, "") or db_verdict.get(email_final, "")) if email_final else ""
        if email_final and not email_statut and email_src in ("agencebio",) + tuple(DIRECTORIES):
            email_statut = "declare"
        emails_autres = "|".join(e for e, _ in uniq[1:4])

        # ---------------- sites ----------------
        scands = []
        for src, mlist in ms.items():
            base = src.split("(")[0]
            if base in SITE_RANK:
                for m in mlist:
                    if m.get("website") and not is_aggregator(m["website"]):
                        scands.append((m["website"], base))
        # a `valide` crawl verdict counts whatever listing brought the domain
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

        # ---------------- context: sous-segment, description, contact person ----------------
        seg = op.get("sous_segment", "")
        desc, desc_src, contact = "", "", ""
        for src in sorted(ms, key=lambda s: rank(CONTEXT_RANK, s.split("(")[0])):
            for m in ms[src]:
                seg = merge_tags(seg, tags_from_category(f"{m.get('categorie','')} {m.get('productions','')}"))
                if not desc and (m.get("description") or "").strip() and src in CONTEXT_RANK:
                    desc, desc_src = clean(m["description"]).strip(), src
                if not contact and (m.get("contact_name") or "").strip():
                    contact = clean(m["contact_name"]).strip()
        ab = (ms.get("agencebio") or [{}])[0]
        prod_bio = ""
        mm = re.search(r"productions=([^;]*)", ab.get("detail") or "")
        if mm:
            prod_bio = mm.group(1)
        if not desc and prod_bio:
            desc, desc_src = prod_bio, "agencebio"
        if not desc:
            desc, desc_src = NAF_LABELS.get(op.get("codeNAF", ""), op.get("productions", "")), "registre (NAF)"
        if EXCLUDED_RE.search(seg) or op.get("codeNAF", "") in EXCLUDED_NAF:
            sys.exit(f"PRINCIPLE VIOLATED on {rid} {op['raisonSociale']}: naf={op.get('codeNAF')} seg={seg}")

        pj = (ms.get("pagesjaunes") or [{}])[0]
        osm = (ms.get("osm") or [{}])[0]
        baf = (ms.get("bienvenue_ferme") or [{}])[0]
        numero_bio = ab.get("listing_id", "")[2:] if ab.get("listing_id", "").startswith("NB") else ""
        deja = ""
        if op.get("siret") and op["siret"] in sent_sirets:
            deja = "siret"
        elif tel_final and phone_digits(tel_final) in sent_phones:
            deja = "telephone"

        row = {c: "" for c in COLUMNS}
        for c in ("raisonSociale", "siret", "gerant", "codeNAF", "categories", "productions",
                  "adresse", "codePostal", "ville", "lat", "lon", "activites", "flag_hors_agri",
                  "siren", "denomination_legale", "enseigne", "forme_juridique", "nature_juridique",
                  "date_creation", "tranche_effectif", "est_siege", "prenom", "nom", "fonction",
                  "flag_public", "procedure_collective"):
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
            "sous_segment": seg, "descriptif_activite": desc[:1500], "source_contexte": desc_src,
            "contact_directory": contact,
            "provider_phone": provider_phone, "provider_email": provider_email,
            "provider_nom": clean(provider.get("contact_name", "")),
            "provider_fichier": clean(re.search(r"file=([^;]*)", provider.get("detail") or "").group(1)
                                      if provider.get("detail") and "file=" in provider["detail"] else ""),
            "statut_sirene": "actif (liquidateur nommé)" if op.get("procedure_collective") else "actif",
            "population_source": "registre", "telephone_confirme_par": confirme_par, "deja_envoye": deja,
        })
        row["_uniq"] = uniq
        row["_dbv"] = db_verdict
        return row

    for op in ops:
        rows.append(build_row(op, op["siret"] or f"X{op.get('siren', '')}"))

    # ---- shared corporate domain on > 2 farms = a third party (H26) ----
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
            r["email_statut"] = (verified.get(e, "") or r["_dbv"].get(e, "")
                                 or ("declare" if s in ("agencebio",) + tuple(DIRECTORIES) else "")) if e else ""
            r["emails_autres"] = "|".join(x for x, _ in alt[1:4])
    for r in rows:
        r.pop("_uniq", None)
        r.pop("_dbv", None)
    # ---- a discovered website on two different SIRENs is a shared / network
    # site, not either farm's own (H9); two établissements of ONE legal unit may
    # share it. Applied over the WHOLE file, so it runs again after the
    # éleveurs rows are appended (a producteur and an éleveur shared
    # coeur-de-fermier.com in the first V1 build) ----
    def withhold_shared_sites() -> None:
        site_sirens = defaultdict(set)
        for r in rows:
            if r["website_final"] and r["source_website"] != "agencebio":
                site_sirens[site_key(r["website_final"])].add(r["siren"] or r["siret"])
        for r in rows:
            if r["website_final"] and r["source_website"] != "agencebio":
                d = site_key(r["website_final"])
                if len(site_sirens[d]) > 1:
                    stats[f"site withheld: shared by {len(site_sirens[d])} legal units ({d})"] += 1
                    r["website_final"], r["source_website"], r["site_confiance"] = "", "", ""
    withhold_shared_sites()
    n_reg = len(rows)

    # ---- the éleveurs (M6 full export), appended minus porcins / pets ----
    if args.with_eleveurs:
        p = PROJECT_ROOT / "exports" / "eleveurs" / f"eleveurs_{dept}_{args.eleveurs_version}.csv"
        if not p.exists():
            sys.exit(f"{p} missing — build the M6 export first (m6_s9_export.py --version {args.eleveurs_version}).")
        seen = {r["siret"] for r in rows if r["siret"]}
        for e in read_csv(p, delim=","):
            if e.get("codeNAF") in EXCLUDED_NAF or e.get("type_elevage") == "porcins":
                stats["eleveurs dropped: porcins (principle)"] += 1
                continue
            if e.get("flag_animaux_compagnie") == "1":
                stats["eleveurs dropped: pet trade"] += 1
                continue
            if e.get("population_source") != "registre":
                stats["eleveurs dropped: provider-only row (no SIRET)"] += 1
                continue
            if e["siret"] in seen:
                stats["eleveurs dropped: SIRET already a producteurs row"] += 1
                continue
            row = {c: clean(e.get(c, "")) for c in COLUMNS}
            row.update({
                "sous_segment": merge_tags("éleveur", e.get("type_elevage", "")),
                "descriptif_activite": clean(e.get("productions_bio") or e.get("categories") or e.get("type_elevage")),
                "source_contexte": "agencebio" if e.get("productions_bio") else "registre (NAF)",
                "contact_directory": "", "population_source": "eleveurs (M6)",
            })
            rows.append(row)
            seen.add(e["siret"])
            stats["eleveurs rows appended"] += 1
        withhold_shared_sites()

    # ---- Sans SIRET sheet ----
    # a listing whose phone or e-mail is already on a matched row IS that
    # business (another directory named it): never a second row
    main_phones = {phone_digits(r["telephone_final"]) for r in rows if r["telephone_final"]}
    main_mails = {r["email_final"].lower() for r in rows if r["email_final"]}
    sans = []
    seen_key = set()
    for u in read_csv(CHECK_DIR / f"unmatched_{dept}.csv"):
        if (phone_digits(nphone(u.get("phone")) or "") in main_phones and nphone(u.get("phone"))) or \
           ((u.get("email") or "").lower().strip() in main_mails and (u.get("email") or "").strip()):
            stats["sans siret dropped: phone / e-mail already on a matched row"] += 1
            continue
        if listing_excluded(u["name"], u["categorie"], u["website"], u["email"], u["description"]):
            stats["sans siret dropped: principle"] += 1
            continue
        e = (u.get("email") or "").lower().strip()
        tel = nphone(u.get("phone")) or ""
        # what the crawl of the listing's own website found (m7_s4 --unmatched),
        # taken only when the site was validated for THIS listing
        urid = f"U{u['source']}:{u['listing_id']}"
        for c in site_contacts.get(urid, []):
            if c.get("confiance") not in ("confirme", "probable"):
                continue
            ce = (c.get("email") or "").lower().strip()
            if ce and not e and verified.get(ce) != "invalide" and not is_aggregator(ce.partition("@")[2]):
                e = ce
                stats["sans siret: e-mail from the listing's own site"] += 1
            cp_ = nphone(c.get("phone")) or ""
            if cp_ and not tel and not is_surtaxe(cp_):
                tel = cp_
                stats["sans siret: phone from the listing's own site"] += 1
        if e and verified.get(e) == "invalide":
            e = ""
            stats["sans siret: e-mail withheld invalide"] += 1
        if e and e in main_mails:
            # the listing's own site names an address a matched row already ships: same business
            stats["sans siret dropped: site e-mail already on a matched row"] += 1
            continue
        if tel and phone_digits(tel) in main_phones:
            stats["sans siret dropped: site phone already on a matched row"] += 1
            continue
        if tel and phone_digits(tel) in sent_phones:
            stats["sans siret dropped: phone already sent to Maha"] += 1
            continue
        k = phone_digits(tel) or e or u["name"].lower()
        if k in seen_key:
            stats["sans siret: duplicate listing"] += 1
            continue
        seen_key.add(k)
        sans.append({"name": clean(u["name"]), "contact": clean(u["alt_name"]), "phone": tel,
                     "mobile": nphone(u.get("mobile")) or "", "email": e,
                     "email_statut": (verified.get(e, "") or "declare") if e else "",
                     "website": clean(u["website"]), "address": clean(u["address"]),
                     "postcode": u["postcode"], "city": clean(u["city"]),
                     "sous_segment": merge_tags(tags_from_category(f"{u['categorie']} {u['productions']}")),
                     "description": clean(u["description"])[:1500], "source": u["source"], "url": u["url"]})

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / f"producteurs_{dept}_{args.version}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    sans_csv = OUT_DIR / f"producteurs_{dept}_{args.version}_sans_siret.csv"
    with sans_csv.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SANS_SIRET_COLUMNS)
        w.writeheader()
        w.writerows(sans)

    from openpyxl import Workbook
    xlsx_path = OUT_DIR / f"producteurs_{dept}_{args.version}.xlsx"
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Producteurs")
    ws.append(COLUMNS)
    for r in rows:
        ws.append([str(r[c]) if c in ("siret", "codePostal", "siren") and r[c] else r[c] for c in COLUMNS])
    ws2 = wb.create_sheet("Sans SIRET")
    ws2.append(SANS_SIRET_COLUMNS)
    for r in sans:
        ws2.append([str(r[c]) if c == "postcode" and r[c] else r[c] for c in SANS_SIRET_COLUMNS])
    wb.save(xlsx_path)

    n = len(rows)
    tel = sum(1 for r in rows if r["telephone_final"])
    mail = sum(1 for r in rows if r["email_final"])
    site = sum(1 for r in rows if r["website_final"])
    reach = sum(1 for r in rows if r["telephone_final"] or r["email_final"])
    log.info("-" * 62)
    log.info(f"[{dept}] {n} rows ({n_reg} producteurs + {n - n_reg} éleveurs) -> {xlsx_path.name} + .csv ; Sans SIRET sheet {len(sans)} rows")
    log.info(f"  telephone_final   {tel:>5}  ({tel/n:.1%})  by source: "
             f"{dict(Counter(r['source_telephone'].split('(')[0] for r in rows if r['telephone_final']).most_common())}")
    log.info(f"  email_final       {mail:>5}  ({mail/n:.1%})  by source: "
             f"{dict(Counter(r['source_email'] for r in rows if r['email_final']).most_common())}  statut: "
             f"{dict(Counter(r['email_statut'] or 'non verifie' for r in rows if r['email_final']))}")
    log.info(f"  website_final     {site:>5}  ({site/n:.1%})")
    log.info(f"  joignables        {reach:>5}  ({reach/n:.1%})  (dialable phone OR e-mail)")
    log.info(f"  contact (gérant)  {sum(1 for r in rows if r['gerant']):>5}   contact from a directory {sum(1 for r in rows if r['contact_directory'])}"
             f"   description from a directory {sum(1 for r in rows if r['source_contexte'] in CONTEXT_RANK)}")
    log.info(f"  sous-segments     {dict(Counter(t for r in rows for t in r['sous_segment'].split('|') if t).most_common(12))}")
    log.info(f"  deja_envoye {dict(Counter(r['deja_envoye'] for r in rows if r['deja_envoye']))}")
    srcs = sorted({s for s, _ in agreement})
    if srcs:
        log.info("  phone agreement with another witness on the same row (the measurement the ranks rest on):")
        for s in srcs:
            a, d = agreement[(s, "agree")], agreement[(s, "disagree")]
            log.info(f"    {s:<20} agree {a:>4}  disagree {d:>4}  -> {a/(a+d):.0%} of {a+d}")
    log.info(f"  Sans SIRET: phone {sum(1 for r in sans if r['phone'])}  e-mail {sum(1 for r in sans if r['email'])}  "
             f"by source {dict(Counter(r['source'] for r in sans))}")
    for k, v in stats.most_common():
        log.info(f"  {k:<70} {v}")


if __name__ == "__main__":
    main()
