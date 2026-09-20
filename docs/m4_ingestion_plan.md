# M4 — Multi-sector ingestion into Supabase (5 new provider files + boulangerie V15 + agriculteurs bio 63/03)

> **Note (handover, 2026-09-20).** This is a reference document kept as it was written.
> It sometimes cites working notes that are not part of this repository (per-sector work
> logs `*_progress.md`, `implementation_plan_leads.md`, session handoffs). The current
> commands are in `docs/RUNBOOK.md`; the rules and conventions in `CLAUDE.md`.

## Context

Mehdi dropped 5 new Excel files into `Data Globale 05 juillet 2026/` on 2026-09-07 and wants them
cleaned, deduplicated and loaded into Supabase. The two file-only deliverables we built
(boulangerie dept 13 V15, 1,704 rows; agriculteurs bio dept 63 V2 + 03 V1, 2,189 rows) have never
been loaded either — they were DB-free by design after three pooler data-loss incidents.

Ines's question: is the schema/pipeline robust enough to hold every sector in one place, or does
it need an upgrade first? **Measured answer: the identity core holds (source_files → companies →
sites → contacts → emails), but four things are single-sector by construction and must change
BEFORE loading, not after** — the same "refactor first" lesson CLAUDE.md records for
`sector_rules.py`. Loading first and migrating later reproduces the "untangle two sectors from one
column" problem one table deeper.

### What the audit found (verified against code, files and the DB)

1. **`m1_s3_ingest.py` cannot run today.** Its column contract maps `dep` for
   `Copie de agriculteurs total.xlsx`, but that file's 5th header is blank (`Unnamed: 4`, verified
   with calamine — the same blank department column recurs in 3 of the 5 new files). The contract
   check runs *before* the hash-skip, so the script raises `COLUMN CONTRACT VIOLATION` on file A
   and never reaches a new file. It also explains the 93.4% NULL `sites.department` (migration
   006 blamed the source); harmless, the views derive `department_code` from the postal code.
2. **Sector lives only on `staging.source_files.sector`** (nullable TEXT, no CHECK). Companies,
   sites, contacts, emails and all public views have no sector. `ON CONFLICT (siren) DO UPDATE`
   never updates `source_file_id`, so a business present in two sectors' files keeps the first
   sector's attribution and the second sector's qualification pass never sees it. This is not
   hypothetical: **336 SIRETs of the new tourism file are already in the agriculture base**
   (farm stays), and 5 bakery SIRETs are.
3. **Downstream is hard-coded to agriculture:** `m1_s8_export.py` has `SECTOR_SLUG="agriculture"`
   and no sector predicate (a second sector would be exported into the agriculture file);
   `check_data_quality.py` checks 7 (deliverable 80k–100k), 8 (dup rate 3–10% global) and 13
   (`rule_version` uniform) fail by construction with two sectors; `m1_s5b_dedup.py` is global
   (two no-SIREN businesses with the same name + postal code in different sectors would merge).
4. **No home for multi-witness contact data.** Contacts hold 2 phone slots, no provenance. The
   M2/M3AG deliverables carry per-phone source and corroboration labels, non-dialable `piste`
   numbers, e-mail source/confidence, website validation verdicts (1,332 + 935 (SIRET,domain)
   verdicts), social links, `numeroBio`, bio certificates, plus 135 + 51 **proven-invalid
   e-mails deliberately withheld from the files** that the DB must remember. COALESCE
   first-writer-wins is the opposite of best-source-wins.
5. **Ingest robustness:** bare `psycopg2.connect` (no keepalives/retry), one transaction per file
   committed at the end, no CLI, no-SIRET rows are plain INSERTs with no idempotency, contacts
   have no unique key. `raw.ingest_rows` (full JSON per row) is the safety net and stays.
6. **Supabase project is PAUSED** — every MCP call timed out. Ines restores it from the dashboard.

### The 5 new files, measured (all read with every column as text)

