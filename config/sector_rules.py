"""
Sector qualification rules — M1-S5
===================================
Per-sector config for the qualification pass. Lives in git (not a DB table) so
rule changes are diffable, reviewable, and attributable in history. The DB
records which version produced each verdict via
staging.companies.qualification_rule_version.

IMPORTANT: bump a sector's RULE_VERSION whenever you change any of its rules.
The qualification script re-processes every row of that sector whose stored
version differs from the current one, so bumping the version is what triggers a
re-qualification. Editing a pattern without bumping the version leaves old
verdicts in place — and the run reports "0 rows", which reads as "nothing
changed" when it actually means "nothing was examined".

── MULTI-SECTOR, since 2026-08-02 ────────────────────────────────────────────
This file used to be single-sector by construction: SECTOR, RULE_VERSION and the
NAF/label constants were module-level and imported directly by m1_s5_qualify.py.
That made a second sector impossible without a rewrite, and the failure mode was
not a crash — it was silent cross-contamination:

    the stale-row selector is `qualification_rule_version IS DISTINCT FROM <v>`,
    so running sector B's pass would have matched EVERY sector A row (their
    version differs) and re-classified 128k agricultural companies against
    bakery rules.

The fix has two halves and BOTH are required:
  1. rules live in the SECTORS dict below, selected by --sector;
  2. the target set is scoped by joining staging.source_files and filtering on
     `source_sectors`, so a pass can only ever touch its own sector's rows.

`source_sectors` is the link between a rule set and the data: it lists the
values that appear in staging.source_files.sector for this sector's files.
Adding a sector means adding an entry here and setting `sector` correctly at
ingestion — nothing else.
"""

# ─── Reason vocabulary ────────────────────────────────────────────────────────
# Must stay in sync with the CHECK constraint in migrations/004_qualification.sql.

REASON_PUBLIC_ADMIN      = "public_administration"
REASON_NAF_OUT_OF_SCOPE  = "naf_out_of_scope"
REASON_LABEL_OUT_SCOPE   = "label_out_of_scope"
REASON_BUSINESS_CLOSED   = "business_closed"
REASON_SIRENE_NOT_FOUND  = "sirene_not_found"
REASON_NO_SIREN_NO_LABEL = "no_siren_no_label"


# ─── Agriculture / livestock (the pilot sector) ───────────────────────────────

