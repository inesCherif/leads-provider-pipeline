# Leads pipeline — working guide for Claude Code

Read `README.md` for the overview and install, and `docs/RUNBOOK.md` for the exact commands of every
pipeline. This file holds what must stay true while you work here: conventions, gates, and the
rules that were **measured** and must not be softened.

## What this is

A zero-budget French B2B lead pipeline. Two worlds:

- **The database** (Supabase Postgres): `scripts/m1_*`, `scripts/m4_*`, `scripts/ingest_lib.py`,
  `scripts/check_data_quality.py`, `config/`, `migrations/`. Provider Excel files → `raw.*`
  (immutable JSONB) → `staging.*` (`source_files` → `companies` (SIREN) → `sites` (SIRET) →
  `contacts` → `emails`, plus `company_sources`, `contact_points`, `company_identifiers`,
  `company_attributes`) → `public.*` views. `audit.*` is append-only. Full reference:
  `docs/project_documentation.md`, `migrations/README.md`.
- **The sector pipelines, DB-free by design**: `m2_*` (bakeries), `m3ag_*` (organic farmers),
  `m5_*` (gîtes and campings), `m6_*` (livestock farmers), `m7_*` (producers), `france_*` (96
  départements), `paca_*` (detached runners for one région). They read and write files only, under
  `exports/<sector>/` and `exports/<sector>/checkpoints/`. No connection is ever held open, which is
  what makes a killed run lose nothing. The only DB access is three read-only witnesses
  (`m5_s19`, `m6_s19`, `m7_s19`).

Every sector pipeline has the same shape: `s1` acquire (registry) → `s2` transform → harvesters
(`s5*` directories, `s6` OSM, `s7` Pages Jaunes, `s3` search, `s4` crawl, `s18` social) → `s8`
match listings to SIRETs → `s11` verify e-mails → `s9` export (source ranking) → `s12` gate →
`s14` reduced call sheet. Later sectors are thin wrappers over `m3ag_*` and `m2_*` with their own
`mN_lib.py` (scope, vocabulary, paths).

## Conventions

- **Idempotent and resumable.** Every script keeps a done-file or a checkpoint column; re-running
  does only what is missing. Enrichment updates use `COALESCE` (never overwrite a fact);
  *derived* columns (qualification) overwrite on purpose, keyed on `rule_version`.
- **Never hold a DB connection open during slow work.** Open a fresh connection per write, flush
  every page, log `written=` not `collected=`. The pooler closes idle connections; this lost
  thousands of rows three times before it became a rule. Use keepalives on long reads.
- Bulk inserts via `psycopg2.extras.execute_values`, never row by row.
- Every inserted row carries `source_file_id`. No orphan rows.
- `SUPABASE_DB_URL` must be the **session-mode pooler**. The direct host is IPv6-only and fails as
  "could not translate host name", which looks like bad credentials and is not.
- **Our failure is not evidence about the data.** A DNS timeout is not an invalid address; a site
  that times out for us is not closed. Tri-state verdicts, and a sanity gate that aborts a run when
  an implausible share looks unreachable.
- **Pilot first**: `--pilot 10|20`, then open the rows by hand. Go/no-go for a new lever is 30 %
  usable rows. Do not trust a printed percentage — one pilot printed 50 % while writing junk.
- **Anything over ~10 minutes runs detached**, with a log, from a `.sh` runner. On Windows:
  `Start-Process "C:\Program Files\Git\usr\bin\sh.exe" -ArgumentList 'scripts/<runner>.sh'
  -WorkingDirectory <repo> -WindowStyle Hidden`. If the detached shell cannot find Python, set
  `PYTHON_DIR`. A `.sh` written with a BOM exits silently with no log.
- `exports/`, `logs/`, `.env`, every `.xlsx` / `.csv` (except `config/*.csv`) are gitignored.
  Never commit prospect data. Deliverables travel by shared drive.
- Commits: conventional (`feat|fix|docs|refactor|chore: what and why`), one logical change each.

## Quality gates — cheap, run them

```bash
python scripts/test_normalisers.py                 # unit cases, no DB, milliseconds
python scripts/check_data_quality.py --strict      # distribution checks on the live DB
python scripts/m7_s12_check.py --departement 63 --version v2 --baseline v1 --strict
python scripts/france_verify.py --strict           # the whole delivery tree, read back
```