| file | rows | sector | identifier | e-mail | phone | verdict |
|---|---|---|---|---|---|---|
| `data_finale sans croisement` | **50,849** | **tourism / hospitality** — 19 provider sources (`SOURCE_FILE`): chambres d'hôtes 14.5k, gîtes 10k, hôtels 8.5k, résidences 7.2k, **campings 5.6k**, centres équestres 1.2k; NAF 5520Z/5510Z/5530Z/6820A/B… | SIRET 71.9% (36,547 × 14 digits, **100% Luhn-valid**); 14,175 rows are the placeholder `0.0`; 126 truncated (13/12 digits, recoverable by zero-padding) | 66.7%, **1,181 glued domains** (`@gmail.coml.com`, `@orange.frfr`) that pass a naive regex | 100%, `33XXXXXXXXX`; 390 premium 08xx | **Highest quality file. "Sans croisement" = not yet cross-matched against the base**, not a multi-sector union (5 SIRETs shared with boulang) |
| `boulang_patisserie` | 10,685 | bakeries/pastry, nationwide (dept 13: 920 rows); 3 provider sources; **contaminated** (NAF 6820B SCIs, 0161Z farms, 5610C restaurants — ACTIVITE is a concatenation of directory categories) | `siren_enrichi` **100%** (20 × 8 digits truncated) — the key; `SIRET` 91.2%; `siret_enrichi` 8.8% | 29.1%; **508 addresses contain a space** (`@systeme u.fr`), 86 glued | 100%; 433 bare 9-digit | Same sector key as our dept-13 registry work → two sources, one sector; SIREN overlap with V15 is the cross-check |
| `base imprimerie scrapé` | 9,182 (3 empty sheets) | printing shops (ACTIVITE = `\xa0Imprimerie` on every row, NBSP prefix) | **none** (SIRET column empty) | **none** | 100%, stored as float 9-digit; 6.5% shared switchboards; 41 premium | **City shifted into ADRESSE1 on 5,020 rows** (ADRESSE1/VILLE mutually exclusive → lossless COALESCE); no street anywhere |
| `Camping` | 890 | campings (ACTIVITE empty; inferred from names) | **none** | **none** | 100% | **CP wrong in 26% / DEP wrong in 21% (phone fragments)**, 41.5% rows with irreversible `ï¿½` mojibake, **434 of 890 phones already in data_finale**; col B = SOCIETE uppercased. Net new value ≈ 300–400 rows. **Recommend: do not load** |
| `pagesjaunes_viticulteur_gironde` | 2,534 → **~1,630** | wine growers, Gironde (PJ scrape, 9 cols, own schema) | **none**; `detailUrl` PJ id (187 rows carry the placeholder `pagesjaunes.fr#`) | **none** | 97%, clean `0X XX XX XX XX` | **896 exact duplicate rows (35%)**; accents preserved (base is ASCII-folded → fold before matching) |

Cross-file: the four non-camping files are mutually disjoint. Against the base: tourism ∩
agriculture = 336 SIRETs / 264 e-mails; boulang ∩ agriculture = 10.
The 9 call-centre columns (`DATE, HEURE, MAIL_NOMMINATIF, CALL_RESULT_TERM, REFUSAL_REASON_TERM,
type_refus, type_barrage, type_rappel, PHONE_NUMBER1`) are **empty in every file** → not
modelled, kept in `raw_json` only. `TELEPHONE` = `PHONE_NUMBER` byte-identical (one phone).

### The two deliverables (for the loaders)

| | boulangerie_13_v15.csv | agriculteurs_63_v2 / 03_v1.csv |
|---|---|---|
| sep / enc | `;` utf-8-sig, 34 cols | `,` utf-8-sig, 45 cols |
| key | **SIRET 100% unique** (1,594 SIREN → 110 sibling établissements) | **`numeroBio` 100% unique**; SIRET 96.6/97.5%, **50 rows share a SIRET** (two bio operators, one établissement), 66 rows no SIRET, 2 SIRETs of 13 chars; **no SIREN column** |
| immutable raw | `checkpoints/api_raw.jsonl` 3,825 recherche-entreprises payloads (dirigeants[], etat_administratif, lat/lon, nature_juridique code) | `checkpoints/agencebio_{63,03}.jsonl` (certificats[], productions[] with AB state, adressesOperateurs[], venteAnnuaire{}) |
| provenance only in checkpoints | `site_verdicts.csv` 1,332; `verified_emails.csv` 505 incl. **135 invalide**; `etablissements.csv` (lat/lon) | `site_verdicts.csv` 935; `verified_emails.csv` 305 incl. 51 invalide |
| vocabularies | `Telephone source` ∈ `PHONE_RANK` (`m2_s14_export_v3.py:172`) + `corrobore(a+b)`; `Email verifie` ∈ {valide, non verifie, risque}; `Site confiance` ∈ {confirme, probable, non verifie} | `source_telephone`, `email_statut` ∈ {declare, valide, non verifie, risque}, `telephone_piste` (`num (host)` ×3), `flag_hors_agri` |