_AGRI = {
    "key": "agriculture_livestock",
    "rule_version": "agri-v2",

    # Values of staging.source_files.sector that belong to this rule set.
    # This is what scopes the UPDATE — see the module docstring.
    "source_sectors": ("agriculture", "livestock"),

    # ── Tier 1: official INSEE NAF classification ────────────────────────────
    #
    # Prefix match, NOT an exact-code allow-list. This is deliberate: 1,281
    # companies carry pre-2008 NAF rév.1 codes ('01.2A', '01.4A', '01.1A',
    # '01.2E', '01.3Z') and 1,136 of those are agricultural. An exact list of
    # modern rév.2 codes ('01.41Z') would silently drop every one of them.
    # Prefixes cover both classifications.
    #
    # Section A of the NAF: 01 = agriculture/livestock, 02 = forestry, 03 = fishing.
    "naf_prefixes_in_scope": ("01.", "02.", "03."),

    # Codes whose official classification is out of scope but which are genuinely
    # farms when the source label agrees. Decided 2026-07-19:
    #   68.20B — GFA: real farms legally structured as land-holding companies (2,040 rows)
    #   35.11Z — on-farm electricity producers: solar/biogas farms (253 rows)
    # Both are exactly the heavy-energy-consumer profile the client wants.
    "naf_rescue_codes": ("68.20B", "35.11Z"),

    # Prefix-matched rescue, added in agri-v2 (decided 2026-07-20). Same guard as
    # naf_rescue_codes: only qualifies when the source label is agricultural, so a
    # high-street bakery with no farm label stays out.
    #
    # The rescue criterion here is ENERGY INTENSITY, not "is it a farm". The client
    # sells energy efficiency, so on-farm transformation is exactly the target
    # profile — these run ovens, pasteurisers, cold rooms and fermentation tanks:
    #   10.*  food processing  (289 rows) — dairy/cheese, meat, bakery, fruit & veg
    #   11.*  beverages        (201 rows) — wine, cider, beer, spirits
    #   35.1* electricity      (7 rows)   — widens the existing exact 35.11Z rescue
    #
    # Deliberately NOT rescued, though they carry agricultural labels: riding
    # schools (85.51Z), sport (93.19Z), associations (94.99Z), farm gites (55.20Z),
    # garden retail (47.76Z), land holding (68.20A). Agritourism and leisure are not
    # heavy energy consumers; 68.20A is a holding shell, unlike 68.20B GFAs.
    "naf_rescue_prefixes": ("10.", "11.", "35.1"),

    # Disqualified regardless of what the source label claims. Communes own farmland
    # and get labelled 'AGRICULTEURS' by the data provider, but a town hall is not a
    # prospect (128 rows).
    "naf_hard_exclude": ("84.11Z",),

    # ── Tier 2: source-label matching (no-SIREN companies) ───────────────────
    #
    # Applied as a Postgres regex to unaccent(lower(naf_label)), so patterns must be
    # lowercase and unaccented. The label vocabulary is inconsistent free text mixing
    # real INSEE labels ('ELEVAGE DE VACHES LAITIERES') with the data provider's own
    # buckets ('AGRICULTEURS', 'Producteurs', 'eleveur'), in both cases.
    "include": (
        r"agricult|exploitation agricole|\yferme\y|"
        r"elevage|eleveur|"
        r"producteur|production animale|"
        r"cultur|viticult|maraich|horticult|arboricult|"
        r"apicult|piscicult|aquacult|"
        r"haras|equide|cheval|chevaux|trotteur|"
        r"laitier|laitiere|bovin|ovin|caprin|porcin|volaille|"
        r"cereal|semence|fourrage"
    ),

    # Load-bearing and non-obvious. A naive "elevage|eleveur" match sweeps in ~3,200
    # companies that are not heavy energy consumers and would waste client budget:
    #   eleveur chien chat        1,714 rows — pet breeders
    #   eleveur d oiseaux         1,541 rows — hobbyist aviaries (1,525 have emails,
    #                                          so this looks like a win until you read it)
    #   Fleuriste / Fleuriste Eco 1,153 rows — retail florists, not agriculture
    #
    # Word boundaries (\y) are required: without them 'chat' matches inside 'achat'
    # and 'marchand', wrongly disqualifying real prospects.
    #
    # KNOWN GAP (measured 2026-08-02): this is applied to naf_label ONLY, so 125
    # live businesses carry excluded vocabulary in their TRADE NAME while holding a
    # generic label that passes ('LES CANICHES DE ZALDIVAR - ELEVEUR DE CHIENS',
    # naf_label 'eleveur'). Reviewed and deliberately NOT auto-excluded: 10 of them
    # are tier-1 farms whose names merely contain an animal word (SEBASTIEN CHAT is
    # 01.41Z dairy — CHAT is the surname), and no lexical rule separates
    # 'FERME DU CHAT BLANC' from 'AU CHIEN BLEU'. See docs/m1_s9_progress.md.
    "exclude": (
        r"\y(chien|chiens|chat|chats|chaton|chatons|chiot|chiots|"
        r"oiseau|oiseaux|volatile|perroquet|canari|"
        r"fleuriste|fleuristes|"
        r"animalerie|toilettage|aquariophilie|aquarium|"
        r"reptile|reptiles|rongeur|rongeurs|nac)\y"
    ),

    # NOTE: horse breeding (haras, trotteurs, chevaux de sport) is deliberately
    # INCLUDED — docs/project_documentation.md:63 lists horse breeders as a target
    # sector. Revisit if the client says otherwise.
}


SECTORS = {
    _AGRI["key"]: _AGRI,
}

DEFAULT_SECTOR = _AGRI["key"]


def get_sector(key: str | None = None) -> dict:
    """Return one sector's rule set. Unknown keys fail loudly and list the
    valid ones — a typo must never silently fall back to another sector's
    rules and re-classify the wrong 128k rows."""
    key = key or DEFAULT_SECTOR
    if key not in SECTORS:
        raise SystemExit(
            f"Unknown sector '{key}'. Known sectors: {', '.join(sorted(SECTORS))}"
        )
    return SECTORS[key]


# ─── Backwards-compatible module-level names ─────────────────────────────────
# Kept so existing callers and ad-hoc scripts that did
# `from config.sector_rules import AGRI_INCLUDE` keep working. New code should
# use get_sector(...) instead — these only ever describe the DEFAULT sector.

SECTOR                = _AGRI["key"]
RULE_VERSION          = _AGRI["rule_version"]
NAF_PREFIXES_IN_SCOPE = _AGRI["naf_prefixes_in_scope"]
NAF_RESCUE_CODES      = _AGRI["naf_rescue_codes"]
NAF_RESCUE_PREFIXES   = _AGRI["naf_rescue_prefixes"]
NAF_HARD_EXCLUDE      = _AGRI["naf_hard_exclude"]
AGRI_INCLUDE          = _AGRI["include"]
AGRI_EXCLUDE          = _AGRI["exclude"]