Sector gates: `m2_s19_check_v5.py`, `m3ag_s12_check.py`, `m6_s12_check.py`, `m7_s12_check.py`.
Run the DB gate after any stage that writes and before any export ships. Run a sector gate on
every built file; **a file ships only because someone saw `0 failed`**. Constraints guard structure;
these guard *distribution*, which is where every bug here has lived. A new rule gets a new gate
check, and the check is accepted only if it **fails on the old, faulty file**.

Ingestion enforces a **column contract**: a mapped column missing from the file stops the import.
Update the spec's `col_map` and `required` together (`config/ingest_specs.py`).

## Rules that were measured — do not soften

**Phones**
- Rank is set by measured agreement, not intuition. Bakeries: `osm > serper_places > pagesjaunes >
  own site (confirmed)`; OSM and Maps agree on 95.7 % of shared businesses; a business's own site
  agreed only 62 %. A source below ~80 % agreement is a **witness**, never dialled on its own.
- `piste` (search snippets, 76 %) and `site/faible` (17 %) never reach the dialled column. They
  may *corroborate*: two **independent** sources naming the same number is different evidence.
  Independence is by source; the gate fails a `corrobore(...)` label with fewer than two witnesses.
- A phone must be **announced** as one: a `tel:` href or a phone word within 60 characters.
  Ten-digit runs inside minified JavaScript are not phones.
- **Switchboard guard**: a number claimed by more than 2 distinct SIRENs is dropped (keyed on
  SIREN so a real multi-site company keeps its line). Premium-rate numbers never reach a phone column.
- A provider file's phone agrees ~73 % with measured sources: it keeps **its own labelled column**
  (`provider_phone`) and never the dialled one.
- Never write a numbering-plan table from memory; context does the filtering.

**E-mails**
- **An e-mail on a page is not the page owner's e-mail.** Keep an address only if it shares the
  crawled domain or is a consumer mailbox (`is_third_party_email`); suppliers, directories and web
  agencies otherwise flood the file.
- **Unknown must mean weak.** A one-witness e-mail from a weak source must carry the business's
  name in the address. "Weak" is *every source absent from `EMAIL_RANK`*, never a hand-written list
  — a hand-written list let an unranked label ship addresses attached to the wrong business
  (gate H18, `m7_s12`). Files built before that fix can carry such rows; a rebuild corrects them.
- More than 12 addresses on one page is a franchise store list. A domain claimed by more than one
  SIRET is a network (`reseau`), confidence `faible`. One address on several SIRENs is a shared
  mailbox (franchise HQ), not each shop's own.
- A generated address ships only when a mail server accepts it, and never on a catch-all domain.
  Never pattern-expand an aggregator's domain.
- An `invalid` address never ships; a proven bounce invalidates every staged copy.
- Residential IPs are refused by Orange / SFR / Outlook / Yahoo: those stay `non verifie`.

**Sites and matching**
- A site verdict is per **(SIRET, domain)**, never per domain. Test order in
  `m2lib_validate.classify()` is load-bearing: shop-page evidence > directory test > ownership >
  shared; an HTTP 403 host is alive (flag, never delete).
- A social page is not a website. Deep links (posts, videos) are rejected, never truncated.
- Match on geography before name: distance and house number + street + postcode beat a trade name
  that repeats across a city. Ambiguous within 40 m → rejected, not guessed.
- The registry filters at legal-unit level: build rows from `matching_etablissements`, keep the
  activity guard, rank officers (gérant > président > exploitant > DG; never the auditor).
- Département membership goes through `france_lib.in_dept` — `cp.startswith(dept)` is False for
  every Corsican row (2A / 2B) and used to produce an empty Corsica, silently.
- A named Maps query proves nothing on its own; named results go through the matcher like any other.

**Database**
- **Sector = membership** (`staging.company_sources`); the verdict follows the **primary** sector.
  Qualification, dedup (never across sectors), gate and export all scope on it. Every
  `source_files.sector` must have a rule set in `config/sector_rules.py` — the gate fails otherwise.
  Without the `source_sectors` scope, qualifying one sector silently re-classifies every other.
