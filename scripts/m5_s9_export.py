"""
M5-S9 — Merge every source, apply the precedence rules and the liveness score, write the full file
=================================================================================================
Input : checkpoints/operateurs_<dept>.csv      (m5_s2 — the registry population)
        checkpoints/matched_<dept>.csv         (m5_s8 — pj / osm / datatourisme / atout / france_travail / provider)
        checkpoints/sirene_etat_<dept>.csv     (m5_s13)
        checkpoints/bodacc_status_<dept>.csv   (m5_s15)
        checkpoints/site_contacts.csv + site_verdicts.csv   (m5_s4 — optional, later versions)
        checkpoints/search_hits.csv            (m5_s3 — optional)
        checkpoints/verified_emails.csv        (m5_s11 — optional)
Output: exports/hebergement/hebergement_<dept>_<version>.xlsx and .csv (every row, every claim)

Precedence (measured on M2/M3AG, kept; provider is a witness at 73 %):
  phone   osm > pagesjaunes > site/mentions_legales > site/confirme > corrobore(2 independent
          witnesses) > france_travail (owner-declared in a job offer) > google_panel.
          A provider-only number is a `piste`, never dialled. A provider number
          that agrees with a measured source is noted as confirmation.
  e-mail  site/confirme > site/probable > osm > france_travail > pagesjaunes >
          provider (déclaré) > snippet. `invalide` withheld; junk mailboxes and
          aggregator domains withheld.
  site    atout > datatourisme > osm > pagesjaunes > provider > france_travail > crawl.

Liveness (Maha's method): one point per independent family with a dated trace —
  emploi (France Travail offer ≤ 12 mo) · bodacc (création / modification / dépôt
  des comptes / acquisition ≤ 24 mo) · datatourisme (fiche mise à jour ≤ 12 mo) ·
  atout (classement ≤ 5 ans) · annuaire (OSM / Pages Jaunes listing) · creation
  (immatriculation ≤ 24 mo) · site (crawl valide, later versions).
  `preuves_activite` spells them out in French; `derniere_annonce` is the most
  recent BODACC / France Travail item as `type · date · URL`.
  Hard facts, never softened: SIRENE closed and BODACC radié / liquidation /
  fonds cédé are `mort=1`; redressement / sauvegarde / plan are `alerte`.

Usage:
    python scripts/m5_s9_export.py --departement 63 --version v0
"""

import argparse
import csv
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m1_s8_export import ILLEGAL_XML                       # noqa: E402
from m2lib_contact import normalize_fr_phone, is_surtaxe   # noqa: E402
from m5_lib import (CHECK_DIR, OUT_DIR, read_csv, name_tokens, root_domain,  # noqa: E402
                    is_aggregator, is_junk_witness, strong_tokens, JUNK_MAILBOX, is_social)

POP_COLUMNS = ["raisonSociale", "siret", "gerant", "telephone", "telephoneCommerciale",
               "codeNAF", "siteWebs", "categories", "productions",
               "adresse", "codePostal", "ville", "lat", "lon",
               "email", "numeroBio", "activites", "flag_hors_agri", "flag_hors_dept",
               "siren", "denomination_legale", "enseigne", "type_hebergement",
               "forme_juridique", "nature_juridique", "flag_public", "procedure_collective",
               "date_creation", "tranche_effectif", "est_siege", "prenom", "nom", "fonction",
               "source_population"]
COLUMNS = POP_COLUMNS + [
    "telephone_final", "source_telephone", "telephone_confirme_par", "telephone_piste",
    "email_final", "source_email", "email_statut", "emails_autres",
    "website_final", "source_website", "site_confiance",
    "type_final", "classement", "capacite", "emplacements", "labels",
    "statut_sirene", "bodacc_statut", "bodacc_statut_date", "bodacc_alerte",
    "bodacc_dernier_type", "bodacc_dernier_date", "bodacc_url",
    "bodacc_preuve_type", "bodacc_preuve_date", "dirigeant_bodacc",
    "dt_lastupdate", "atout_date", "ft_derniere_offre", "ft_intitule", "ft_url",
    "score_activite", "preuves_activite", "derniere_annonce", "mort", "alerte",
    "pj_phone", "pj_name", "pj_method", "osm_phone", "osm_email", "osm_website", "osm_name", "osm_method",
    "provider_phone", "provider_email", "provider_name", "provider_dirigeant",
    "ft_phone", "ft_email", "facebook",
]

