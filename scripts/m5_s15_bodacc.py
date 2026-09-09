"""
M5-S15 — BODACC: legal-notice annonces for the population (alive proofs + death flags)
=====================================================================================
Maha's method, step one, done with the most reliable "annonce" there is: the
Bulletin officiel des annonces civiles et commerciales. Every registered
business leaves dated, linkable traces there — and the traces we care most
about are the ones that say a business is DEAD, because SIRENE lags them.

Source: the DILA open-data API (opendatasoft, free, no key, licence etalab-2.0)
    https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/annonces-commerciales/records

Two passes, both resumable through `bodacc_done.txt` keys:
  --mode siren   registre IN (<100 population SIRENs>) per query, all dates.
                 `registre` is a LIST (a sale lists buyer and seller), so the
                 SIREN's role in a sale is read from listepersonnes (buyer)
                 vs listeprecedent* (seller).
  --mode text    per dept, since --since, every annonce whose personne or
                 établissement text names a lodging (camping, gîte, chambres
                 d'hôtes, meublé de tourisme…). Finds businesses the NAF
                 filter of m5_s1 missed (farms with a gîte, 68.20 holdings
                 running a camping) — the seed for m5_s20 / phase 2.

Raw families (measured 2026-09-09): creation, immatriculation, modification,
dpc (dépôt des comptes), radiation, collective (procédures), vente.

Outputs (exports/hebergement/checkpoints/):
  bodacc_annonces.csv          one row per annonce × SIREN of ours it names
                               (or × the SIREN it carries, for text hits),
                               deduped on (id, siren), ';'-delimited
  bodacc_status_<dept>.csv     one row per population SIREN, derived from the
                               annonces file each run:
      statut      radié | liquidation judiciaire | redressement judiciaire |
                  plan de redressement (en cours) | sauvegarde |
                  procédure collective (autre) | fonds cédé | actif | aucune annonce
      alerte      a death event on a PERSONNE PHYSIQUE older than 24 months
                  (RCS radiation of an individual who may rent on as LMNP —
                  the hand-check found 2010–2021 radiations on establishments
                  SIRENE lists as active): recorded, not a status; SIRENE decides
      preuve_*    latest LIFE event (création / immatriculation / modification /
                  dépôt des comptes / acquisition d'un fonds) — the annonce
                  proof for the liveness score
      dernier_*   latest event of any kind (type · date · URL)
      dirigeants_bodacc, activite_bodacc — from the latest annonce naming them

The rule that decides drops is downstream (m5_s9): radié / liquidation
judiciaire / fonds cédé = dead; redressement / sauvegarde = flagged and
counted, Ines decides.

Usage:
    python scripts/m5_s15_bodacc.py --pilot 2                 # 2 SIREN chunks + 1 text term per dept
    python scripts/m5_s15_bodacc.py                            # both modes, resumes
    python scripts/m5_s15_bodacc.py --mode text --since 2020-01-01
"""

import argparse
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from m5_lib import CHECK_DIR, DEPARTEMENTS, read_csv, append_rows, load_done, mark_done  # noqa: E402

API = "https://bodacc-datadila.opendatasoft.com/api/explore/v2.1/catalog/datasets/annonces-commerciales/records"
OUT_PATH = CHECK_DIR / "bodacc_annonces.csv"
DONE_PATH = CHECK_DIR / "bodacc_done.txt"
PAGE = 100
CHUNK = 100                 # SIRENs per IN query (URL stays < 2 KB)
DELAY = 0.3
RETRIES = 4
TEXT_TERMS = ("camping", "gîte", "gite", "gîtes", "chambres d'hôtes", "chambre d'hôtes",
              "meublé de tourisme", "meublés de tourisme", "hébergement touristique",
              "village de vacances", "hôtellerie de plein air", "mobil-home", "caravaning")
LODGING_RE = re.compile(r"camping|caravan|g[iî]tes?\b|chambres?\s+d.h[oô]tes|meubl[ée]s?\s+de\s+tourisme|"
                        r"h[ée]bergements?\s+touristique|village\s+de\s+vacances|plein\s+air|mobil[- ]?home|"
                        r"location\s+saisonni|locations?\s+de\s+vacances", re.I)