- When a rule changes, **reload from raw, never patch in place**: `m4_unload_file.py` is the only
  DELETE (scoped by file, `--dry-run` then `--confirm`), then re-ingest.
- An enriched SIREN needs the row's own evidence; a provider SIRET that contradicts it wins.
- `naf_label` holds the source file's activity string, not a SIRENE label — tier-2 qualification
  runs its regexes on it. Do not "fix" it.
- Any `DISTINCT ON` needs a guaranteed-unique final tiebreak. `public.v_deliverable_businesses` is
  the single definition of the deliverable population; do not re-implement the collapse.
- `is_active` lives on `staging.sites`, not `staging.companies`.

**Scope and delivery**
- The producers' scope excludes alcohol and pork-specific activities by design: `m7_lib.EXCLUDED_NAF`,
  `EXCLUDED_RE` (strict, word-bounded), `EXCLUDED_LOOSE_RE`, `host_hits`; gates H14 / T19. A
  **surname** that collides with a trade word (Vigneron, Brasseur) is a spelling coincidence on a
  farming NAF — `m7_lib.name_hit_is_surname`; a plural, a leading article or a collective word means
  the trade. Every name-based drop is logged to `principle_excluded_<dept>_<version>.csv`; reviewed
  rescues live in `config/principle_rescue.csv`. To change the scope, edit these and rebuild.
- **Never-twice register**: every call sheet excludes everything already delivered to the call
  agent (`scripts/maha_lib.py`, copies in `exports/maha_sent/`, gate T16). Register a sheet right
  after it is sent: `python scripts/maha_lib.py --register <xlsx>`.
- Versions are **single-digit** (`v1`…`v9`): the delivery tree sorts lexically, `v10 < v2`.
- Two versions of one département never sit side by side in the delivery tree (`france_publish.py`).
- The client format is **xlsx**: French Excel splits CSV on `;`, turns a SIRET into `4,47E+13` and
  drops a postcode's leading zero. Strip control characters before openpyxl; use write-only mode.

**Pages Jaunes and rendered sites**
- Only through a Chrome the user started with `--remote-debugging-port=9222`; the script attaches
  (`--attach`). One harvester per Chrome at a time.
- Navigate **by URL**, never by typing in the search form (its location field keeps an internal id,
  so every "search" silently re-runs the previous town). An unknown trade slug is a free-text
  search, not a 404 — filter on the category (`PJ_CATEGORY_OK_RE`).
- Result cards outrank any block marker in `is_blocked()`: the stylesheet contains the word
  `captcha`. On a real challenge the run waits for the human, then resumes from its done-file.
- Pages Jaunes is a **phone** lever (phone on ~100 % of cards, a website on ~1 in 1,000, never an
  e-mail). Anonymous Instagram returns an empty app shell: closed. Public Facebook pages, rendered
  in their own empty browser, give ~40–80 % — and the pool comes *from the crawl*, so social runs
  after it.
- Respect `robots.txt` (one directory's phone endpoint is disallowed and is never requested).

## Current state and open items (2026-09-20)

- Database: every sector loaded and gated; per-sector exports via `m1_s8_export.py --sector`.
  `m1_s4_sirene_enrich.py` holds one connection for hours and can stop on a pooler drop — it is
  idempotent, re-run it. RLS has zero policies (`CREATE POLICY IF NOT EXISTS` is invalid SQL in
  migration 001); the service role bypasses RLS, but 001 cannot be replayed as is.
- France (96 départements) built as v1; départements 03 and 63 carry their own later versions.
- PACA e-mail pass: 13, 83, 84 rebuilt and gated as v2; **04, 05, 06 not rebuilt**; Pages Jaunes and
  the search sweep not run on PACA. Commands: RUNBOOK, "Finir une passe d'enrichissement".
- Overseas départements 971–976 are not handled (`dept_of_cp` collapses them to "97").
- `m7_s13_places.py` (Maps sweep for farms, a wrapper over `m2_s13_places.py`) is not written.
- Bakeries paused at V15 (dept 13). Gîtes/campings: crawl, verify, search, farm gîtes and municipal
  campings not built. Supabase write side for éleveurs / producteurs / hébergement not built.
- Open scope question: a farm whose website says "vignoble" while its NAF is not wine-growing —
  today the NAF decides and the row is kept.
