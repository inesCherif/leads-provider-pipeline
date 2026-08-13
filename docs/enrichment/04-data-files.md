# 04 — Data files: every checkpoint and deliverable

Everything lives under `exports/boulangerie/` (gitignored — local only).
All CSVs: `;` delimiter, `utf-8-sig`. Append-and-flush; safe to read mid-run.

## `checkpoints/` — the working files

| File | Written by | Read by | Content |
|---|---|---|---|
| `api_raw.jsonl` | m2_s1 | m2_s2 | raw registry pages, immutable |
| `api_progress.json` | m2_s1 | m2_s1 | acquisition resume state |
| **`etablissements.csv`** | m2_s2 | almost everything | **the population**: 1,704 rows — siret, siren, raison_sociale, enseigne, forme juridique, NAF, adresse/CP/commune, prenom/nom/fonction (dirigeant), tranche_effectif, procedure_collective, latitude/longitude |
| `osm_listings.csv` | m2_s5 | m2_s7 | OSM bakeries: name, phone, email, website, facebook, addr, `ref:FR:SIRET`, lat/lon |
| `pj_listings.csv` | m2_s6 | m2_s7 | Pages Jaunes listings — **currently absent** (blocked) |
| `places_listings.csv` | m2_s13 (both modes) | m2_s7 | Google Maps listings: cid (`listing_id`), name, phone, website, address, CP/city parsed, lat/lon, rating, category, `anchor_siret` |
| `places_anchors_done.txt` / `places_named_done.txt` | m2_s13 | m2_s13 | resume (geo-sweep / named mode) |
| **`matched.csv`** | m2_s7 (rewritten whole each run) | m2_s9, m2_s14, everything | accepted (listing → SIRET) pairs with method, distance, phone/email/website/facebook payload, source label |
| `discovered_sites.csv` | m2_s8 | m2_s9, m2_s14, m2_s10 | candidate sites per business + snippet phone (`snippet_geo_ok`), fb/insta, backend |
| `sites_done.txt` | m2_s8 | m2_s8 | resume (`*_v2archive` = pre-API Brave era, superseded) |
| `site_emails.csv` | m2_s9 | m2_s11, m2_s14, m2_s10 | e-mails read off sites: email, found_on, `confirmation` (siret/siren/cp+nom/cp/none/reseau), `confiance` (confirme/faible) |
| `site_contacts.csv` | m2_s9 | m2_s14 | per (siret,domain): phone, surtaxe, phones_autres, facebook, instagram, linkedin, confirmation, confiance |
| `emails_done.txt` / `emails_done_deep.txt` | m2_s9 | m2_s9 | crawl resume (normal / `--deep`) |
| `serp_snippets.csv` | m2_s16 | m2_s14 (corroboration) | phone leads from SERP snippets: phone, surtaxe, engine, `snippet_domain`, geo_marker, query |
| `serp_done.txt` | m2_s16 | m2_s16 | resume (per-siret) |
| `pattern_candidates.csv` | m2_s10 | m2_s14 | **only SMTP-`valid`** generated addresses: candidate, pattern, status, tool |
| `patterns_done.txt` | m2_s10 | m2_s10 | resume (per-domain) |
| `verified_emails.csv` | m2_s11 (rewritten whole) | m2_s14 | verdict per address: valide / invalide / risque / non verifie. **Check its mtime is NEWER than the xlsx before shipping** |
| **`search_quota.json`** | m2lib_search | m2lib_search | the hard-stop credit counter: `{serper:{used,limit}, tavily:{used,limit,month}, ddgs:{used,limit,day}}` |
| `pj_chrome_profile/`, `pj_storage_state.json` | m2_s6 | m2_s6 | real-Chrome profile + cookie backup (holds the human-solved `cf_clearance`, which sadly is not portable) |

## Deliverables (versioned, frozen once shipped)

`boulangerie_13_v1.xlsx` → registry only.
`boulangerie_13_v2.xlsx/csv` → +105 phones (OSM era).
`boulangerie_13_v3.xlsx/csv` → +Serper/Tavily (590 phones).
**`boulangerie_13_v4.xlsx/csv` → current: 674 phones · 333 e-mails · 47.7% reachable.**

### V4 columns (32)

SIRET · SIREN · Raison sociale · Enseigne · Activite · Code NAF · Adresse ·
Code postal · Ville · Prenom · Nom · Fonction · **Telephone** (normalized
`0X XX XX XX XX`) · **Telephone source** (osm / serper_places / pagesjaunes /
site/confirme / corrobore(a+b)) · Telephone surtaxe (oui/blank) ·
**Telephone piste (non confirme)** (a lead, ~76%, only when Telephone empty) ·
Email · Email verifie (valide/risque/non verifie) · Email confiance
(confirme/faible/pattern/verifie) · Autres emails · Site web · Facebook ·
Instagram · LinkedIn · Source contact · Contact (personne morale) · Forme
juridique · Date de creation · Anciennete (ans) · Tranche effectif · Siege
social · Procedure collective

SIRET/SIREN/CP/dates/Telephone are forced to **text** in the xlsx (the
`4,47956E+13` Excel bug). xlsx is the client format — French Excel splits CSV
on `;` and a comma-CSV looks broken.
