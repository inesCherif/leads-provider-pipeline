# 05 — Rules, guards, gates — and the incident behind each one

Nothing here is a preference. Every rule exists because the naive version
shipped (or nearly shipped) wrong data on this very project.

## Phone rules

### Precedence in the dialled `Telephone` column — set by measurement

```
osm (0)  >  serper_places (1)  >  pagesjaunes (2)  >  site/confirme (3)  >  corrobore(a+b) (4)
surtaxé adds +100 → a premium number loses to any ordinary one
```

- OSM×Maps agree 45/47 (**95.7%**) — two independent sources; nothing else has
  that, so directories outrank everything.
- `site/confirme` (SIRET proven on the page) agrees with Maps only **62%** —
  intuition said a business's own site should win; the measurement said no.
  Kept, ranked last: it fills gaps.
- **Banned from the column entirely** (ship in `Telephone piste` instead):
  search snippets (**76%**) and `site/faible` (**17%**, 50% after the
  switchboard guard). One wrong number in four = a salesperson calls a
  stranger. `faible` means the domain was never proven to be theirs — the
  `EARL DU VIEUX CHENE → vieuxchene.fr` lesson in phone form.

### Corroboration (piste → dialled)

Two **independent** sources naming the same normalized number promotes it as
`corrobore(a+b)` at rank 4. Independence is by SOURCE (`serp/pagesjaunes` +
`site/faible` = yes; two snippets from one directory = no). The promoted
number still passes the switchboard guard. Implementation detail that matters:
untrusted claims are a **list per SIRET** — keeping only the first claim
(an early bug) silently discarded the second witness, which *is* the signal.

### The guards

| Guard | Incident that created it |
|---|---|
| **Phone must be announced** (`tel:` href or phone-word within 60 chars) | loose full-page extraction pulled `01 11 24 63 33` etc. out of minified JS — 15–53% agreement |
| **Switchboard**: number claimed by >2 distinct SIRENs → dropped | `04 42 56 68 46` sat on **19 companies'** pages (franchise/web-agency footer). Keyed on SIREN so a real multi-site firm keeping one line survives |
| **Surtaxé flagged, ranked last, never silently dropped** | agriculture once "deduped" 12.5% of its base on a shared premium hotline |
| **Unassigned-prefix filter only** (01 1x/01 2x/03 0x/05 9x) | a fuller table from memory rejected a REAL line (`09 52 25 45 55`); context does the real filtering |
| **Normalization to `0X XX XX XX XX`** everywhere | dedup/corroboration only work on one canonical form |

## E-mail rules

| Rule | Incident |
|---|---|
| **Site identity before addresses count**: SIRET/SIREN on page → confirme; CP+name → confirme; else faible (never pattern-expanded) | agriculture nearly wrote a stranger's address from a guessed domain |
| **Third-party filter**: address must share the crawled domain or be a consumer mailbox | **320 supplier/aggregator addresses** were about to ship — `pavailler.com` (oven maker) ×24, `societeinfo.com` ×19, `doctrine.fr` ×14 |
| **Store-list cap**: >12 addresses on a domain → keep only commune-matching/generic; >12 phones → keep NOTHING (phones have no locality marker) | sophie-lebreuilly.com: 94 mailboxes of shops in other départements |
| **Network domain**: >1 SIRET claims a domain → `reseau`, faible | franceboulangerie.fr covered 3 of ours; its mailbox is the network's |
| **Escape-artifact strip** | `u003emarius@…` — JSON-escaped `>` welded to the local part, guaranteed bounce |
| **Generated addresses ship only SMTP-`valid`** | catch-all domains answer 250 to everything → `risky` → discarded. Never-refuted ≠ proven |
| **Never pattern-expand an aggregator** | `contact@fr.mappy.com` is a real mailbox at a company that is not our prospect |
| **`invalide` never ships** | agriculture migration 015: before it, marking invalid didn't stop exporting |
| **Our failure ≠ data's failure**: SMTP/DNS errors → `unknown`; >20% dead-domain rate aborts the run | the 10,478-good-addresses-marked-dead night |

## The gate (m2_s17_check_v4, 19 checks, reads the **xlsx back**)

Hard (exit 1): H1 SIRET Luhn · H2 SIREN=left(SIRET,9) · H3 no dup SIRET ·
H4 CP starts 13 · H5 NAF in scope · H6 no empty source column (flag columns
exempt but reported) · H7 identifiers stored as TEXT · H8 core fields
non-empty · H9 phone format · H10 surtaxé flagged · H11 phone provenance
stated · H12 **no regression vs previous version** · H13 no untrusted source
in the dialled column · H14 piste empty where a phone exists · H15 every
`corrobore` cites 2 distinct sources.
Warn (fail with `--strict`): W1 row-count band · W2 name coverage ≥70% ·
W3 phone coverage band · W4 liquidation disclosure.

**Known blind spot (V4 incident):** H12 only fails when numbers go DOWN. The
export once ran 3 minutes before verification finished and shipped 109
proven-invalid addresses — looked like a *win* to the gate. Therefore the
shipping ritual is: verify → **check `verified_emails.csv` mtime is newer than
the xlsx** → export → gate → read the workbook back and assert 0 shipped
addresses carry an `invalide` verdict.

## Chronological incident log (this sector)

1. Registry `departement=13` matches sièges elsewhere → build from
   `matching_etablissements` (V1).
2. 84 non-bakery outlets passed the legal-unit NAF filter → activity guard (V1).
3. `dirigeants[0]` is often the auditor → ranked selection (V1).
4. Franchise store list, network domain, escape artifact (V2 — see above).
5. Serper `/places` has no phone field; `/maps` has no pagination (V3).
6. Social page registered as "website" on 13 SIRETs (V3).
7. Supplier mailboxes ×320 (V3).
8. Snippet/site-faible phones fail precision → piste column + bans (V3).
9. JS digit-runs as phones; switchboard ×19; over-eager numbering table (V4).
10. `claims` dict shadowed by a loop variable → corroborator saw nothing (V4).
11. Export raced verification → 109 invalid shipped for 3 minutes (V4).
12. PJ is Cloudflare, not DataDome; human-solved clearance not portable (V4).
