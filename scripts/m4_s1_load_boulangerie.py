r"""
M4-S1 — load the boulangerie dept-13 deliverable (V15) into Supabase
=====================================================================
    python scripts/m4_s1_load_boulangerie.py --dry-run
    python scripts/m4_s1_load_boulangerie.py [--version v15] [--limit N]

Inputs (all files, DB-free until the write; the M2 rule):
    exports/boulangerie/boulangerie_13_<version>.csv   the deliverable (best picks)
    exports/boulangerie/checkpoints/api_raw.jsonl      3,825 recherche-entreprises
                                                       payloads -> raw.ingest_rows
    exports/boulangerie/checkpoints/etablissements.csv lat/lon, tranche, contact
    exports/boulangerie/checkpoints/site_verdicts.csv  (SIRET, domain) -> verdict
    exports/boulangerie/checkpoints/verified_emails.csv e-mail -> SMTP verdict,
                                                       INCLUDING the 135 invalide
    exports/boulangerie/checkpoints/site_emails.csv    e-mail claims with confidence
    exports/boulangerie/checkpoints/site_contacts.csv  phone/social claims from sites

What it writes, each stage in its own committed transaction (idempotent):
    source_files   one row for the deliverable, sector='boulangerie',
                   data_source='recherche-entreprises.api.gouv.fr + M2 enrichment'
    raw            every JSONL line, row_index = line number
    companies      SIREN path. Registry facts go to the REGISTRY columns
                   (naf_code, legal_form, employee_bracket, creation_date,
                   nature_juridique_code, sirene_etat='A', sirene_last_checked_at)
                   because recherche-entreprises IS the source m1_s4 uses.
    members        company_sources, row_index = the SIREN's JSONL line
    sites          SIRET path + lat/lon + website_url/domain/verdict/confidence
    contacts       prenom/nom/fonction (name_source='rne_dirigeant'), or the
                   personne-morale officer, or a generic contact; phone_main =
                   the DIALLED phone only
    emails         the deliverable's Email with its SMTP verdict remapped
                   (valide->valid, non verifie->unknown, risque->risky), source =
                   the confidence label; is_primary set once per contact
    points         contact_points: every phone (dialled with its source; piste
                   with is_dialable=false), every e-mail (deliverable +
                   site_emails + verified_emails, invalid ones included),
                   every (SIRET, domain) verdict as kind='website', socials
    attrs          company_attributes: procedure_collective, source_contact,
                   page_contact, autres_emails, telephone_surtaxe

Rules carried over from M2 (measured, do not soften):
    PHONE_RANK decides is_dialable; a source not in it never reaches phone_main.
    An 'invalide' e-mail is stored (contact_points, verdict='invalid') so it is
    never re-proposed, and never written to staging.emails.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import psycopg2                                    # noqa: E402
import psycopg2.extras                             # noqa: E402
from ingest_lib import (                           # noqa: E402
    classify_email, clean_phone, clean_siren, clean_siret, clean_str, get_conn,
    is_surtaxe, json_dumps, phone_digits, propagate_invalid_emails, sha256_file,
)
from m1_s3_ingest import stage_attributes, stage_membership   # noqa: E402

EXPORT_DIR = PROJECT_ROOT / "exports" / "boulangerie"
CKPT       = EXPORT_DIR / "checkpoints"
SECTOR     = "boulangerie"
COLLECTED  = date(2026, 8, 13)
SCRIPT     = "m4_s1_load_boulangerie.py"
PAGE       = 2000

# m2_s14_export_v3.PHONE_RANK — lower is better; anything else is not dialable
PHONE_RANK = {"osm": 0, "serper_places": 1, "pagesjaunes": 2, "bcontact": 3,
              "site/confirme": 4, "corrobore": 5, "google_panel": 6, "social_fb": 7}
VERDICT_FR = {"valide": "valid", "invalide": "invalid", "risque": "risky", "non verifie": "unknown"}
SITE_SHIPS = {"valide", "non_verifiable"}


def read_csv(path: Path, sep=";") -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=sep))


def parse_date(s) -> date | None:
    s = clean_str(s)
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt).date()
        except ValueError:
            continue
    return None


def domain_of(url: str | None) -> str | None:
    u = clean_str(url)
    if not u:
        return None
    if not u.startswith("http"):
        u = "http://" + u
    host = (urlparse(u).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or None


def strip_cp_city(addr: str | None, cp: str | None) -> str | None:
    """'3 B BOULEVARD CAMILLE FLAMMARION 13001 MARSEILLE' -> the street only."""
    a = clean_str(addr)
    if not a:
        return None
    if cp and cp in a:
        a = a[:a.index(cp)].strip()
    return a or None


def phone_source_rank(label: str) -> tuple[str, int | None, bool]:
    """('serper_places', 1, True) / ('corrobore(a+b)', 5, True) / ('snippet', None, False)."""
    key = "corrobore" if label.startswith("corrobore") else label
    rank = PHONE_RANK.get(key)
    return label, rank, rank is not None


# ─── build the row model ─────────────────────────────────────────────────────

def build(version: str, limit: int | None):
    deliv = read_csv(EXPORT_DIR / f"boulangerie_13_{version}.csv")
    etabs = {r["siret"]: r for r in read_csv(CKPT / "etablissements.csv")}
    verdicts = read_csv(CKPT / "site_verdicts.csv")
    verified = {r["email"].strip().lower(): r for r in read_csv(CKPT / "verified_emails.csv")}
    site_emails = read_csv(CKPT / "site_emails.csv")
    site_contacts = read_csv(CKPT / "site_contacts.csv")

    # JSONL: siren -> (line index, nature_juridique, etat)
    api = {}
    with open(CKPT / "api_raw.jsonl", encoding="utf-8") as f:
        for i, line in enumerate(f):
            d = json.loads(line)
            api[d.get("siren")] = (i, d.get("nature_juridique"), d.get("etat_administratif"))

    if limit:
        deliv = deliv[:limit]
    rows = []
    for r in deliv:
        siret, siren = clean_siret(r["SIRET"]), clean_siren(r["SIREN"])
        if not siret or not siren:
            continue
        e = etabs.get(siret, {})
        line, nat_jur, _ = api.get(siren, (None, None, None))
        n = {
            "siret": siret, "siren": siren, "_row_index": line if line is not None else -1,
            "legal_name": clean_str(r["Raison sociale"]), "trade_name": clean_str(r["Enseigne"]),
            "naf_label": clean_str(r["Activite"]), "naf_code": clean_str(r["Code NAF"]),
            "legal_form": clean_str(r["Forme juridique"]), "creation_date": parse_date(r["Date de creation"]),
            "employee_bracket": clean_str(r["Tranche effectif"]), "nature_juridique_code": clean_str(nat_jur),
            "is_headquarters": r["Siege social"] == "Oui",
            "address_line1": strip_cp_city(e.get("adresse") or r["Adresse"], r["Code postal"]),
            "postal_code": clean_str(r["Code postal"]), "city": clean_str(r["Ville"]),
            "latitude": clean_str(e.get("latitude")), "longitude": clean_str(e.get("longitude")),
            "first_name": clean_str(r["Prenom"]), "last_name": clean_str(r["Nom"]),
            "job_title": clean_str(r["Fonction"]), "personne_morale": clean_str(r["Contact (personne morale)"]),
            "_has_name": True, "_ids": {}, "_attrs": {},
        }
        # dialled phone
        n["_phones"] = []
        tel = clean_phone(r["Telephone"])
        if tel:
            label, rank, dialable = phone_source_rank(clean_str(r["Telephone source"]) or "unknown")
            n["_phones"].append((label, tel, is_surtaxe(tel), dialable and not is_surtaxe(tel), rank, {"column": "Telephone"}))
        n["phone_main"] = tel if (tel and n["_phones"][0][3]) else None
        for p in re.split(r"\s*[|;]\s*", r["Telephone piste (non confirme)"] or ""):
            cp_ = clean_phone(p)
            if cp_ and cp_ != tel:
                n["_phones"].append(("piste", cp_, is_surtaxe(cp_), False, None, {"column": "Telephone piste"}))
        # e-mail best pick
        n["_emails"] = []
        norm, verdict, ev = classify_email(r["Email"])
        n["email_address"] = None
        if norm and verdict == "candidate":
            fr = clean_str(r["Email verifie"]) or "non verifie"
            n["email_address"] = norm
            n["email_status"] = VERDICT_FR.get(fr, "unknown")
            n["email_source"] = clean_str(r["Email confiance"]) or "unknown"
            n["_emails"].append((n["email_source"], norm, n["email_status"], {"column": "Email", "confiance": n["email_source"]}))
        elif norm:
            n["_emails"].append(("deliverable", norm, "malformed", ev))
        # website best pick
        n["website_url"] = clean_str(r["Site web"])
        n["website_domain"] = domain_of(n["website_url"])
        n["website_confidence"] = clean_str(r["Site confiance"])
        # socials
        n["_socials"] = [(k.lower(), clean_str(r[k])) for k in ("Facebook", "Instagram", "LinkedIn") if clean_str(r[k])]
        n["_attrs"] = {k: v for k, v in {
            "procedure_collective": clean_str(r["Procedure collective"]),
            "source_contact": clean_str(r["Source contact"]),
            "page_contact": clean_str(r["Page contact"]),
            "autres_emails": clean_str(r["Autres emails"]),
            "telephone_surtaxe": clean_str(r["Telephone surtaxe"]),
            "anciennete_ans": clean_str(r["Anciennete (ans)"]),
        }.items() if v}
        rows.append(n)

    by_siret = {n["siret"]: n for n in rows}
    # website verdicts per (siret, domain)
    web_claims = []
    for v in verdicts:
        s = clean_siret(v["siret"])
        if s in by_siret and v["domain"]:
            web_claims.append((s, v["domain"].lower(), v["verdict"], v.get("ownership"),
                               {"reason": v.get("reason"), "ownership": v.get("ownership"),
                                "bakery_score": v.get("bakery_score"), "shop_url": v.get("shop_url"),
                                "contact_url": v.get("contact_url"), "url": v.get("url_recorded")},
                               parse_date(v.get("checked_at"))))
            n = by_siret[s]
            if n["website_domain"] and n["website_domain"] == v["domain"].lower():
                n["website_verdict"] = v["verdict"]
                n["website_checked_at"] = parse_date(v.get("checked_at"))
    # e-mail claims from the crawl + SMTP verdicts (invalid included)
    email_claims = []
    seen = set()
    for se in site_emails:
        s = clean_siret(se["siret"])
        norm, verdict, ev = classify_email(se["email"])
        if s in by_siret and norm and verdict == "candidate":
            vf = verified.get(norm)
            db_verdict = VERDICT_FR.get(vf["verdict"], "unknown") if vf else "unknown"
            src = f"site/{se.get('confiance') or 'faible'}"
            email_claims.append((s, norm, src, db_verdict, {"found_on": se.get("found_on"),
                                 "confirmation": se.get("confirmation"), "smtp": vf.get("detail") if vf else None}))
            seen.add((s, norm))
    # verified addresses that appear in the deliverable but not in site_emails
    for n in rows:
        for _, norm, _, _ in n["_emails"]:
            vf = verified.get(norm)
            if vf and (n["siret"], norm) not in seen:
                email_claims.append((n["siret"], norm, "verify/smtp", VERDICT_FR.get(vf["verdict"], "unknown"),
                                     {"smtp": vf.get("detail")}))
    # phone + social claims from the crawled sites
    site_claims = []
    for sc in site_contacts:
        s = clean_siret(sc["siret"])
        if s not in by_siret:
            continue
        conf = sc.get("confiance") or "faible"
        for p in [sc.get("phone")] + re.split(r"\s*[|;]\s*", sc.get("phones_autres") or ""):
            cp_ = clean_phone(p)
            if cp_:
                site_claims.append((s, "phone", phone_digits(cp_), cp_, f"site/{conf}", conf, None,
                                    conf == "confirme" and not is_surtaxe(cp_), is_surtaxe(cp_),
                                    PHONE_RANK.get(f"site/{conf}"), {"found_on": sc.get("found_on"), "domain": sc.get("domain")}))
        for k in ("facebook", "instagram", "linkedin"):
            if clean_str(sc.get(k)):
                site_claims.append((s, k, sc[k].strip(), sc[k].strip(), f"site/{conf}", conf, None, None, None, None,
                                    {"found_on": sc.get("found_on")}))
    n_unlinked_verified = sum(1 for em in verified if not any(em == c[1] for c in email_claims))
    return rows, web_claims, email_claims, site_claims, len(api), n_unlinked_verified


def describe(rows, web, emails, site_claims, n_api, unlinked):
    c = Counter()
    for n in rows:
        c["rows"] += 1
        c["phone_main"] += bool(n["phone_main"])
        c["phone_claims"] += len(n["_phones"])
        c["piste"] += sum(1 for p in n["_phones"] if p[0] == "piste")
        c["email_ships"] += bool(n["email_address"])
        c["website"] += bool(n["website_domain"])
        c["website_verdict"] += bool(n.get("website_verdict"))
        c["named"] += bool(n["last_name"])
        c["personne_morale"] += bool(n["personne_morale"] and not n["last_name"])
        c["latlon"] += bool(n["latitude"])
        c["socials"] += len(n["_socials"])
        c["liquidation"] += n["_attrs"].get("procedure_collective") == "Liquidation"
    ev = Counter(e[3] for e in emails)
    print(f"\n  deliverable rows {c['rows']:,} · API payloads {n_api:,}")
    for k in ("named", "personne_morale", "latlon", "phone_main", "phone_claims", "piste",
              "email_ships", "website", "website_verdict", "socials", "liquidation"):
        print(f"  {k:<16} {c[k]:>7,}")
    print(f"  website verdict claims {len(web):,} · e-mail claims {len(emails):,} by verdict {dict(ev)}"
          f" · site phone/social claims {len(site_claims):,} · verified e-mails without a SIRET link {unlinked}")


# ─── writers ─────────────────────────────────────────────────────────────────

def stage_register(conn, path: Path, file_hash: str, n_raw: int):
    with conn.cursor() as cur:
        cur.execute("SELECT id, row_count_imported FROM staging.source_files WHERE file_hash = %s", (file_hash,))
        row = cur.fetchone()
        if row:
            return row[0], ("done" if row[1] is not None else "resume")
        cur.execute("""INSERT INTO staging.source_files
                (file_name, file_hash, sector, pipeline_stage, notes, row_count_raw, imported_by, data_source, collected_at)
                VALUES (%s,%s,%s,'verified',%s,%s,%s,%s,%s) RETURNING id""",
                    (path.name, file_hash, SECTOR,
                     "Sector-2 deliverable (registry + M2 enrichment). raw rows = api_raw.jsonl payloads.",
                     n_raw, SCRIPT, "recherche-entreprises.api.gouv.fr + M2 enrichment (OSM, PJ, Places, sites, SMTP)",
                     COLLECTED))
        sfid = cur.fetchone()[0]
    conn.commit()
    return sfid, "new"


def stage_raw(conn, sfid) -> int:
    recs = []
    with open(CKPT / "api_raw.jsonl", encoding="utf-8") as f:
        for i, line in enumerate(f):
            recs.append((sfid, i, line.strip()))
    for start in range(0, len(recs), 2000):
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(cur, "INSERT INTO raw.ingest_rows (source_file_id, row_index, raw_json) "
                                                "VALUES %s ON CONFLICT DO NOTHING", recs[start:start + 2000], page_size=PAGE)
        conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw.ingest_rows WHERE source_file_id = %s", (sfid,))
        return cur.fetchone()[0]


def stage_companies(conn, sfid, rows) -> dict:
    by_siren = {}
    for n in rows:
        by_siren.setdefault(n["siren"], (n["siren"], n["legal_name"], n["trade_name"], n["naf_label"], n["naf_code"],
                                         n["legal_form"], n["employee_bracket"], n["creation_date"],
                                         n["nature_juridique_code"], "unqualified", sfid))
    stats = Counter()
    with conn.cursor() as cur:
        res = psycopg2.extras.execute_values(cur, """
            INSERT INTO staging.companies
                (siren, legal_name, trade_name, naf_label, naf_code, legal_form, employee_bracket, creation_date,
                 nature_juridique_code, qualification_status, source_file_id, sirene_etat, sirene_last_checked_at)
            VALUES %s
            ON CONFLICT (siren) DO UPDATE SET
                legal_name            = COALESCE(staging.companies.legal_name,            EXCLUDED.legal_name),
                trade_name            = COALESCE(staging.companies.trade_name,            EXCLUDED.trade_name),
                naf_label             = COALESCE(staging.companies.naf_label,             EXCLUDED.naf_label),
                naf_code              = COALESCE(staging.companies.naf_code,              EXCLUDED.naf_code),
                legal_form            = COALESCE(staging.companies.legal_form,            EXCLUDED.legal_form),
                employee_bracket      = COALESCE(staging.companies.employee_bracket,      EXCLUDED.employee_bracket),
                creation_date         = COALESCE(staging.companies.creation_date,         EXCLUDED.creation_date),
                nature_juridique_code = COALESCE(staging.companies.nature_juridique_code, EXCLUDED.nature_juridique_code),
                sirene_etat           = COALESCE(staging.companies.sirene_etat,           EXCLUDED.sirene_etat),
                sirene_last_checked_at= COALESCE(staging.companies.sirene_last_checked_at,EXCLUDED.sirene_last_checked_at)
            RETURNING siren, id, (xmax = 0) AS inserted""",
            [v + ("A", datetime.combine(COLLECTED, datetime.min.time())) for v in by_siren.values()],
            page_size=PAGE, fetch=True)
        ids = {}
        for siren, cid, inserted in res:
            ids[siren] = cid
            stats["inserted" if inserted else "merged"] += 1
    conn.commit()
    for n in rows:
        n["_company_id"] = ids[n["siren"]]
    return stats


def stage_sites(conn, sfid, rows) -> dict:
    stats = Counter()
    with conn.cursor() as cur:
        res = psycopg2.extras.execute_values(cur, """
            INSERT INTO staging.sites
                (siret, company_id, is_headquarters, is_active, address_line1, postal_code, city, department,
                 latitude, longitude, website_url, website_domain, website_verdict, website_confidence,
                 website_checked_at, source_file_id)
            VALUES %s
            ON CONFLICT (siret) DO UPDATE SET
                is_active          = COALESCE(staging.sites.is_active, EXCLUDED.is_active),
                latitude           = COALESCE(staging.sites.latitude,  EXCLUDED.latitude),
                longitude          = COALESCE(staging.sites.longitude, EXCLUDED.longitude),
                website_url        = COALESCE(staging.sites.website_url,        EXCLUDED.website_url),
                website_domain     = COALESCE(staging.sites.website_domain,     EXCLUDED.website_domain),
                website_verdict    = COALESCE(staging.sites.website_verdict,    EXCLUDED.website_verdict),
                website_confidence = COALESCE(staging.sites.website_confidence, EXCLUDED.website_confidence),
                website_checked_at = COALESCE(staging.sites.website_checked_at, EXCLUDED.website_checked_at)
            RETURNING siret, id, (xmax = 0) AS inserted""",
            [(n["siret"], n["_company_id"], n["is_headquarters"], True, n["address_line1"], n["postal_code"],
              n["city"], "13", n["latitude"], n["longitude"], n["website_url"], n["website_domain"],
              n.get("website_verdict"), n["website_confidence"], n.get("website_checked_at"), sfid) for n in rows],
            page_size=PAGE, fetch=True)
        ids = {}
        for siret, sid, inserted in res:
            ids[siret] = sid
            stats["inserted" if inserted else "merged"] += 1
    conn.commit()
    for n in rows:
        n["_site_id"] = ids[n["siret"]]
    return stats


def stage_contacts(conn, sfid, rows) -> dict:
    stats = Counter()
    site_ids = [n["_site_id"] for n in rows]
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
            sid = n["_site_id"]
            if n["last_name"]:
                full = f"{n['first_name']} {n['last_name']}".strip()
                first, last, title, src = n["first_name"], n["last_name"], n["job_title"], "rne_dirigeant"
            elif n["personne_morale"]:
                full, first, last, title, src = n["personne_morale"], None, None, "personne morale", "rne_dirigeant"
            else:
                full = None
            if full:
                if (sid, full) in named:
                    n["_contact_id"] = named[(sid, full)]
                    stats["named_reused"] += 1
                    fill.append((named[(sid, full)], n["phone_main"], first, last, title))
                else:
                    new_named.append((sid, n["_company_id"], first, last, full, title, n["phone_main"], False, "not_started", sfid, src))
                    named[(sid, full)] = None
            elif n["phone_main"] or n["email_address"]:
                if sid in generic:
                    n["_contact_id"] = generic[sid]
                    stats["generic_reused"] += 1
                    fill.append((generic[sid], n["phone_main"], None, None, None))
                else:
                    new_generic.append((sid, n["_company_id"], n["phone_main"], True, "not_started", sfid))
                    generic[sid] = None
        if new_named:
            res = psycopg2.extras.execute_values(cur, """
                INSERT INTO staging.contacts (site_id, company_id, first_name, last_name, full_name, job_title,
                    phone_main, is_generic_contact, enrichment_status, source_file_id, name_source)
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
                UPDATE staging.contacts c SET phone_main = COALESCE(c.phone_main, v.pm),
                       first_name = COALESCE(c.first_name, v.fn), last_name = COALESCE(c.last_name, v.ln),
                       job_title = COALESCE(c.job_title, v.jt)
                FROM (VALUES %s) AS v(id, pm, fn, ln, jt) WHERE c.id = v.id::uuid""", fill, page_size=PAGE)
            stats["filled_existing"] = len(fill)
        for n in rows:
            if n.get("_contact_id"):
                continue
            sid = n["_site_id"]
            full = (f"{n['first_name']} {n['last_name']}".strip() if n["last_name"] else n["personne_morale"])
            n["_contact_id"] = named.get((sid, full)) if full else generic.get(sid)
    conn.commit()
    return stats


def stage_emails(conn, sfid, rows) -> dict:
    recs, cids = [], set()
    for n in rows:
        if n["email_address"] and n.get("_contact_id"):
            verified_at = datetime.combine(COLLECTED, datetime.min.time()) if n["email_status"] != "unknown" else None
            recs.append((n["_contact_id"], n["email_address"], False, n["email_status"], verified_at,
                         "m2_s11 smtp" if verified_at else None, sfid, n["email_source"]))
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


def stage_points(conn, sfid, rows, web, emails, site_claims) -> dict:
    by_siret = {n["siret"]: n for n in rows}
    recs, stats = [], Counter()
    for n in rows:
        cid, sid, ctid = n["_company_id"], n["_site_id"], n.get("_contact_id")
        for label, canonical, surtaxe, dialable, rank, ev in n["_phones"]:
            recs.append((cid, sid, ctid, "phone", phone_digits(canonical), canonical, label,
                         "dialled" if dialable else "piste", None, dialable, surtaxe, rank, json_dumps(ev), COLLECTED, sfid))
            stats["phone"] += 1
        for src, norm, verdict, ev in n["_emails"]:
            recs.append((cid, sid, ctid, "email", norm, norm, src, None, verdict, None, None, None, json_dumps(ev), COLLECTED, sfid))
            stats["email_deliverable"] += 1
        for kind, url in n["_socials"]:
            recs.append((cid, sid, ctid, kind, url, url, "deliverable", None, None, None, None, None, None, COLLECTED, sfid))
            stats["social"] += 1
    for s, domain, verdict, ownership, ev, checked in web:
        n = by_siret[s]
        recs.append((n["_company_id"], n["_site_id"], None, "website", domain, ev.get("url"), "validator",
                     ownership, verdict, None, None, None, json_dumps(ev), checked or COLLECTED, sfid))
        stats["website"] += 1
    for s, norm, src, verdict, ev in emails:
        n = by_siret[s]
        recs.append((n["_company_id"], n["_site_id"], None, "email", norm, norm, src, None, verdict, None, None, None,
                     json_dumps(ev), COLLECTED, sfid))
        stats["email_" + verdict] += 1
    for s, kind, norm, raw, src, conf, verdict, dialable, surtaxe, rank, ev in site_claims:
        n = by_siret[s]
        recs.append((n["_company_id"], n["_site_id"], None, kind, norm, raw, src, conf, verdict, dialable, surtaxe, rank,
                     json_dumps(ev), COLLECTED, sfid))
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
    ap.add_argument("--version", default="v15")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    path = EXPORT_DIR / f"boulangerie_13_{args.version}.csv"
    rows, web, emails, site_claims, n_api, unlinked = build(args.version, args.limit)
    describe(rows, web, emails, site_claims, n_api, unlinked)
    if args.dry_run:
        print("\n  DRY RUN — nothing written.")
        return 0
    file_hash = sha256_file(path)
    conn = get_conn()
    try:
        sfid, state = stage_register(conn, path, file_hash, n_api)
        if state == "done":
            print(f"  [SKIP] Already imported (source_file_id={sfid}).")
            return 0
        print(f"  source_file_id={sfid} ({state})")
        print(f"  raw       written={stage_raw(conn, sfid):,}")
        print(f"  companies {dict(stage_companies(conn, sfid, rows))}")
        print(f"  members   written={stage_membership(conn, sfid, rows):,}")
        print(f"  sites     {dict(stage_sites(conn, sfid, rows))}")
        print(f"  contacts  {dict(stage_contacts(conn, sfid, rows))}")
        print(f"  emails    {dict(stage_emails(conn, sfid, rows))}")
        print(f"  points    {dict(stage_points(conn, sfid, rows, web, emails, site_claims))}")
        print(f"  attrs     companies={stage_attributes(conn, sfid, rows, SECTOR):,}")
        print(f"  bounces   {propagate_invalid_emails(conn)}   (proven-invalid claims applied to every staged copy)")
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
