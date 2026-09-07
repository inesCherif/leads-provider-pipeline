r"""
M4-S2 — load the agriculteurs bio deliverable (dept 63 V2 / dept 03 V1)
========================================================================
    python scripts/m4_s2_load_agriculteurs.py --departement 63 --dry-run
    python scripts/m4_s2_load_agriculteurs.py --departement 63
    python scripts/m4_s2_load_agriculteurs.py --departement 03

Inputs (files only until the write):
    exports/agriculteurs/agriculteurs_<dept>_<version>.csv   the deliverable
    exports/agriculteurs/checkpoints/agencebio_<dept>.jsonl  full Agence Bio
        payloads -> raw.ingest_rows, and the certification/production facts
    exports/agriculteurs/checkpoints/sirene_etat_<dept>.csv  m3ag_s13 liveness
        (etat_ul / etat_etab / date_fermeture / note)
    exports/agriculteurs/checkpoints/site_verdicts.csv       (row, domain) verdicts
    exports/agriculteurs/checkpoints/verified_emails.csv     SMTP verdicts incl.
        the declared Agence Bio addresses (m3ag_s11, 2026-09-07)
    exports/agriculteurs/checkpoints/site_contacts.csv       crawl claims

Identity: the operator key is numeroBio (unique). A valid SIRET links the
operator to a company/site (two operators on one SIRET = one site, two
numero_bio identifiers, one contact each). A 13-char SIRET is kept as a
siret_truncated identifier, never padded. No SIRET -> the company is found
again by numero_bio.

Liveness: etat_etab = 'F' -> sites.is_active = FALSE (drops the row from the
deliverable views); etat_ul = 'C' -> companies.sirene_etat = 'F'; the
liquidator note and closure date go to company_attributes.

E-mails: 'declare' (Agence Bio, self-declared) -> verification_status
'unknown' with source 'agencebio', unless verified_emails says otherwise;
invalid ones never reach staging.emails and propagate to every staged copy.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import psycopg2                                    # noqa: E402
import psycopg2.extras                             # noqa: E402
from ingest_lib import (                           # noqa: E402
    classify_email, clean_phone, clean_siret, clean_str, get_conn, is_surtaxe,
    json_dumps, phone_digits, propagate_invalid_emails, sha256_file, siret_to_siren,
    truncated_identifier,
)
from m1_s3_ingest import stage_attributes, stage_companies, stage_membership   # noqa: E402

EXPORT_DIR = PROJECT_ROOT / "exports" / "agriculteurs"
CKPT       = EXPORT_DIR / "checkpoints"
SECTOR     = "agriculteurs_bio"
SCRIPT     = "m4_s2_load_agriculteurs.py"
PAGE       = 2000
VERSION    = {"63": "v2", "03": "v1"}
COLLECTED  = {"63": date(2026, 9, 3), "03": date(2026, 9, 3)}

# m3ag_s9_export.PHONE_RANK — position = rank; anything else is not dialable
PHONE_RANK = {"agencebio": 0, "osm": 1, "bienvenue_ferme": 2, "pagesjaunes": 3, "places": 4,
              "site/confirme": 5, "corrobore": 6}
VERDICT_FR = {"valide": "valid", "invalide": "invalid", "risque": "risky", "non verifie": "unknown",
              "declare": "unknown"}
RAW_PHONE_COLS = [("telephone", "agencebio"), ("telephoneCommerciale", "agencebio"), ("pj_phone", "pagesjaunes"),
                  ("pj_mobile", "pagesjaunes"), ("osm_phone", "osm"), ("baf_phone", "bienvenue_ferme"),
                  ("places_phone", "places")]
RAW_EMAIL_COLS = [("email", "agencebio"), ("osm_email", "osm"), ("baf_email", "bienvenue_ferme")]
RAW_SITE_COLS  = [("siteWebs", "agencebio"), ("osm_website", "osm"), ("baf_website", "bienvenue_ferme"),
                  ("places_website", "places")]


def read_csv(path: Path, sep=",") -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=sep))


def domain_of(url: str | None) -> str | None:
    u = clean_str(url)
    if not u:
        return None
    u = u.split(";")[0].strip()
    if not u.startswith("http"):
        u = "http://" + u
    host = (urlparse(u).hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    # a query-string fragment ('searchfirstavailabledates=1') is not a host
    return host if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", host) else None


def parse_date(s) -> date | None:
    s = clean_str(s)
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s[:16], fmt).date()
        except ValueError:
            continue
    return None


def rank_of(label: str | None) -> tuple[int | None, bool]:
    if not label:
        return None, False
    key = "corrobore" if label.startswith("corrobore") else label
    r = PHONE_RANK.get(key)
    return r, r is not None


def operator_facts(payload: dict) -> dict:
    """The Agence Bio facts that have no column: certification lifecycle,
    productions with their AB/conversion state, sales channels, addresses."""
    certs = [{k: c.get(k) for k in ("numeroControleEu", "organisme", "etatCertification",
                                     "dateEngagement", "dateArret", "dateSuspension", "url")}
             for c in payload.get("certificats") or []]
    prods = [{"code": p.get("code"), "nom": p.get("nom"),
              "etats": [(e.get("etatProduction"), e.get("anneeReferenceControle")) for e in p.get("etatProductions") or []]}
             for p in payload.get("productions") or []]
    addrs = [{"types": a.get("typeAdresseOperateurs"), "cp": a.get("codePostal"), "active": a.get("active")}
             for a in payload.get("adressesOperateurs") or []]
    return {
        "numeroBio": payload.get("numeroBio"), "denominationcourante": payload.get("denominationcourante"),
        "certificats": certs, "productions": prods, "activites": payload.get("activites"),
        "categories": payload.get("categories"), "venteAnnuaire": payload.get("venteAnnuaire"),
        "mixite": payload.get("mixite"), "reseau": payload.get("reseau"),
        "datePremierEngagement": payload.get("datePremierEngagement"), "dateMaj": payload.get("dateMaj"),
        "telephoneNational": payload.get("telephoneNational"), "adresses": addrs,
        "certification_active": any(c.get("etatCertification") not in ("ARRETEE", "SUSPENDUE") for c in certs) if certs else None,
    }


# ─── build ───────────────────────────────────────────────────────────────────

def build(dept: str, limit: int | None):
    version = VERSION[dept]
    deliv = read_csv(EXPORT_DIR / f"agriculteurs_{dept}_{version}.csv")
    payloads, line_of = {}, {}
    with open(CKPT / f"agencebio_{dept}.jsonl", encoding="utf-8") as f:
        for i, line in enumerate(f):
            d = json.loads(line)
            payloads[str(d.get("numeroBio"))] = d
            line_of[str(d.get("numeroBio"))] = i
    liveness = {r["siret"]: r for r in read_csv(CKPT / f"sirene_etat_{dept}.csv", ";")}
    verdicts = [r for r in read_csv(CKPT / "site_verdicts.csv", ";") if r["dept"] == dept]
    verified = {r["email"].strip().lower(): r for r in read_csv(CKPT / "verified_emails.csv", ";")}
    site_contacts = [r for r in read_csv(CKPT / "site_contacts.csv", ";") if r["dept"] == dept]

    if limit:
        deliv = deliv[:limit]
    rows, by_row_id = [], {}
    for r in deliv:
        nb = clean_str(r["numeroBio"])
        siret = clean_siret(r["siret"])
        trunc = None if siret else truncated_identifier(r["siret"])
        live = liveness.get(siret or "", {})
        n = {
            "numero_bio": nb, "siret": siret, "siren": siret_to_siren(siret),
            "_row_index": line_of.get(nb, -1), "_has_name": True,
            "legal_name": clean_str(r["raisonSociale"]), "trade_name": clean_str((payloads.get(nb) or {}).get("denominationcourante")),
            "naf_label": clean_str(r["categories"]) or clean_str(r["activites"]),
            "naf_code_source": clean_str(r["codeNAF"]), "creation_date": None,
            "address_line1": clean_str(r["adresse"]), "address_line2": None,
            "postal_code": clean_str(r["codePostal"]), "city": clean_str(r["ville"]), "department": dept,
            "latitude": clean_str(r["lat"]), "longitude": clean_str(r["lon"]),
            "status_raw": None, "contact_full_name": clean_str(r["gerant"]),
            "etat_ul": clean_str(live.get("etat_ul")), "etat_etab": clean_str(live.get("etat_etab")),
            "is_active": (live.get("etat_etab") == "A") if live.get("etat_etab") else None,
            "sirene_checked_at": parse_date(live.get("checked_at")),
            "_ids": {"numero_bio": nb} | ({"siret_truncated": trunc} if trunc else {}),
        }
        # phones: the dialled one first, then every raw source column, then piste
        phones, seen = [], set()
        tel = clean_phone(r["telephone_final"])
        src = clean_str(r["source_telephone"]) or "unknown"
        if tel:
            rank, dialable = rank_of(src)
            phones.append((src, tel, is_surtaxe(tel), dialable and not is_surtaxe(tel), rank, {"column": "telephone_final"}))
            seen.add((src, tel))
        for col, label in RAW_PHONE_COLS:
            for p in re.split(r"\s*[|;/]\s*", r.get(col) or ""):
                cp_ = clean_phone(p)
                if cp_ and (label, cp_) not in seen:
                    rank, dialable = rank_of(label)
                    phones.append((label, cp_, is_surtaxe(cp_), dialable and not is_surtaxe(cp_), rank, {"column": col}))
                    seen.add((label, cp_))
        for item in re.split(r"\s*\|\s*", r.get("telephone_piste") or ""):
            m = re.match(r"(.+?)\s*\((.+)\)\s*$", item.strip())
            cp_ = clean_phone(m.group(1) if m else item)
            if cp_ and ("piste", cp_) not in seen:
                phones.append(("piste", cp_, is_surtaxe(cp_), False, None, {"witness": m.group(2) if m else None}))
                seen.add(("piste", cp_))
        n["_phones"] = phones
        n["phone_main"] = tel if (tel and phones and phones[0][3]) else None
        n["phone_alt"] = None
        # e-mails: best pick + every raw column + emails_autres, verdict from verified_emails
        emails, seen_e = [], set()
        best, bverdict, _ = classify_email(r["email_final"])
        n["email_address"], n["email_status"], n["email_source"] = None, None, None
        if best and bverdict == "candidate":
            fr = clean_str(r["email_statut"]) or "non verifie"
            vf = verified.get(best)
            status = VERDICT_FR.get(vf["verdict"], "unknown") if vf else VERDICT_FR.get(fr, "unknown")
            source = clean_str(r["source_email"]) or "unknown"
            if status != "invalid":
                n["email_address"], n["email_status"], n["email_source"] = best, status, source
            emails.append((source, best, status, {"column": "email_final", "statut": fr, "smtp": vf.get("detail") if vf else None}))
            seen_e.add(best)
        elif best:
            emails.append(("deliverable", best, "malformed", {"column": "email_final"}))
        for col, label in RAW_EMAIL_COLS + [("emails_autres", "autres")]:
            for e in re.split(r"\s*[|;,]\s*", r.get(col) or ""):
                norm, verdict, ev = classify_email(e)
                if not norm or norm in seen_e:
                    continue
                seen_e.add(norm)
                if verdict == "candidate":
                    vf = verified.get(norm)
                    emails.append((label, norm, VERDICT_FR.get(vf["verdict"], "unknown") if vf else "unknown",
                                   {"column": col, "smtp": vf.get("detail") if vf else None}))
                else:
                    emails.append((label, norm, "malformed", ev | {"column": col}))
        n["_emails"] = emails
        # websites
        n["website_url"] = clean_str(r["website_final"])
        n["website_domain"] = domain_of(n["website_url"])
        n["website_confidence"] = clean_str(r["site_confiance"])
        n["website_source"] = clean_str(r["source_website"])
        n["_sites"] = []
        for col, label in RAW_SITE_COLS:
            for u in re.split(r"\s*[|;,]\s*", r.get(col) or ""):
                d = domain_of(u)
                if d:
                    n["_sites"].append((label, d, u.strip()))
        n["_socials"] = [(k, clean_str(r[k])) for k in ("facebook", "instagram") if clean_str(r[k])]
        facts = operator_facts(payloads.get(nb) or {})
        facts.update({"flag_hors_agri": r.get("flag_hors_agri"), "baf_contact": clean_str(r.get("baf_contact")),
                      "sirene": {k: clean_str(live.get(k)) for k in ("etat_ul", "etat_etab", "date_fermeture", "note", "checked_at")} if live else None})
        n["_attrs"] = {"operateurs": {nb: facts}}
        rows.append(n)
        by_row_id[nb] = n
        if siret:
            by_row_id.setdefault(siret, n)

    web_claims, site_claims = [], []
    for v in verdicts:
        n = by_row_id.get(v["row_id"]) or by_row_id.get(clean_siret(v["siret"]) or "")
        if n and v["domain"]:
            web_claims.append((n, v["domain"].lower(), v["verdict"], v.get("own"),
                               {"source": v.get("source"), "reason": v.get("reason"), "own": v.get("own"),
                                "shared": v.get("shared"), "agri_score": v.get("agri_score"), "reached": v.get("reached")}))
            if n["website_domain"] == v["domain"].lower():
                n["website_verdict"] = v["verdict"]
    for sc in site_contacts:
        n = by_row_id.get(sc["row_id"]) or by_row_id.get(clean_siret(sc["siret"]) or "")
        if not n:
            continue
        conf = sc.get("confiance") or "faible"
        cp_ = clean_phone(sc.get("phone"))
        if cp_:
            site_claims.append((n, "phone", phone_digits(cp_), cp_, f"site/{conf}", conf, None,
                                conf == "confirme" and not is_surtaxe(cp_), is_surtaxe(cp_), PHONE_RANK.get(f"site/{conf}"),
                                {"domain": sc.get("domain")}))
        norm, verdict, _ = classify_email(sc.get("email"))
        if norm and verdict == "candidate":
            vf = verified.get(norm)
            site_claims.append((n, "email", norm, norm, f"site/{conf}", conf,
                                VERDICT_FR.get(vf["verdict"], "unknown") if vf else "unknown", None, None, None,
                                {"domain": sc.get("domain"), "smtp": vf.get("detail") if vf else None}))
        for k in ("facebook", "instagram"):
            if clean_str(sc.get(k)):
                site_claims.append((n, k, sc[k].strip(), sc[k].strip(), f"site/{conf}", conf, None, None, None, None,
                                    {"domain": sc.get("domain")}))
    return rows, web_claims, site_claims, len(payloads), version


def describe(rows, web, site_claims, n_api):
    c = Counter()
    for n in rows:
        c["rows"] += 1
        c["siret"] += bool(n["siret"]); c["truncated"] += "siret_truncated" in n["_ids"]
        c["closed_etab"] += n["is_active"] is False
        c["ul_ceased"] += n["etat_ul"] == "C"
        c["gerant"] += bool(n["contact_full_name"])
        c["phone_main"] += bool(n["phone_main"]); c["phone_claims"] += len(n["_phones"])
        c["piste"] += sum(1 for p in n["_phones"] if p[0] == "piste")
        c["email_ships"] += bool(n["email_address"]); c["email_claims"] += len(n["_emails"])
        c["email_invalid_claims"] += sum(1 for e in n["_emails"] if e[2] == "invalid")
        c["website"] += bool(n["website_domain"]); c["website_verdict"] += bool(n.get("website_verdict"))
        c["latlon"] += bool(n["latitude"]); c["socials"] += len(n["_socials"])
        c["hors_agri"] += n["_attrs"]["operateurs"][n["numero_bio"]].get("flag_hors_agri") == "1"
        c["cert_stopped"] += n["_attrs"]["operateurs"][n["numero_bio"]].get("certification_active") is False
    print(f"\n  deliverable rows {c['rows']:,} · Agence Bio payloads {n_api:,}")
    for k in ("siret", "truncated", "closed_etab", "ul_ceased", "gerant", "latlon", "phone_main", "phone_claims",
              "piste", "email_ships", "email_claims", "email_invalid_claims", "website", "website_verdict",
              "socials", "hors_agri", "cert_stopped"):
        print(f"  {k:<20} {c[k]:>6,}")
    print(f"  website verdict claims {len(web):,} · site claims {len(site_claims):,}")


# ─── writers ─────────────────────────────────────────────────────────────────

def stage_register(conn, path: Path, file_hash: str, dept: str, n_raw: int):
    with conn.cursor() as cur:
        cur.execute("SELECT id, row_count_imported FROM staging.source_files WHERE file_hash = %s", (file_hash,))
        row = cur.fetchone()
        if row:
            return row[0], ("done" if row[1] is not None else "resume")
        cur.execute("""INSERT INTO staging.source_files
                (file_name, file_hash, sector, pipeline_stage, notes, row_count_raw, imported_by, data_source, collected_at)
                VALUES (%s,%s,%s,'verified',%s,%s,%s,%s,%s) RETURNING id""",
                    (path.name, file_hash, SECTOR,
                     f"Agence Bio operators dept {dept} + M3AG enrichment. raw rows = agencebio_{dept}.jsonl payloads.",
                     n_raw, SCRIPT, "opendata.agencebio.org + M3AG enrichment (OSM, PJ, BAF, sites, SMTP, SIRENE liveness)",
                     COLLECTED[dept]))
        sfid = cur.fetchone()[0]
    conn.commit()
    return sfid, "new"


def stage_raw(conn, sfid, dept) -> int:
    recs = []
    with open(CKPT / f"agencebio_{dept}.jsonl", encoding="utf-8") as f:
        recs = [(sfid, i, line.strip()) for i, line in enumerate(f)]
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, "INSERT INTO raw.ingest_rows (source_file_id, row_index, raw_json) "
                                            "VALUES %s ON CONFLICT DO NOTHING", recs, page_size=PAGE)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.ingest_rows WHERE source_file_id = %s", (sfid,))
        return cur.fetchone()[0]


def stage_registry(conn, rows) -> int:
    """SIRENE liveness from m3ag_s13 onto the company (never overwrites a value)."""
    recs = [(n["_company_id"], "F" if n["etat_ul"] == "C" else ("A" if n["etat_ul"] == "A" else None),
             n["sirene_checked_at"]) for n in rows if n.get("_company_id") and n["etat_ul"]]
    with conn.cursor() as cur:
        if recs:
            psycopg2.extras.execute_values(cur, """
                UPDATE staging.companies c
                   SET sirene_etat = COALESCE(c.sirene_etat, v.etat::char(1)),
                       sirene_last_checked_at = COALESCE(c.sirene_last_checked_at, v.checked::timestamptz)
                  FROM (VALUES %s) AS v(id, etat, checked) WHERE c.id = v.id::uuid""", recs, page_size=PAGE)
    conn.commit()
    return len(recs)


def stage_sites(conn, sfid, rows) -> dict:
    stats = Counter()
    with conn.cursor() as cur:
        by_siret = {}
        for n in rows:
            if n["siret"]:
                by_siret.setdefault(n["siret"], n)
        if by_siret:
            res = psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.sites
                    (siret, company_id, is_headquarters, is_active, address_line1, postal_code, city, department,
                     latitude, longitude, website_url, website_domain, website_verdict, website_confidence, source_file_id)
                VALUES %s
                ON CONFLICT (siret) DO UPDATE SET
                    is_active          = COALESCE(EXCLUDED.is_active, staging.sites.is_active),
                    latitude           = COALESCE(staging.sites.latitude,  EXCLUDED.latitude),
                    longitude          = COALESCE(staging.sites.longitude, EXCLUDED.longitude),
                    website_url        = COALESCE(staging.sites.website_url,        EXCLUDED.website_url),
                    website_domain     = COALESCE(staging.sites.website_domain,     EXCLUDED.website_domain),
                    website_verdict    = COALESCE(staging.sites.website_verdict,    EXCLUDED.website_verdict),
                    website_confidence = COALESCE(staging.sites.website_confidence, EXCLUDED.website_confidence)
                RETURNING siret, id, (xmax = 0) AS inserted""",
                [(s, n["_company_id"], True, n["is_active"], n["address_line1"], n["postal_code"], n["city"],
                  n["department"], n["latitude"], n["longitude"], n["website_url"], n["website_domain"],
                  n.get("website_verdict"), n["website_confidence"], sfid) for s, n in by_siret.items()],
                page_size=PAGE, fetch=True)
            ids = {}
            for siret, sid, inserted in res:
                ids[siret] = sid
                stats["siret_inserted" if inserted else "siret_merged"] += 1
            for n in rows:
                if n["siret"]:
                    n["_site_id"] = ids[n["siret"]]
        need = {n["_company_id"] for n in rows if not n["siret"] and n.get("_company_id")}
        existing = {}
        if need:
            cur.execute("""SELECT DISTINCT ON (company_id) company_id, id FROM staging.sites
                           WHERE company_id = ANY(%s::uuid[]) AND siret IS NULL ORDER BY company_id, created_at, id""",
                        (list(need),))
            existing = dict(cur.fetchall())
            stats["fallback_reused"] = len(existing)
        first = {}
        for n in rows:
            if not n["siret"] and n.get("_company_id") and n["_company_id"] not in existing:
                first.setdefault(n["_company_id"], n)
        if first:
            res = psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.sites (company_id, is_headquarters, is_active, address_line1, postal_code, city,
                    department, latitude, longitude, website_url, website_domain, website_verdict, website_confidence, source_file_id)
                VALUES %s RETURNING company_id, id""",
                [(cid, True, n["is_active"], n["address_line1"], n["postal_code"], n["city"], n["department"],
                  n["latitude"], n["longitude"], n["website_url"], n["website_domain"], n.get("website_verdict"),
                  n["website_confidence"], sfid) for cid, n in first.items()], page_size=PAGE, fetch=True)
            existing.update(dict(res))
            stats["fallback_inserted"] = len(res)
        for n in rows:
            if not n["siret"] and n.get("_company_id"):
                n["_site_id"] = existing.get(n["_company_id"])
    conn.commit()
    return stats


def stage_contacts(conn, sfid, rows) -> dict:
    """One contact per OPERATOR (numeroBio): named after the gérant when known,
    else a generic contact carrying the operator's phone/e-mail."""
    stats = Counter()
    site_ids = list({n["_site_id"] for n in rows if n.get("_site_id")})
    with conn.cursor() as cur:
        cur.execute("""SELECT site_id, full_name, is_generic_contact, id FROM staging.contacts
                       WHERE site_id = ANY(%s::uuid[]) ORDER BY site_id, created_at, id""", (site_ids,))
        named, generic = {}, {}
        for sid, fn, gen, cid in cur.fetchall():
            if fn:
                named.setdefault((sid, fn), cid)
            elif gen:
                generic.setdefault(sid, cid)
        new_named, new_generic, fill = [], [], []
        for n in rows:
            sid = n.get("_site_id")
            if not sid:
                continue
            name = n["contact_full_name"]
            if name:
                if (sid, name) in named:
                    n["_contact_id"] = named[(sid, name)]; stats["named_reused"] += 1
                    fill.append((named[(sid, name)], n["phone_main"]))
                else:
                    new_named.append((sid, n["_company_id"], name, n["phone_main"], False, "not_started", sfid, "agencebio_gerant"))
                    named[(sid, name)] = None
            elif n["phone_main"] or n["email_address"]:
                if sid in generic:
                    n["_contact_id"] = generic[sid]; stats["generic_reused"] += 1
                    fill.append((generic[sid], n["phone_main"]))
                else:
                    new_generic.append((sid, n["_company_id"], n["phone_main"], True, "not_started", sfid))
                    generic[sid] = None
        if new_named:
            res = psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.contacts (site_id, company_id, full_name, phone_main, is_generic_contact,
                    enrichment_status, source_file_id, name_source)
                VALUES %s RETURNING site_id, full_name, id""", new_named, page_size=PAGE, fetch=True)
            for sid, fn, cid in res:
                named[(sid, fn)] = cid
            stats["named_inserted"] = len(res)
        if new_generic:
            res = psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.contacts (site_id, company_id, phone_main, is_generic_contact, enrichment_status, source_file_id)
                VALUES %s RETURNING site_id, id""", new_generic, page_size=PAGE, fetch=True)
            for sid, cid in res:
                generic[sid] = cid
            stats["generic_inserted"] = len(res)
        if fill:
            psycopg2.extras.execute_values(cur, """
                UPDATE staging.contacts c SET phone_main = COALESCE(c.phone_main, v.pm)
                FROM (VALUES %s) AS v(id, pm) WHERE c.id = v.id::uuid""", fill, page_size=PAGE)
        for n in rows:
            sid = n.get("_site_id")
            if not sid or n.get("_contact_id"):
                continue
            n["_contact_id"] = named.get((sid, n["contact_full_name"])) if n["contact_full_name"] else generic.get(sid)
    conn.commit()
    return stats


