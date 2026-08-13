# 01 — Strategy: why the enrichment works the way it does

Every rule in this file was **paid for** by a real incident, either in
agriculture (sector 1) or in this sector. None is a style preference.

## The goal, in business terms

The client sells energy-efficiency services to businesses. A lead is worth
something when a salesperson can **reach** it: a phone number they can dial, or
an e-mail that will not bounce, ideally with the decision-maker's name. The
registry gives names for free (90.5% coverage); the entire enrichment problem
is the **contact channel**. For bakeries, phone is the primary channel —
measured, not assumed: even after four versions, e-mails plateau near 20%
while phones keep climbing.

## Principle 1 — Measure first, always at N≈20

No source is trusted on reputation. Every new source gets a ~20-item pilot
whose **precision is measured against something already trusted** (usually the
Google Maps phones) before any full run. Numbers that came out of this habit:

- Serper `/places` pilot → **no phone field at all** → switched to `/maps`
  before wasting the credit budget.
- Search-snippet phones → 76% agreement → banned from the dialled column.
- Unconfirmed-site phones → **17%** agreement → banned.
- Named Maps queries → 35% incremental yield → approved (gate was 25%).
- Facebook: agreed rule is the same — 20 public pages, hard 30% go/no-go,
  **never** logged-in scraping.

## Principle 2 — The matcher decides, never the harvester

No harvester writes contact data onto a business. Harvesters write **listings**
(name, geo, phone, site) to CSV; `m2_s7_match.py` decides which SIRET a listing
belongs to, using geo + name rules where **ambiguity = rejection**. The reason
is one measured example among many: a named Maps query for `COMPAGNIE
BOULANGERE` returned "Boulanger Aubagne" — an *electronics retailer*. A
harvester that trusted its own results would have shipped that.

## Principle 3 — Corroboration beats confidence

A single weak claim stays a lead. Two **independent** sources naming the same
phone number is different evidence: for them to agree by chance, two unrelated
publishers would have to make the same mistake about the same shop. This is
the same logic that made OSM×Maps agreement (45/47 = **95.7%**) the strongest
signal in the project, applied to the weak sources. Mechanics in
[05-rules-and-gates.md](05-rules-and-gates.md). Independence is judged **by
source** — two snippets from one directory are one page read twice.

## Principle 4 — Uncertainty ships disclosed, never dropped, never promoted

Wrong on either side costs money: a dropped lead is lost value, an oversold
one makes a salesperson call a stranger. So everything uncertain has its own
labelled column: `Telephone piste (non confirme)`, `Telephone surtaxe`,
`Email confiance`, `Email verifie`. Proven-invalid e-mails are **withheld**
(shipping them damages deliverability for the whole campaign — the agriculture
hard-bounce lesson). The gate enforces this shape mechanically.

## Principle 5 — Our failure is not evidence about the data

The most expensive recurring mistake in the whole project. Agriculture wrote
DNS timeouts as `invalid` and killed 10,478 good addresses. This sector nearly
repeated it twice in one day:

- a numbering-plan filter written from memory rejected `09 52 25 45 55` — a
  real bakery's line (caught by the selftest);
- a `pgrep` wait-loop read "process not running" as "verification finished"
  and the export shipped 109 proven-invalid addresses for 3 minutes (caught by
  comparing file timestamps).

Consequences: SMTP failures resolve to `unknown`, never `invalid`; abort gates
stop runs whose failure rate looks like *our* breakage (>20% dead domains);
and the export is rebuilt only **after** verification's output file is fresh.

## Principle 6 — The two kinds of walls, and why the difference matters

- **Structural wall**: the data does not exist. Farms have no websites — four
  independent measurements agreed. No spend fixes this.
- **Infrastructural wall**: the data exists but the access path blocks us.
  These sometimes fall to a different path (free APIs) and otherwise to small
  spend (a VPS with clean reverse-DNS for SMTP verification).

V2 misdiagnosed an infrastructural wall as needing money, when it actually
needed a different *path*. The correct sequence when blocked is:
**1) is there an official API with a free tier? 2) is the data visible in
search-engine snippets? 3) only then consider paying.** What remains genuinely
paid-only today: SMTP probing of Orange/SFR/Outlook/Yahoo (mail servers judge
the connecting IP; no free tier substitutes) and Pages Jaunes direct crawl
(Cloudflare — see [02-sources.md](02-sources.md)).

## Principle 7 — Crash-safety is file-shape, not luck

Inherited from agriculture's triple data-loss: every long-running script
**appends and flushes after every unit of work**, keeps a done-file for
resume, and logs `written=` (rows on disk), never `collected=` (rows in
memory). Every script is idempotent; killing anything at any time costs at
most one unit. Quotas are enforced by a **local hard-stop counter**
(`search_quota.json`) so a bug can never sail past a free tier into billing.

## Principle 8 — Versions are frozen; each must beat the last

Each deliverable (`_v2`, `_v3`, `_v4`…) is a new file; older ones stay
reproducible. The gate's no-regression check compares against the previous
version — an "enrichment" that loses data is a bug, and this project has hit
that failure mode enough times to test for it. Caveat learned in V4: the
regression check only catches numbers going *down*; a timing bug that adds bad
rows looks like a win, hence the explicit invalid-addresses-read-back check.
