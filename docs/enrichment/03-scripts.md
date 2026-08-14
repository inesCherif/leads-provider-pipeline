# 03 — Scripts: what each one does

All in `scripts/`. Every one is idempotent, resumable via a done-file, and
flushes per unit of work. Libraries have no step number; stages run in
numerical order but most are independent.

## Libraries

### `m2lib_search.py` — all search backends behind one call
`search(query, backend, max_results, ll)` → normalized rows
(`title,url,snippet,phone,address,lat,lon,rating,source_id`). Backends:
`serper_maps` (3 credits, phone+geo, `ll="@lat,lon,15z"` anchoring),
`serper_places` (1cr, NO phone), `serper_web`, `tavily`, `ddgs`.
Persistent quota counter (`search_quota.json`) charged BEFORE each request;
`QuotaExceeded` hard-stops a run — callers never swallow it. Retries 429/5xx;
401/403 → `SearchAuthError` (bad key). CLI: `--selftest` (offline, canned
payloads + counter round-trip), `--ping` (~4 credits), `--quota`.

### `m2lib_contact.py` — phone/social/e-mail hygiene
- `normalize_fr_phone` → `0X XX XX XX XX` or `""` (+33/0033/dots/tel: all
  handled).
- `is_surtaxe` → 089/081/082 premium-rate flag.
- `extract_phones` (loose — for SNIPPETS only) vs **`extract_phones_ctx`**
  (for web pages: requires a `tel:` href or a phone-word within 60 chars —
  full-page loose extraction measured 15–53% wrong because minified JS is
  full of 10-digit runs).
- `plausible_fr_number` — excludes ONLY unassigned ranges (01 1x/01 2x/03 0x/
  05 9x). Deliberately NOT a full numbering table: a fuller table written from
  memory rejected a real bakery's `09 52…` line.
- `extract_social` — fb/insta/linkedin page URLs; rejects share/login/plugin
  paths; most-frequent candidate wins.
- `is_third_party_email(email, crawled_domain)` — an address must share the
  crawled site's domain or be a consumer mailbox (gmail/orange/…). A different
  corporate domain on someone else's site is someone else's company (the
  oven-manufacturer incident: 320 supplier addresses were about to ship).
- CLI: `--selftest` — 50+ cases including every measured false positive.

## V1 pipeline (registry → first Excel)

| Script | Role |
|---|---|
| `m2_s1_acquire.py` | recherche-entreprises API → `api_raw.jsonl` (flush per page) |
| `m2_s2_transform.py` | JSONL → `etablissements.csv`, 1 row per dept-13 site. Activity guard (drops holdings/retail the legal-unit filter lets through), dirigeant ranking (gérant > président > exploitant; auditors excluded), liquidation flag, lat/lon kept on purpose |
| `m2_s3_export.py` | → `boulangerie_13_v1.xlsx` |
| `m2_s4_check.py` | V1 gate (reads the xlsx BACK) |

## Harvesters (write listings; never decide)

| Script | Source → output | Notes |
|---|---|---|
| `m2_s5_osm.py` | Overpass → `osm_listings.csv` | one HTTP call, `nwr` query, splits facebook-in-website |
| `m2_s6_pagesjaunes.py` | Pages Jaunes → `pj_listings.csv` | ✅ **WORKS via `--attach`** (816 listings, 2026-08-14). Attaches over CDP to the human's own Chrome, navigates per commune with `pj_location_slug()`, **clicks "Afficher le N°" to reveal phones** (they do not exist in the DOM before that), clicks pagination, dedupes on `listing_id`. Block detection lets result cards outrank any marker. CLI: `--attach --pilot N --dump-html --goto --start-url` |
| `m2_s13_places.py` | Serper /maps → `places_listings.csv` | Two modes. **Geo-sweep** (default): anchors a generic query at an uncovered target, marks everything within 300 m covered; 525 queries covered all 1,700 geocoded targets (1,575 credits). **`--named`**: asks for phoneless businesses by name (crowded-out-in-dense-streets fix; 35% incremental). Both dedupe by cid, credit ceiling `--max-pool-used` |
| `m2_s8_websites.py` | search APIs → `discovered_sites.csv` | website discovery per business (enseigne first — legal names often don't exist online). `--backend tavily|ddgs|serper_web`. Also mines snippets: geo-gated phone, fb/insta. ~90-entry AGGREGATORS blocklist |
| `m2_s16_serp_phones.py` | search snippets → `serp_snippets.csv` | phones of BLOCKED directories read from SERP snippets. Per-result geo gate; per-engine+domain provenance (feeds corroboration). `--backend`, `--pilot`. Built in V4, **first real run is V5** |

## Site crawl, patterns, verification

| Script | Role |
|---|---|
| `m2_s9_emails.py` | Crawls confirmed+discovered sites (fixed subpage list; `--deep` = sitemap.xml + the site's own contact-ish links, cap 10 pages, separate done-file). Extracts e-mails (obfuscation-aware), phones (`extract_phones_ctx`), socials. **Confirms site identity**: SIRET/SIREN on page → `confirme`; CP+name → `confirme`; else `faible`. Guards: >12 addresses = store list; >12 phones = keep NOTHING; shared domain = `reseau`; third-party filter; escape-artifact strip |
| `m2_s10_patterns.py` | For businesses with own-domain site + dirigeant name and no e-mail: mines patterns from our corpus (contact/info/bonjour, then `{p}.{n}`…), generates ≤6 candidates/domain, SMTP-verifies via the m1_s9g core. **Only `valid` ships** (catch-all domains answer 250 to everything → `risky` → discarded). Never expands aggregator domains. `--pilot`, `--dry-run` |
| `m2_s11_verify.py` | SMTP verdict for every harvested address (`valide/invalide/risque/non verifie`). Imports the m1_s9g core (catch-all probe, unknown-not-invalid, >20% dead abort). Consumer ISPs refuse residential IPs → those stay `non verifie` honestly. **Must FINISH before the export runs** (V4 timing incident) |

## Matcher, export, gates

| Script | Role |
|---|---|
| `m2_s7_match.py` | THE decision point. Listings → SIRET. Rules strongest-first: `siret_exact` → `geo_name` (≤150 m + token ratio ≥0.62) → `geo_only` (≤40 m unique) → `name_commune` → `name_fuzzy`. Ambiguity = rejection; no match without location agreement. Sources list at the top — adding a harvester = one tuple |
| `m2_s14_export_v3.py` | Builds the deliverable (BASENAME picks version). Phone precedence + corroboration + switchboard guard + piste routing; e-mail ranking incl. `pattern/verifie`; invalid withheld; third-party dropped. See [05-rules-and-gates.md](05-rules-and-gates.md) |
| `m2_s12_export_v2.py` | frozen V2 exporter (reproducibility) |
| `m2_s15_check_v3.py` / `m2_s17_check_v4.py` | Version gates: 19 checks, read the **xlsx back**. Copy-and-rebase pattern for each new version |

## Reused from sector 1

`m1_s9g_verify_emails.py` — the SMTP verification core (`classify_domain`,
`resolve_mx`, `probe_domain`, `KNOWN_BLOCKERS`, `MAX_DEAD_DOMAIN_RATE`,
`SMTP_WORKERS=8`). Import it, never copy it: it carries the incident fixes.
`m1_s8_export.ILLEGAL_XML` — control-character strip for openpyxl.