---

## Decisions taken by Ines (2026-09-07)

- **Camping.xlsx is skipped.** Campings come from `data_finale` (5,592 rows). Mehdi gets the
  numbers (no SIRET, no e-mail, CP wrong 26%, mojibake 41%, 49% already in data_finale).
- **`data_finale` = one sector `tourisme`**; sub-scope by NAF at export time.
- **Deliverable loaders load best picks + every claim + the e-mail/site verdicts** (not the
  full checkpoint audit trail).
- **Pacing: one step per turn** — each step explained, run, committed, pushed, reported, then
  wait for Ines before the next (her standing rule; nothing writes to the DB unannounced).

## Design decisions (recommended; ⚖ = still Ines's call, asked at the step where it matters)

**D1. Keep the core schema; extend additively (migration 018).** Only `ADD COLUMN IF NOT EXISTS`
/ `CREATE TABLE IF NOT EXISTS`; nothing rewritten; every step reversible.

**D2. Sector = membership, not identity.** New `staging.company_sources (company_id,
source_file_id, row_index, UNIQUE(company_id, source_file_id, row_index))`, written for **every**
ingested row, SIREN path and fallback path alike. A farm-stay present in agriculture and tourism
has two rows. `m1_s5_qualify.py`'s scope becomes `EXISTS (company_sources ⋈ source_files WHERE
sector = ANY(source_sectors))` instead of `co.source_file_id`. Backfill from
`companies.source_file_id` for the 128k existing rows (row_index NULL → `-1`). A company in two
sectors keeps ONE `qualification_status` (the last sector run wins) — a new quality check counts
them so the overlap is visible; a per-sector verdict table is the next step only if that count is
material (measure first).

**D3. One multi-witness table, `staging.contact_points`**, instead of more phone slots:
`(id, company_id NOT NULL, site_id, contact_id, kind CHECK IN ('phone','email','website',
'facebook','instagram','linkedin'), value_norm NOT NULL, value_raw, source NOT NULL, confidence,
verdict, is_dialable BOOL, is_surtaxe BOOL, rank_hint INT, evidence JSONB, observed_at DATE,
source_file_id NOT NULL, UNIQUE (company_id, kind, value_norm, source))`.
Every phone/e-mail/site/social from every source lands here with provenance (`source` ∈
`client_file:TELEPHONE`, `pagesjaunes`, `osm`, `agencebio`, `site/confirme`, `corrobore`…;
`evidence` = witnesses, method, distance_m). `contacts.phone_main` / `staging.emails` /
`sites.website_domain` stay the **best-pick projection** written by the loaders using the measured
ranks. `piste` numbers and premium-rate numbers: `is_dialable=false`, never `phone_main`.
Malformed e-mails (glued domain, space) go here with `verdict='malformed'` and never into
`staging.emails`, which ships.

**D4. `staging.company_identifiers (company_id, id_type, id_value, source_file_id,
UNIQUE(id_type,id_value))`** — `numero_bio`, `pj_listing_id`, `file_row` (`<file_hash>:<row_index>`).
The idempotency key for businesses without a SIRET (imprimerie 9,182, viticulteurs, camping,
14k tourism rows, 66 agriculteurs) and the correct model for two bio operators on one SIRET (one
company, two identifiers).

**D5. `staging.company_attributes (company_id, sector, attrs JSONB, source_file_id,
PRIMARY KEY(company_id, sector))`** for the sector-specific long tail: bio certificates + AB state,
productions, sales channels, provider `EFFECTIF` as written, `SOURCE_FILE`, `flag_hors_agri`,
`procedure_collective`. Queryable via MCP, no per-sector columns.

**D6. Small columns:** `sites.address_line2`; `companies.naf_code_source CHAR(6)` (the file's NAF —
never into SIRENE-owned `naf_code`, never into regex-matched `naf_label`);
`companies.nature_juridique_code CHAR(4)` (flagged in migration 016); `emails.source TEXT`;
`sites.website_url`, `website_verdict`, `website_confidence`, `website_checked_at`.
Agence Bio `declare` → `verification_status='unknown'` + `source='agencebio'` (CHECK unchanged).

**D7. Views gain sector (migration 019):** `v_qualified_contacts` → `v_deliverable_businesses` →
`v_enrichment_queue` expose `sectors TEXT[]` + `primary_sector`, appended last so
`CREATE OR REPLACE VIEW` stays legal; new `public.v_sector_summary`.

**D8. Sector keys / `source_sectors` in `config/sector_rules.py`** (each `rule_version <key>-v1`;
`include`/`exclude` regexes are mandatory keys, so sectors without a tier-2 rule get a
never-matching regex): ⚖ NAF scopes are proposals for Ines/Sam.
- `boulangerie` — sources `("boulangerie",)` = Mehdi's file **and** the dept-13 registry
  deliverable; NAF `10.71`, `10.72`; hard-exclude `68.20`, `01.`, `56.` (the measured contamination).
- `imprimerie` — NAF `18.1`; the file has no NAF → tier 2 on label `imprim`.
- `viticulture` — NAF `01.21Z`, `11.02`; tier 2 on `viticult|vin|château|chateau|cave`.
- `tourisme` — one sector for `data_finale` (decided): NAF `55.` tier 1; rescue `93.1`, `85.51Z`
  (equestrian), `68.20A/B` only with a hospitality label; sub-scope by NAF at export (`--naf`).
- `agriculteurs_bio` — sources `("agriculteurs_bio",)`, deliberately NOT added to `_AGRI`'s
  tuple; NAF `01.`.

**D9. Cleaning rules, per file, applied at ingest and unit-tested** (all reuse existing code
where it exists):
- read every column as text (`dtype=str`, calamine); SIRET `0.0`/`0` → NULL; 13/12-digit SIRET
  and 8-digit SIREN → left-pad and keep only if Luhn-valid (new `luhn_ok`); CP 4-digit → pad
  (existing `clean_postal_code`); `20xxx` Corsica stays (view maps 2A/2B).
- phones: `normalize_fr_phone` + `is_surtaxe` from `scripts/m2lib_contact.py:45,57`
  (`33XXXXXXXXX`, bare 9-digit float, `0X XX XX XX XX` all covered — add selftest cases).
- e-mails: existing `repair_email_domain`; **new strict-domain gate** (domain must end in a known
  TLD and contain no second TLD → else `malformed`); the S9-F glued-domain logic in
  `scripts/m1_s9f_email_glued_repair.py` reused for the 1,181 + 86; spaced locals/domains →
  `malformed` with a hyphen-substitution candidate in `evidence` (⚖ repair later, never ship).
- strip NBSP / whitespace everywhere (`clean_str` extended); the blank header column = department
  (data_finale, imprimerie) or a duplicate of SOCIETE (camping) → mapped by position in the spec.
- imprimerie: `city = COALESCE(VILLE, ADRESSE1)` (mutually exclusive, verified).
- viticulteur: in-file dedup on `(name, zipcode, phone)` → ~1,630; `pagesjaunes.fr#` is not an id;
  fold accents for matching; `category` `+N` suffix stripped.