def stage_emails(conn, sfid, rows, dept) -> dict:
    recs, cids = [], set()
    when = datetime.combine(COLLECTED[dept], datetime.min.time())
    for n in rows:
        if n["email_address"] and n.get("_contact_id"):
            recs.append((n["_contact_id"], n["email_address"], False, n["email_status"],
                         when if n["email_status"] != "unknown" else None,
                         "m3ag_s11 smtp" if n["email_status"] != "unknown" else None, sfid, n["email_source"]))
            cids.add(n["_contact_id"])
    stats = Counter(candidates=len(recs))
    with conn.cursor() as cur:
        if recs:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.emails (contact_id, email_address, is_primary, verification_status, verified_at,
                                            verifier_tool, source_file_id, source)
                VALUES %s ON CONFLICT (contact_id, email_address) DO UPDATE SET
                    verification_status = CASE WHEN staging.emails.verification_status IN ('candidate','unknown')
                                               THEN EXCLUDED.verification_status ELSE staging.emails.verification_status END,
                    verified_at = COALESCE(staging.emails.verified_at, EXCLUDED.verified_at),
                    verifier_tool = COALESCE(staging.emails.verifier_tool, EXCLUDED.verifier_tool),
                    source = COALESCE(staging.emails.source, EXCLUDED.source)""", recs, page_size=PAGE)
            cur.execute("""
                UPDATE staging.emails e SET is_primary = TRUE WHERE e.id IN (
                    SELECT DISTINCT ON (contact_id) id FROM staging.emails x
                    WHERE x.contact_id = ANY(%s::uuid[]) AND x.verification_status <> 'invalid'
                      AND NOT EXISTS (SELECT 1 FROM staging.emails p WHERE p.contact_id = x.contact_id AND p.is_primary)
                    ORDER BY contact_id, (verification_status = 'valid') DESC, created_at, id)""", (list(cids),))
            stats["primaries_set"] = cur.rowcount
    conn.commit()
    return stats


def stage_points(conn, sfid, rows, web, site_claims, dept) -> dict:
    when = COLLECTED[dept]
    recs, stats = [], Counter()
    for n in rows:
        cid, sid, ctid = n["_company_id"], n.get("_site_id"), n.get("_contact_id")
        for label, canonical, surtaxe, dialable, rank, ev in n["_phones"]:
            recs.append((cid, sid, ctid, "phone", phone_digits(canonical), canonical, label,
                         "dialled" if dialable else ("piste" if label == "piste" else None), None, dialable, surtaxe, rank,
                         json_dumps(ev | {"numeroBio": n["numero_bio"]}), when, sfid))
            stats["phone"] += 1
        for src, norm, verdict, ev in n["_emails"]:
            recs.append((cid, sid, ctid, "email", norm, norm, src, None, verdict, None, None, None,
                         json_dumps(ev | {"numeroBio": n["numero_bio"]}), when, sfid))
            stats["email_" + verdict] += 1
        for label, d, url in n["_sites"]:
            recs.append((cid, sid, None, "website", d, url, label, None, None, None, None, None,
                         json_dumps({"numeroBio": n["numero_bio"]}), when, sfid))
            stats["website_raw"] += 1
        for kind, url in n["_socials"]:
            recs.append((cid, sid, ctid, kind, url, url, "deliverable", None, None, None, None, None, None, when, sfid))
            stats["social"] += 1
    for n, domain, verdict, own, ev in web:
        recs.append((n["_company_id"], n.get("_site_id"), None, "website", domain, domain, "validator", own, verdict,
                     None, None, None, json_dumps(ev), when, sfid))
        stats["website_verdict"] += 1
    for n, kind, norm, raw, src, conf, verdict, dialable, surtaxe, rank, ev in site_claims:
        recs.append((n["_company_id"], n.get("_site_id"), None, kind, norm, raw, src, conf, verdict, dialable, surtaxe,
                     rank, json_dumps(ev), when, sfid))
        stats["site_" + kind] += 1
    for start in range(0, len(recs), 5000):
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.contact_points
                    (company_id, site_id, contact_id, kind, value_norm, value_raw, source, confidence, verdict,
                     is_dialable, is_surtaxe, rank_hint, evidence, observed_at, source_file_id)
                VALUES %s ON CONFLICT (company_id, kind, value_norm, source) DO NOTHING""",
                recs[start:start + 5000], page_size=PAGE)
        conn.commit()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--departement", required=True, choices=sorted(VERSION))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    dept = args.departement

    rows, web, site_claims, n_api, version = build(dept, args.limit)
    describe(rows, web, site_claims, n_api)
    if args.dry_run:
        print("\n  DRY RUN — nothing written.")
        return 0
    path = EXPORT_DIR / f"agriculteurs_{dept}_{version}.csv"
    file_hash = sha256_file(path)
    conn = get_conn()
    try:
        sfid, state = stage_register(conn, path, file_hash, dept, n_api)
        if state == "done":
            print(f"  [SKIP] Already imported (source_file_id={sfid}).")
            return 0
        print(f"  source_file_id={sfid} ({state})")
        print(f"  raw       written={stage_raw(conn, sfid, dept):,}")
        print(f"  companies {dict(stage_companies(conn, sfid, rows))}")
        print(f"  registry  liveness applied={stage_registry(conn, rows):,}")
        print(f"  members   written={stage_membership(conn, sfid, rows):,}")
        print(f"  sites     {dict(stage_sites(conn, sfid, rows))}")
        print(f"  contacts  {dict(stage_contacts(conn, sfid, rows))}")
        print(f"  emails    {dict(stage_emails(conn, sfid, rows, dept))}")
        print(f"  points    {dict(stage_points(conn, sfid, rows, web, site_claims, dept))}")
        # two operators on one company: merge their facts under attrs.operateurs
        # BEFORE the shallow merge in stage_attributes / the DB's `||`
        merged = {}
        for n in rows:
            merged.setdefault(n["_company_id"], {}).update(n["_attrs"]["operateurs"])
        for n in rows:
            n["_attrs"] = {"operateurs": merged[n["_company_id"]]}
        print(f"  attrs     companies={stage_attributes(conn, sfid, rows, SECTOR):,}")
        print(f"  bounces   {propagate_invalid_emails(conn)}")
        if not args.limit:
            with conn.cursor() as cur:
                cur.execute("UPDATE staging.source_files SET row_count_imported = %s WHERE id = %s", (n_api, sfid))
            conn.commit()
            print(f"  finish    row_count_imported={n_api:,}")
        print("  Done. Now run: python scripts/check_data_quality.py --strict")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