PHONE_RANK = ["osm", "pagesjaunes", "site/mentions_legales", "site/confirme", "corrobore",
              "france_travail", "google_panel"]
EMAIL_RANK = ["site/confirme", "site/probable", "osm", "france_travail", "pagesjaunes",
              "provider", "site/non verifie", "snippet"]
SITE_RANK = ["atout", "datatourisme", "osm", "pagesjaunes", "provider", "france_travail", "crawl"]
DEAD = {"radié", "liquidation judiciaire", "fonds cédé"}
FLAGGED = {"redressement judiciaire", "sauvegarde", "plan de redressement (en cours)", "procédure collective (autre)"}

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m5_s9")
TODAY = date.today()


def clean(v) -> str:
    return ILLEGAL_XML.sub("", str(v if v is not None else ""))


def nphone(p: str) -> str:
    return normalize_fr_phone(p or "") or ""


def rank(order: list, key: str) -> int:
    return order.index(key) if key in order else len(order)


def to_date(s: str):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s[:10], fmt).date()
        except ValueError:
            continue
    return None


def mmyyyy(s: str) -> str:
    d = to_date(s)
    return d.strftime("%m/%Y") if d else ""


def detail_get(detail: str, key: str) -> str:
    m = re.search(rf"(?:^|;\s*){re.escape(key)}=([^;]*)", detail or "")
    return m.group(1).strip() if m else ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departement", default="63")
    ap.add_argument("--version", default="v0")
    args = ap.parse_args()
    dept = args.departement

    ops = read_csv(CHECK_DIR / f"operateurs_{dept}.csv", delim=",")
    if not ops:
        sys.exit(f"operateurs_{dept}.csv missing — run m5_s2_transform.py first.")
    matches = defaultdict(lambda: defaultdict(list))
    for m in read_csv(CHECK_DIR / f"matched_{dept}.csv"):
        matches[m["row_id"]][m["source"]].append(m)
    sirene = {r["siret"]: r for r in read_csv(CHECK_DIR / f"sirene_etat_{dept}.csv")}
    bodacc = {r["siren"]: r for r in read_csv(CHECK_DIR / f"bodacc_status_{dept}.csv")}
    site_contacts = defaultdict(list)
    for r in read_csv(CHECK_DIR / "site_contacts.csv"):
        if r.get("dept", dept) == dept:
            site_contacts[r["row_id"]].append(r)
    last_verdict = {}
    for r in read_csv(CHECK_DIR / "site_verdicts.csv"):
        if r.get("dept", dept) == dept:
            last_verdict[(r["row_id"], r["domain"])] = r
    site_verdicts = defaultdict(list)
    for r in last_verdict.values():
        if r["verdict"] == "valide":
            site_verdicts[r["row_id"]].append(r)
    hits = defaultdict(list)
    for r in read_csv(CHECK_DIR / "search_hits.csv"):
        hits[r["row_id"]].append(r)
    verified = {r["email"].lower(): r["verdict"] for r in read_csv(CHECK_DIR / "verified_emails.csv")}

    rows, stats = [], Counter()
    for op in ops:
        rid = op["siret"] or f"X{op['siren']}"
        ms = matches.get(rid, {})
        toks = name_tokens(op["raisonSociale"], op.get("denomination_legale", ""), op.get("gerant", ""))
        first = lambda src: (ms.get(src) or [{}])[0]
        pj, osm, dt, at, ft, pv = (first(s) for s in ("pagesjaunes", "osm", "datatourisme", "atout", "france_travail", "provider"))

        # ---------------- phones --------------------------------------------
        cands = []
        for src in ("osm", "pagesjaunes", "france_travail"):
            for m in ms.get(src, []):
                for p in (m.get("phone"), m.get("mobile")):
                    if nphone(p) and not is_surtaxe(nphone(p)):
                        cands.append((nphone(p), src))
        for c in site_contacts.get(rid, []):
            if nphone(c.get("phone")) and c.get("confiance") in ("confirme", "mentions_legales"):
                cands.append((nphone(c["phone"]), f"site/{c['confiance']}"))
        provider_phones = [nphone(p) for m in ms.get("provider", []) for p in (m.get("phone"), m.get("mobile")) if nphone(p)]
        witnesses = defaultdict(set)                     # one-witness claims
        for p in provider_phones:
            witnesses[p].add("provider")
        for h in hits.get(rid, []):
            if is_junk_witness(h.get("host", "")):
                continue
            for p in (h.get("phones") or "").split("|"):
                if nphone(p) and not is_surtaxe(nphone(p)):
                    witnesses[nphone(p)].add(root_domain(h["host"]))
        listed = {p for p, _ in cands}
        pistes, confirmed_by = [], []
        for p, ws in witnesses.items():
            if p in listed:
                confirmed_by.append(f"{p} ({'+'.join(sorted(ws))})")
                continue
            if len(ws) >= 2:
                cands.append((p, f"corrobore({'+'.join(sorted(ws)[:3])})"))
            else:
                pistes.append(f"{p} ({'fichier fournisseur' if 'provider' in ws else next(iter(ws))})")
        cands.sort(key=lambda x: rank(PHONE_RANK, x[1].split("(")[0]))
        tel_final, tel_src = (cands[0] if cands else ("", ""))
        tel_conf = ""
        if tel_final:
            ws = witnesses.get(tel_final, set())
            others = {s for _, s in cands if _ == tel_final and s != tel_src}
            agree = sorted(ws | others)
            if agree:
                tel_conf = ", ".join("fichier fournisseur" if a == "provider" else a for a in agree)

        # ---------------- e-mails -------------------------------------------
        ecands = []
        for c in site_contacts.get(rid, []):
            e = (c.get("email") or "").lower().strip()
            if e:
                ecands.append((e, f"site/{c.get('confiance') or 'non verifie'}"))
        for src in ("osm", "france_travail", "pagesjaunes", "provider"):
            for m in ms.get(src, []):
                e = (m.get("email") or "").lower().strip()
                if e:
                    ecands.append((e, src))
        for h in hits.get(rid, []):
            for e in (h.get("emails") or "").split("|"):
                if e.strip():
                    ecands.append((e.strip().lower(), "snippet"))
        strong = strong_tokens(toks)

        def named(e: str) -> bool:
            flat = e.replace(".", "").replace("-", "").replace("_", "").replace("@", "")
            return any(t.lower() in flat for t in strong)
        seen_e, uniq = set(), []
        for e, s in sorted(ecands, key=lambda x: rank(EMAIL_RANK, x[1])):
            if e in seen_e or "@" not in e:
                continue
            local, _, dom = e.partition("@")
            if set(re.split(r"[._\-+]", local)) & JUNK_MAILBOX or local in JUNK_MAILBOX:
                stats["e-mail withheld: junk mailbox"] += 1
                continue
            if verified.get(e) == "invalide":
                stats["e-mail withheld: invalide"] += 1
                continue
            if is_aggregator(dom) and dom != "gmail.com":
                stats["e-mail withheld: aggregator domain"] += 1
                continue
            if s in ("site/non verifie", "snippet") and not named(e):
                stats["e-mail withheld: one witness, no name in address"] += 1
                continue
            seen_e.add(e)
            uniq.append((e, s))
        uniq.sort(key=lambda x: (rank(EMAIL_RANK, x[1]), 0 if named(x[0]) else 1))
        email_final, email_src = (uniq[0] if uniq else ("", ""))
        email_statut = verified.get(email_final, "") if email_final else ""
        if email_final and not email_statut:
            email_statut = "declare" if email_src in ("provider", "france_travail") else "non verifie"
        emails_autres = "|".join(e for e, _ in uniq[1:4])

        # ---------------- sites ---------------------------------------------
        scands = []
        for src in ("atout", "datatourisme", "osm", "pagesjaunes", "provider", "france_travail"):
            for m in ms.get(src, []):
                w = (m.get("website") or "").strip()
                if w and not is_aggregator(w) and not is_social(w):
                    scands.append((w if w.startswith("http") else f"http://{w}", src))
        for v in site_verdicts.get(rid, []):
            scands.append((f"https://{v['domain']}", "crawl"))
        scands.sort(key=lambda x: rank(SITE_RANK, x[1]))
        site_final, site_src = (scands[0] if scands else ("", ""))
        conf = ""
        for c in site_contacts.get(rid, []):
            if site_final and c["domain"] in site_final:
                conf = c.get("confiance") or conf
        fb = ""
        for m in ms.get("osm", []):
            fb = fb or (m.get("facebook") or "")

        # ---------------- type / capacity / labels ---------------------------
        type_final = op["type_hebergement"]
        if at:
            type_final = detail_get(at["detail"], "type") or type_final
        elif dt and detail_get(dt["detail"], "type") not in ("", "autre hébergement"):
            type_final = detail_get(dt["detail"], "type")
        elif osm and detail_get(osm["detail"], "kind") in ("camping", "chambres d'hôtes"):
            type_final = detail_get(osm["detail"], "kind")
        if type_final in ("gîte-meublé",) and op["type_hebergement"] in ("gîte", "meublé"):
            type_final = op["type_hebergement"]
        classement = detail_get(at["detail"], "classement") if at else ""
        capacite = (detail_get(at["detail"], "capacite") if at else "") or (detail_get(osm["detail"], "capacity") if osm else "")
        emplacements = detail_get(at["detail"], "emplacements") if at else ""
        labels = detail_get(dt["detail"], "classement") if dt else ""
        if not classement and osm and detail_get(osm["detail"], "stars"):
            classement = f"{detail_get(osm['detail'], 'stars')} étoiles (OSM)"

        # ---------------- liveness -------------------------------------------
        e = sirene.get(op["siret"], {})
        if len(op["siret"]) != 14:
            statut_sirene = "SIRET inconnu"
        elif not e or not e.get("etat_ul"):
            statut_sirene = "non retrouvé au registre"
        elif e["etat_ul"] == "C" or e["etat_etab"] == "F":
            statut_sirene = "fermé"
        else:
            statut_sirene = "actif (liquidateur nommé)" if "liquidateur" in (e.get("note") or "") else "actif"
        b = bodacc.get(op["siren"], {})
        b_statut = b.get("statut", "") or "aucune annonce"
        mort = int(statut_sirene == "fermé" or b_statut in DEAD)
        alertes = []
        if b_statut in FLAGGED:
            alertes.append(f"BODACC : {b_statut} ({b.get('statut_date','')})")
        if b.get("alerte"):
            alertes.append(b["alerte"])
        if op["procedure_collective"]:
            alertes.append("liquidateur parmi les dirigeants (registre)")
        if op["flag_public"] == "1":
            alertes.append("opérateur public (phase 2)")

        preuves, annonces = [], []      # (label), (date, text)
        ft_date = ft.get("date") if ft else ""
        if ft and to_date(ft_date) and TODAY - to_date(ft_date) <= timedelta(days=365):
            preuves.append(f"offre d'emploi France Travail ({mmyyyy(ft_date)})")
            annonces.append((to_date(ft_date), f"offre d'emploi · {to_date(ft_date).strftime('%d/%m/%Y')} · {ft.get('listing_url','')}"))
        bp = to_date(b.get("preuve_date", ""))
        if bp and TODAY - bp <= timedelta(days=730):
            preuves.append(f"{b['preuve_type']} BODACC ({bp.strftime('%m/%Y')})")
        bl = to_date(b.get("dernier_date", ""))
        if bl:
            annonces.append((bl, f"{b.get('dernier_type','')} · {bl.strftime('%d/%m/%Y')} · {b.get('dernier_url','')}"))
        dtd = to_date(dt.get("date", "")) if dt else None
        if dtd and TODAY - dtd <= timedelta(days=365):
            preuves.append(f"fiche office de tourisme mise à jour {dtd.strftime('%m/%Y')}")
        atd = to_date(at.get("date", "")) if at else None
        if at and ((atd and TODAY - atd <= timedelta(days=5 * 365 + 2)) or detail_get(at["detail"], "proroge").lower().startswith("o")):
            preuves.append(f"classement Atout France {classement} ({atd.year if atd else ''})".replace(" ()", ""))
        if osm or pj:
            preuves.append("fiche " + " + ".join(x for x, ok in (("OpenStreetMap", bool(osm)), ("Pages Jaunes", bool(pj))) if ok))
        cd = to_date(op.get("date_creation", ""))
        if cd and TODAY - cd <= timedelta(days=730):
            preuves.append(f"immatriculation récente ({cd.strftime('%m/%Y')})")
        if site_verdicts.get(rid):
            preuves.append("site web actif")
        annonces.sort(key=lambda x: x[0], reverse=True)

        row = {c: clean(op.get(c, "")) for c in POP_COLUMNS}
        row.update({
            "telephone_final": tel_final, "source_telephone": tel_src, "telephone_confirme_par": tel_conf,
            "telephone_piste": " | ".join(pistes[:3]),
            "email_final": email_final, "source_email": email_src, "email_statut": email_statut,
            "emails_autres": emails_autres,
            "website_final": clean(site_final), "source_website": site_src, "site_confiance": conf,
            "type_final": type_final, "classement": classement, "capacite": capacite,
            "emplacements": emplacements, "labels": clean(labels),
            "statut_sirene": statut_sirene, "bodacc_statut": b_statut, "bodacc_statut_date": b.get("statut_date", ""),
            "bodacc_alerte": b.get("alerte", ""), "bodacc_dernier_type": b.get("dernier_type", ""),
            "bodacc_dernier_date": b.get("dernier_date", ""), "bodacc_url": b.get("dernier_url", ""),
            "bodacc_preuve_type": b.get("preuve_type", ""), "bodacc_preuve_date": b.get("preuve_date", ""),
            "dirigeant_bodacc": clean(b.get("dirigeants_bodacc", "")),
            "dt_lastupdate": dt.get("date", "") if dt else "", "atout_date": at.get("date", "") if at else "",
            "ft_derniere_offre": ft_date or "", "ft_intitule": clean(ft.get("detail", "")) if ft else "",
            "ft_url": ft.get("listing_url", "") if ft else "",
            "score_activite": len(preuves), "preuves_activite": " · ".join(preuves),
            "derniere_annonce": annonces[0][1] if annonces else "", "mort": mort, "alerte": " ; ".join(alertes),
            "pj_phone": pj.get("phone", ""), "pj_name": clean(pj.get("listing_name", "")), "pj_method": pj.get("method", ""),
            "osm_phone": osm.get("phone", ""), "osm_email": osm.get("email", ""), "osm_website": osm.get("website", ""),
            "osm_name": clean(osm.get("listing_name", "")), "osm_method": osm.get("method", ""),
            "provider_phone": pv.get("phone", ""), "provider_email": pv.get("email", ""),
            "provider_name": clean(pv.get("listing_name", "")), "provider_dirigeant": clean(detail_get(pv.get("detail", ""), "dirigeant")),
            "ft_phone": ft.get("phone", "") or ft.get("mobile", ""), "ft_email": ft.get("email", ""), "facebook": fb,
        })
        rows.append(row)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUT_DIR / f"hebergement_{dept}_{args.version}.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    from openpyxl import Workbook
    xlsx_path = OUT_DIR / f"hebergement_{dept}_{args.version}.xlsx"
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Hebergement")
    ws.append(COLUMNS)
    for r in rows:
        ws.append([str(r[c]) if c in ("siret", "siren", "codePostal") and r[c] else r[c] for c in COLUMNS])
    wb.save(xlsx_path)

    n = len(rows)
    live = [r for r in rows if not r["mort"]]
    tel = sum(1 for r in live if r["telephone_final"])
    log.info("─" * 62)
    log.info(f"[{dept}] {n} rows -> {xlsx_path.name} + .csv   (mort={n-len(live)}: SIRENE fermé / BODACC radié-liquidé-cédé)")
    log.info(f"  telephone_final   {tel:>5}  by source {dict(Counter(r['source_telephone'].split('(')[0] for r in live if r['telephone_final']))}")
    log.info(f"  confirmed by 2nd  {sum(1 for r in live if r['telephone_confirme_par']):>5}   pistes (provider-only etc.) {sum(1 for r in live if r['telephone_piste'] and not r['telephone_final'])}")
    log.info(f"  email_final       {sum(1 for r in live if r['email_final']):>5}  by source {dict(Counter(r['source_email'] for r in live if r['email_final']))}")
    log.info(f"  website_final     {sum(1 for r in live if r['website_final']):>5}  by source {dict(Counter(r['source_website'] for r in live if r['website_final']))}")
    log.info(f"  score_activite    {dict(sorted(Counter(r['score_activite'] for r in live).items()))}")
    log.info(f"  SÛR candidates    {sum(1 for r in live if r['telephone_final'] and r['score_activite'] >= 2 and r['statut_sirene'].startswith('actif') and not r['alerte']):>5}  (phone + ≥2 proofs + SIRENE actif + no alert)")
    log.info(f"  type_final        {dict(Counter(r['type_final'] for r in live).most_common())}")
    for k, v in stats.most_common():
        log.info(f"  {k:<44} {v}")


if __name__ == "__main__":
    main()