- boulang: key on `siren_enrichi`; `SIRET` used only when `left(SIRET,9) = siren_enrichi`.
- tourism vs agriculture overlap (336): same company, two `company_sources` rows — no merge
  needed, that IS the model.
- Camping.xlsx: **skipped** (decided). Not in `FILE_SPECS`; noted in the progress doc and in
  the message to Mehdi.

**D10. Loading order is fixed by dependencies:** restore → migrations → refactored ingest proves
itself on the two agriculture files (`[SKIP]` by hash) → provider files one at a time, gate after
each → deliverable loaders → per-sector qualify/enrich/dedup → per-sector export.

---

## Implementation steps (one step per turn: commit, push, report — Ines's pacing rule)

### Step 0 — Ines: restore the Supabase project; baseline
`check_data_quality.py --strict` must pass (15/0/0); `git tag pre-m4-ingestion`; table counts
recorded in new `docs/m4_ingestion_progress.md` (template of `docs/m1_s9_progress.md`).

### Step 1 — Migration 018 `018_multi_sector_contact_points.sql` (additive, replayable)
D2–D6 tables/columns; backfill `company_sources`; add the missing FK on
`raw.ingest_rows.source_file_id` (`NOT VALID` + `VALIDATE`); unique index
`contacts(site_id, full_name) WHERE full_name IS NOT NULL` only if the live data has no
violations (measure first). Apply via MCP `apply_migration`; verify with counts; commit the file.