FIELDS = ["id", "siren", "dept", "dateparution", "familleavis", "familleavis_lib", "typeavis_lib",
          "tribunal", "role", "denomination", "nom_commercial", "forme", "activite", "adresse",
          "cp", "ville", "administration", "jugement_nature", "jugement_date", "jugement_detail",
          "radiation_detail", "depot_type", "depot_cloture", "modif_detail", "vente_detail",
          "in_population", "lodging_text", "url"]
STATUS_FIELDS = ["siren", "n_annonces", "statut", "statut_date", "statut_detail", "alerte", "personne_physique",
                 "dernier_type", "dernier_date", "dernier_url",
                 "preuve_type", "preuve_date", "preuve_url",
                 "dirigeants_bodacc", "activite_bodacc", "checked_at"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("m5_s15")


# ---------------------------------------------------------------- http
def get(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "leads-provider-m5s15"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < RETRIES:
                time.sleep(3 * 2 ** (attempt - 1))
                continue
            sys.exit(f"HTTP {e.code} — {body}\n{url}")
        except Exception as exc:
            if attempt < RETRIES:
                time.sleep(3 * 2 ** (attempt - 1))
                continue
            sys.exit(f"{type(exc).__name__}: {exc} after {RETRIES} attempts — rerun to resume.")


def iter_records(where: str):
    offset = 0
    while True:
        d = get({"where": where, "limit": PAGE, "offset": offset, "order_by": "dateparution"})
        rows = d.get("results", [])
        yield from rows
        offset += len(rows)
        if len(rows) < PAGE or offset >= d.get("total_count", 0):
            return
        time.sleep(DELAY)


# ---------------------------------------------------------------- parsing
def js(v):
    """Nested fields arrive as JSON strings; tolerate dicts, lists, None."""
    if v is None or v == "":
        return {}
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return {"raw": str(v)}


def txt(v) -> str:
    """BODACC scalar fields are sometimes lists (several prénoms, several
    activities) — join them; never let a list reach a string join."""
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(txt(x) for x in v if x not in (None, ""))
    if isinstance(v, dict):
        return " ".join(txt(x) for x in v.values() if x not in (None, ""))
    return str(v).strip()


def first(d, key):
    """d[key] may be a dict or a list of dicts — return the list."""
    v = js(d).get(key) if isinstance(js(d), dict) else None
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def addr_str(a: dict) -> tuple[str, str, str]:
    if not isinstance(a, dict):
        return "", "", ""
    parts = [a.get("complGeographique"), a.get("numeroVoie"), a.get("typeVoie"), a.get("nomVoie"),
             a.get("localite")]
    street = " ".join(str(p) for p in parts if p).strip()
    return street, str(a.get("codePostal") or ""), str(a.get("ville") or "")


def siren_of(p: dict) -> str:
    n = (p.get("numeroImmatriculation") or {}).get("numeroIdentification") or ""
    return re.sub(r"\D", "", str(n))[:9]


def rows_from_record(r: dict, wanted: set | None) -> list[dict]:
    """One output row per SIREN of interest the annonce names."""
    regs = {re.sub(r"\D", "", str(x)) for x in (r.get("registre") or []) if x}
    regs = {x[:9] for x in regs if len(x) >= 9}
    personnes = first(r.get("listepersonnes"), "personne")
    precedents = (first(r.get("listeprecedentexploitant"), "precedentExploitant")
                  + first(r.get("listeprecedentproprietaire"), "precedentProprietaire"))
    etabs = first(r.get("listeetablissements"), "etablissement")
    jug = js(r.get("jugement")) or {}
    rad = js(r.get("radiationaurcs")) or {}
    dep = js(r.get("depot")) or {}
    mod = js(r.get("modificationsgenerales")) or {}
    acte = js(r.get("acte")) or {}
    vente = acte.get("vente") or {}

    buyers = {siren_of(p) for p in personnes if isinstance(p, dict)} - {""}
    sellers = {siren_of(p) for p in precedents if isinstance(p, dict)} - {""}
    all_sirens = regs | buyers | sellers
    targets = all_sirens & wanted if wanted is not None else all_sirens
    if not targets:
        return []

    blob = " ".join(json.dumps(x, ensure_ascii=False) for x in (personnes, etabs, precedents))
    e0 = etabs[0] if etabs and isinstance(etabs[0], dict) else {}
    out = []
    for s in sorted(targets):
        p = next((x for x in personnes if isinstance(x, dict) and siren_of(x) == s), None)
        role = "principal"
        if r.get("familleavis") == "vente":
            role = "acquéreur" if s in buyers else ("cédant" if s in sellers else "partie")
        if p is None:
            p = next((x for x in precedents if isinstance(x, dict) and siren_of(x) == s), None) or \
                (personnes[0] if personnes and isinstance(personnes[0], dict) else {})
        denom = txt(p.get("denomination")) or " ".join(x for x in (txt(p.get("prenom")), txt(p.get("nom"))) if x)
        street, cp, ville = addr_str(p.get("adresseSiegeSocial") or p.get("adresse") or e0.get("adresse") or {})
        activite = txt(p.get("activite")) or txt(e0.get("activite"))
        out.append({
            "id": r.get("id"), "siren": s, "dept": r.get("numerodepartement") or "",
            "dateparution": r.get("dateparution") or "", "familleavis": r.get("familleavis") or "",
            "familleavis_lib": r.get("familleavis_lib") or "", "typeavis_lib": r.get("typeavis_lib") or "",
            "tribunal": r.get("tribunal") or "", "role": role,
            "denomination": denom, "nom_commercial": txt(p.get("nomCommercial")),
            "forme": txt(p.get("formeJuridique")) or ("personne physique" if p.get("typePersonne") == "pp" else ""),
            "activite": activite, "adresse": street, "cp": cp, "ville": ville,
            "administration": txt(p.get("administration")),
            "jugement_nature": txt(jug.get("nature")), "jugement_date": txt(jug.get("date")),
            "jugement_detail": txt(jug.get("complementJugement"))[:300],
            "radiation_detail": txt(rad.get("commentaire")) or txt(rad.get("dateCessationActivite")),
            "depot_type": txt(dep.get("typeDepot")), "depot_cloture": txt(dep.get("dateCloture")),
            "modif_detail": txt(mod.get("descriptif"))[:300],
            "vente_detail": (txt(vente.get("categorieVente")) or txt(acte.get("descriptif")))[:300] if vente or r.get("familleavis") == "vente" else "",
            "in_population": int(wanted is not None and s in wanted),
            "lodging_text": int(bool(LODGING_RE.search(blob))),
            "url": r.get("url_complete") or f"https://www.bodacc.fr/pages/annonces-commerciales-detail/?q.id=id:{r.get('id')}",
        })
    return out


# ---------------------------------------------------------------- status
DEAD_LJ = re.compile(r"liquidation judiciaire|cl[oô]ture pour insuffisance|cl[oô]ture de la liquidation", re.I)
PLAN = re.compile(r"plan de redressement|plan de continuation|plan de sauvegarde", re.I)
RJ = re.compile(r"redressement judiciaire", re.I)
SV = re.compile(r"sauvegarde", re.I)
# A personne physique struck off the RCS (or liquidated) years ago can be
# renting a meublé today as a non-commercial LMNP with the same SIREN — the
# 2026-09-09 hand-check found radiations from 2010–2021 on establishments
# SIRENE lists as active. For individuals a death event older than this is an
# ALERT, not a status; SIRENE decides. A legal entity's radiation is a
# dissolution and stays a death whatever its date.
PP_DEATH_MAX_DAYS = 730
LIFE = {"creation": "création", "immatriculation": "immatriculation", "modification": "modification",
        "dpc": "dépôt des comptes"}
TYPE_FR = {**LIFE, "radiation": "radiation", "collective": "procédure collective", "vente": "vente / cession"}


def build_status(annonces: list[dict], sirens: set) -> list[dict]:
    from datetime import datetime, timedelta
    today = datetime.now().date()
    by = defaultdict(list)
    for a in annonces:
        if a["siren"] in sirens:
            by[a["siren"]].append(a)
    out = []
    for s in sorted(sirens):
        evs = sorted(by.get(s, []), key=lambda a: a["dateparution"])
        is_pp = any(a["forme"] == "personne physique" for a in evs)
        statut, sdate, sdetail = "actif", "", ""
        alertes = []
        if not evs:
            statut = "aucune annonce"

        def stale(a) -> bool:
            """pp death event older than PP_DEATH_MAX_DAYS -> alert, not status."""
            if not is_pp:
                return False
            try:
                d = datetime.strptime(a["dateparution"][:10], "%Y-%m-%d").date()
            except ValueError:
                return False
            return (today - d) > timedelta(days=PP_DEATH_MAX_DAYS)

        for a in evs:                                   # chronological: the last word wins
            f = a["familleavis"]
            nat = f"{a['jugement_nature']} {a['jugement_detail']}"
            jd = a["jugement_date"] or a["dateparution"]
            if f == "radiation":
                if stale(a):
                    alertes.append(f"radiation RCS ancienne {a['dateparution']} (personne physique)")
                else:
                    statut, sdate, sdetail = "radié", a["dateparution"], a["radiation_detail"]
            elif f == "collective":
                if DEAD_LJ.search(nat) and not PLAN.search(nat):
                    if stale(a):
                        alertes.append(f"liquidation judiciaire ancienne {jd} (personne physique)")
                    else:
                        statut, sdate, sdetail = "liquidation judiciaire", jd, a["jugement_nature"]
                elif PLAN.search(nat):
                    if statut != "liquidation judiciaire":
                        statut, sdate, sdetail = "plan de redressement (en cours)", jd, a["jugement_nature"]
                elif RJ.search(nat):
                    if statut != "liquidation judiciaire":
                        statut, sdate, sdetail = "redressement judiciaire", jd, a["jugement_nature"]
                elif SV.search(nat):
                    if statut not in ("liquidation judiciaire", "redressement judiciaire"):
                        statut, sdate, sdetail = "sauvegarde", jd, a["jugement_nature"]
                elif statut in ("actif", "aucune annonce"):
                    statut, sdate, sdetail = "procédure collective (autre)", jd, a["jugement_nature"]
            elif f == "vente" and a["role"] == "cédant":
                if stale(a):
                    alertes.append(f"fonds cédé {a['dateparution']} (personne physique)")
                else:
                    statut, sdate, sdetail = "fonds cédé", a["dateparution"], a["vente_detail"]
            elif f in LIFE or (f == "vente" and a["role"] == "acquéreur"):
                if statut in ("radié", "fonds cédé"):    # re-registration after a radiation: back to life
                    statut, sdate, sdetail = "actif", "", ""
        if alertes and not sdetail:
            sdetail = " ; ".join(alertes)
        life = [a for a in evs if a["familleavis"] in LIFE or (a["familleavis"] == "vente" and a["role"] == "acquéreur")]
        last = evs[-1] if evs else None
        lp = life[-1] if life else None
        named = [a for a in evs if a["administration"]]
        act = [a for a in evs if a["activite"]]
        out.append({
            "siren": s, "n_annonces": len(evs), "statut": statut, "statut_date": sdate, "statut_detail": sdetail[:200],
            "alerte": " ; ".join(alertes)[:200], "personne_physique": int(is_pp),
            "dernier_type": TYPE_FR.get(last["familleavis"], last["familleavis"]) if last else "",
            "dernier_date": last["dateparution"] if last else "", "dernier_url": last["url"] if last else "",
            "preuve_type": ("acquisition d'un fonds" if lp["familleavis"] == "vente" else LIFE[lp["familleavis"]]) if lp else "",
            "preuve_date": lp["dateparution"] if lp else "", "preuve_url": lp["url"] if lp else "",
            "dirigeants_bodacc": named[-1]["administration"][:300] if named else "",
            "activite_bodacc": act[-1]["activite"][:200] if act else "",
            "checked_at": datetime.now().strftime("%Y-%m-%d"),
        })
    return out


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--departements", default=",".join(DEPARTEMENTS))
    ap.add_argument("--mode", choices=["siren", "text", "both"], default="both")
    ap.add_argument("--since", default="2020-01-01", help="text mode: earliest dateparution")
    ap.add_argument("--pilot", type=int, default=0, help="N SIREN chunks and 1 text term per dept, then stop")
    args = ap.parse_args()
    depts = [d.strip() for d in args.departements.split(",") if d.strip()]

    pop: dict[str, set] = {}
    for d in depts:
        ops = read_csv(CHECK_DIR / f"operateurs_{d}.csv", delim=",")
        if not ops:
            sys.exit(f"operateurs_{d}.csv missing — run m5_s2_transform.py first.")
        pop[d] = {o["siren"] for o in ops if len(o.get("siren", "")) == 9}
    wanted = set().union(*pop.values())
    done = load_done(DONE_PATH)
    existing = {(a["id"], a["siren"]) for a in read_csv(OUT_PATH)}
    log.info(f"population SIRENs: {len(wanted)} ({ {d: len(s) for d, s in pop.items()} }); "
             f"annonces already on disk: {len(existing)}; done keys: {len(done)}")

    stats = Counter()

    def flush(rows: list[dict], key: str) -> None:
        new = [r for r in rows if (r["id"], r["siren"]) not in existing]
        for r in new:
            existing.add((r["id"], r["siren"]))
        append_rows(OUT_PATH, FIELDS, new)
        mark_done(DONE_PATH, key)
        stats["written"] += len(new)

    if args.mode in ("siren", "both"):
        sirens = sorted(wanted)
        chunks = [sirens[i:i + CHUNK] for i in range(0, len(sirens), CHUNK)]
        if args.pilot:
            chunks = chunks[:args.pilot]
        for i, ch in enumerate(chunks, 1):
            key = f"siren:{ch[0]}-{ch[-1]}"
            if key in done:
                stats["chunks skipped"] += 1
                continue
            where = "registre IN (" + ",".join(f'"{s}"' for s in ch) + ")"
            rows = []
            for rec in iter_records(where):
                rows.extend(rows_from_record(rec, wanted))
            flush(rows, key)
            stats["chunks done"] += 1
            log.info(f"  siren chunk {i}/{len(chunks)}: {len(rows)} rows (written total {stats['written']})")
            time.sleep(DELAY)

    if args.mode in ("text", "both"):
        terms = TEXT_TERMS[:1] if args.pilot else TEXT_TERMS
        for d in depts:
            for t in terms:
                key = f"text:{d}:{t}:{args.since}"
                if key in done:
                    stats["text queries skipped"] += 1
                    continue
                where = (f'numerodepartement="{d}" AND dateparution>="{args.since}" AND '
                         f'(search(listepersonnes,"{t}") OR search(listeetablissements,"{t}"))')
                rows = []
                for rec in iter_records(where):
                    for row in rows_from_record(rec, None):
                        row["in_population"] = int(row["siren"] in wanted)
                        rows.append(row)
                flush(rows, key)
                stats["text queries done"] += 1
                log.info(f"  text [{d}] '{t}': {len(rows)} rows, {sum(r['in_population'] for r in rows)} in population "
                         f"(written total {stats['written']})")
                time.sleep(DELAY)

    # derived status files, recomputed from disk every run
    annonces = read_csv(OUT_PATH)
    log.info("─" * 62)
    log.info(f"annonces on disk: {len(annonces)}  (+{stats['written']} this run)")
    for d in depts:
        st = build_status(annonces, pop[d])
        p = CHECK_DIR / f"bodacc_status_{d}.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            import csv
            w = csv.DictWriter(fh, fieldnames=STATUS_FIELDS, delimiter=";")
            w.writeheader()
            w.writerows(st)
        c = Counter(r["statut"] for r in st)
        recent = sum(1 for r in st if r["preuve_date"] >= "2024-09-09")
        log.info(f"[{d}] status for {len(st)} SIRENs -> {p.name}: {dict(c.most_common())}; "
                 f"life proof ≤ 24 months: {recent}")
    hors = {a["siren"] for a in annonces if a["in_population"] == "0" and a["lodging_text"] == "1"}
    log.info(f"text hits OUTSIDE the population with lodging words: {len(hors)} SIRENs (phase-2 seed)")


if __name__ == "__main__":
    main()