### Step 2 — Migration 019 + per-sector quality gate
`019_sector_in_views.sql` (D7). `check_data_quality.py --sector <key>`: checks 7/8/13 read bands
from `SECTOR_BANDS` in `config/sector_rules.py`; **new guard: every distinct
`source_files.sector` appears in some `SECTORS[*].source_sectors`** (a typo'd sector would
otherwise be qualified by nobody, silently); companies with >1 sector (WARN + count); no
`is_dialable=false` point equals a `phone_main`; `contact_points.value_norm` well-formed per
kind; no `malformed` e-mail in `staging.emails`. Agriculture baseline still green.

### Step 3 — Refactor `m1_s3_ingest.py` into a spec-driven, resumable loader
- `config/ingest_specs.py`: `FILE_SPECS` — filename, sector, pipeline_stage, data_source,
  collected_at, sheet, `col_map` (by name or by position for blank headers), `required`,
  `phone_columns`, `identifier_columns`, `attribute_columns`, per-file `pre_clean` hook name
  (imprimerie city coalesce, viticulteur dedup, boulang key choice). The two agriculture specs
  move here; **file A's `dep` mapping dropped** (blank header; department derived in views).
- `scripts/ingest_lib.py`: the inline normalisers moved unchanged (`test_normalisers.py` keeps
  importing them) + `luhn_ok`, `strict_email_domain`, `normalize_fr_phone`/`is_surtaxe` import;
  `check_column_contract`; `get_conn()` with keepalives + bounded retry copied from
  `m1_s8_export.py:243-265`; writers for the new tables.
- `m1_s3_ingest.py --file <name> [--dry-run] [--limit N]`. Sector comes from the spec, never the
  CLI. Stages each committed and idempotent: register `source_files` → raw rows → companies +
  company_sources → sites → contacts → emails → contact_points → identifiers → attributes → update
  `row_count_imported`. A dropped socket resumes by re-running. No-SIRET rows: look up
  `company_identifiers` first, else INSERT and record `file_row` id.
- Proof: `--file "Copie de agriculteurs total.xlsx" --dry-run` passes the contract; real run
  prints `[SKIP] Already imported`; same for file B; `test_normalisers.py` green with new cases;
  quality gate unchanged.

### Step 4 — Ingest the provider files, one per turn, gate + progress entry after each
Order: `base imprimerie scrapé` (proves the no-SIRET path + city fix, 9,182) →
`pagesjaunes_viticulteur_gironde` (proves in-file dedup + PJ ids, ~1,630) →
`boulang_patisserie` (10,685, `pipeline_stage=siret_matched`) →
`data_finale sans croisement` (50,849, `sector=tourisme`; run in background, it is the big one).
Camping is not loaded. After each: `--dry-run` distribution first, then real run,
`check_data_quality.py --sector`, `written=` counts from the DB into the progress doc, 20 rows
read back by eye against the file.

### Step 5 — Load the two deliverables (new scripts; read everything, then short committed writes)
- `scripts/m4_s1_load_boulangerie.py`: (a) `api_raw.jsonl` → `source_files`
  (`sector='boulangerie'`, `data_source='recherche-entreprises.api.gouv.fr'`, `collected_at=
  2026-08-13`) + `raw.ingest_rows`; (b) `checkpoints/etablissements.csv` → companies (SIREN,
  names, legal_form, nature_juridique_code, creation_date, employee_bracket, naf_code_source) +
  sites (SIRET, is_headquarters, address, CP, city, **lat/lon**) + contacts (prenom/nom/fonction,
  `name_source='rne_dirigeant'`); (c) `boulangerie_13_v15.csv` → best picks (phone_main; emails
  valide→valid / non verifie→unknown / risque→risky + `source`; website_url/verdict) **and**
  `contact_points` for every phone/e-mail/site/social with source + confidence, `Telephone
  piste` → `is_dialable=false`; `Procedure collective` → `company_attributes`; (d)
  `verified_emails.csv` incl. the 135 invalide → `contact_points verdict='invalid'`;
  `site_verdicts.csv` → `contact_points kind='website'` with verdict/ownership in `evidence`.
  Cross-check: SIREN overlap with Mehdi's bakery file (expected ≈ the 920 dept-13 rows) becomes
  a second `company_sources` row, not a duplicate company.
- `scripts/m4_s2_load_agriculteurs.py --departement 63|03`: same shape; key `numeroBio` →
  `company_identifiers`; two operators on one SIRET = one site + two identifiers + contacts per
  operator; 13-char SIRETs never loaded as SIRET (raw + identifier, flagged); `certificats`,
  `productions` (AB state), `venteAnnuaire`, `flag_hors_agri` → `company_attributes`; `gerant` →
  `contacts.full_name`; `declare` → `unknown` + `source='agencebio'`.
- Both: `--dry-run`, `--limit`, resumable on natural keys; gate `--sector` after each.

### Step 6 — Rules, qualification, enrichment, dedup — per sector
- `config/sector_rules.py`: the 5 entries (D8) + `SECTOR_BANDS`.
- `m1_s5_qualify.py`: scope via `company_sources` (D2); `--sector <key> --dry-run` then real, per
  sector. Agriculture proof: `--sector agriculture_livestock --force --dry-run` still yields
  57,065 / 43,376 / 9,099.
- `m1_s4_sirene_enrich.py` on the new SIRENs (global is fine: writes only NULL, sector-agnostic).
  `m1_s4b_siren_recovery.py` for viticulteurs/imprimerie (name + CP; expect ~45% as in
  agriculture) — ⚖ run or not, it is hours of API time.
- `m1_s5b_dedup.py`: add a same-sector predicate (join `company_sources`) to passes 1–3; GUARD 2
  (different SIRENs never merge) stays. `--dry-run` per sector, then real.

### Step 7 — Export per sector + docs + handoff
`m1_s8_export.py --sector <key>` (`SECTOR_SLUG` from the flag, `%(sector)s = ANY(sectors)` in
`SELECT_SQL`, output `exports/<sector>/`); agriculture export byte-identical to the pre-M4
archive (archive first — `exports/` is gitignored). `docs/m4_ingestion_progress.md` complete;
CLAUDE.md stage table + NEXT SESSION; `migrations/README.md` rows 018/019; memory resume point.

## Verification (end to end)
- `python scripts/test_normalisers.py` green (moved imports + new phone/e-mail/Luhn cases).
- `check_data_quality.py --strict` green globally and `--sector` green for every sector; the
  "every `source_files.sector` has a rule set" guard passes.
- Agriculture regression: qualify distribution unchanged; export byte-identical;
  `v_deliverable_businesses` count unchanged for `primary_sector='agriculture'`.
- Per sector, read back via MCP: companies / sites / contact_points by kind × source /
  identifiers equal each loader's `written=`; 20 rows per sector compared by eye to the source
  file (defects are found by reading the built thing, not the logs).
- Idempotency: every loader re-run → 0 new rows.

## Files
- New: `config/ingest_specs.py`, `scripts/ingest_lib.py`, `migrations/018_*.sql`,
  `migrations/019_*.sql`, `scripts/m4_s1_load_boulangerie.py`,
  `scripts/m4_s2_load_agriculteurs.py`, `docs/m4_ingestion_progress.md`.
- Modified: `scripts/m1_s3_ingest.py`, `scripts/m1_s5_qualify.py` (scope), `scripts/m1_s5b_dedup.py`
  (sector predicate), `scripts/m1_s8_export.py` (`--sector`), `scripts/check_data_quality.py`,
  `scripts/test_normalisers.py`, `config/sector_rules.py`, `CLAUDE.md`, `migrations/README.md`.
- Reused as-is: `scripts/m2lib_contact.py` (phones), `scripts/m1_s9f_email_glued_repair.py`
  (glued domains), `scripts/m1_s9d_nameparse.py` (`gerant` split later), `m1_s8_export.py`'s
  connection recipe.
