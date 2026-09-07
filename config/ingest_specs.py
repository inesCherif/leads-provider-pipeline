"""
Ingestion specs — one entry per source file (M1-S3 / M4)
=========================================================
A spec is the CONTRACT between a source file and the staging schema. It is
the only place a file's shape is declared; m1_s3_ingest.py is generic.

Keys
----
filename         exact file name under DATA_DIR (spaces included)
sheet            sheet name or index
sector           staging.source_files.sector — MUST appear in some
                 config.sector_rules.SECTORS[*]["source_sectors"], otherwise
                 no qualification pass will ever select these rows
                 (check_data_quality.py asserts this)
pipeline_stage   raw | siret_matched | verified | unknown
data_source      free text provenance, written to source_files.data_source
collected_at     ISO date or None
notes            human observations
col_map          {source column: canonical field}. A mapped column that is
                 missing from the file STOPS the import (column contract).
                 Blank headers arrive from calamine as 'Unnamed: <index>'.
required         columns whose absence stops the import even if unmapped
phone_columns    ordered list of source columns holding phones; the first
                 normalisable one becomes contacts.phone_main, the next
                 phone_alt, ALL of them become contact_points rows
email_columns    source columns holding e-mail addresses
attribute_columns  source columns copied verbatim into
                 staging.company_attributes.attrs (sector-specific long tail)
identifier_columns {id_type: source column} -> staging.company_identifiers
pre_clean        name of a row hook in m1_s3_ingest.PRE_CLEAN applied to the
                 canonical dict before normalisation (file-specific repairs)
skip             True = listed for the record but never loaded (Camping)

Canonical fields understood by the loader
-----------------------------------------
trade_name legal_name contact_full_name address_line1 address_line2
postal_code department city phone_main phone_alt siret siren siret_alt
email_address naf_label naf_code_source status_raw creation_date_raw
effectif_raw source_file_raw pj_detail_url
"""

# The four call-centre outcome columns + PHONE_NUMBER1 are EMPTY in every
# provider file (measured 2026-09-07). They stay in raw_json only.
_PROVIDER_COMMON = {
    "SOCIETE":   "trade_name",
    "DIRIGEANT": "contact_full_name",
    "ADRESSE1":  "address_line1",
    "ADRESSE2":  "address_line2",
    "CP":        "postal_code",
    "VILLE":     "city",
    "TELEPHONE": "phone_main",
    "MOBILE":    "phone_alt",
    "NAF":       "naf_code_source",
    "SIRET":     "siret",
    "EFFECTIF":  "effectif_raw",
    "EMAIL":     "email_address",
    "ACTIVITE":  "naf_label",
}

FILE_SPECS = [
    # ── Sector 1: agriculture (already ingested 2026-07-12; kept so the loader
    #    can prove itself by SKIPPING them on hash) ────────────────────────────
    {
        "filename": "Copie de agriculteurs total.xlsx",
        "sheet": 0,
        "sector": "agriculture",
        "pipeline_stage": "raw",
        "data_source": "client Excel (provider), file A",
        "collected_at": None,
        "notes": "Independent raw source: French farming businesses.",
        # The 5th header is BLANK in this file (calamine: 'Unnamed: 4'); the
        # old spec mapped it as 'dep', which the column contract (added
        # 2026-08-02, after the ingest) would refuse. Department is derived
        # from the postal code in the views, so it is simply not mapped.
        "col_map": {
            "Societe":          "trade_name",
            "Responsable":      "contact_full_name",
            "Adresse":          "address_line1",
            "CP":               "postal_code",
            "Ville":            "city",
            "Telephone":        "phone_main",
            "Siret":            "siret",
            "Email":            "email_address",
            "Activite":         "naf_label",
            "Statut_Activite":  "status_raw",
            "Nom_Officiel":     "legal_name",
        },
        "required": {"Societe", "Adresse", "CP", "Ville", "Siret", "Email",
                     "Activite", "Statut_Activite"},
        "phone_columns": ["Telephone", "Phone_number"],
        "email_columns": ["Email"],
        "attribute_columns": [],
        "identifier_columns": {},
        "pre_clean": None,
    },
    {
        "filename": "Copie de Eleveurs_verified (liste de toutes les eleveurs avec Siret ).xlsx",
        "sheet": 0,
        "sector": "livestock",
        "pipeline_stage": "verified",
        "data_source": "client Excel (provider), file C",
        "collected_at": None,
        "notes": "Primary éleveurs source. Has Siret_Verifie.",
        "col_map": {
            "Societe":          "trade_name",
            "Responsable":      "contact_full_name",
            "Adresse":          "address_line1",
            "CP":               "postal_code",
            "dep":              "department",
            "Ville":            "city",
            "Telephone":        "phone_main",
            "Phone_number":     "phone_alt",
            "Siret":            "siret_alt",
            "Siret_Verifie":    "siret",
            "Email":            "email_address",
            "Activite":         "naf_label",
            "Statut_Activite":  "status_raw",
            "Nom_Officiel":     "legal_name",
        },
        "required": {"Societe", "Adresse", "CP", "Ville", "Siret", "Siret_Verifie",
                     "Email", "Activite", "Statut_Activite"},
        "phone_columns": ["Telephone", "Phone_number"],
        "email_columns": ["Email"],
        "attribute_columns": [],
        "identifier_columns": {},
        "pre_clean": None,
    },

    # ── M4 provider files (Mehdi, 2026-09-07) ───────────────────────────────
    {
        "filename": "Copie de base imprimerie scrapé.xlsx",
        "sheet": "Feuil1",
        "sector": "imprimerie",
        "pipeline_stage": "raw",
        "data_source": "provider scrape (call-centre dialect), printing shops",
        "collected_at": None,
        "notes": ("9,182 rows, no SIRET, no e-mail, no street. City is in "
                  "ADRESSE1 on 5,020 rows (ADRESSE1/VILLE mutually exclusive). "
                  "ACTIVITE is the constant 'Imprimerie' with an NBSP prefix. "
                  "CP and TELEPHONE stored as floats."),
        "col_map": {
            "SOCIETE":    "trade_name",
            "ADRESSE1":   "address_line1",
            "CP":         "postal_code",
            "Unnamed: 5": "department",
            "VILLE":      "city",
            "TELEPHONE":  "phone_main",
            "ACTIVITE":   "naf_label",
        },
        "required": {"SOCIETE", "CP", "TELEPHONE", "ACTIVITE"},
        "phone_columns": ["TELEPHONE"],
        "email_columns": [],
        "attribute_columns": [],
        "identifier_columns": {},
        "pre_clean": "imprimerie_city",
    },
    {
        "filename": "Copie de pagesjaunes_viticulteur_gironde.xlsx",
        "sheet": "pagesjaunes_viticulteur_gironde",
        "sector": "viticulture",
        "pipeline_stage": "raw",
        "data_source": "Pages Jaunes scrape, viticulteurs Gironde",
        "collected_at": None,
        "notes": ("2,534 rows -> ~1,630 after in-file dedup (896 exact duplicate "
                  "rows). No SIRET, no e-mail, no contact name. detailUrl is "
                  "the PJ listing id; 187 rows carry the placeholder "
                  "'pagesjaunes.fr#'. Accents preserved (the base is ASCII-folded)."),
        "col_map": {
            "name":      "trade_name",
            "address":   "address_line1",
            "zipcode":   "postal_code",
            "city":      "city",
            "phone":     "phone_main",
            "mobile":    "phone_alt",
            "detailUrl": "pj_detail_url",
            "category":  "naf_label",
        },
        "required": {"name", "zipcode", "city", "phone", "detailUrl", "category"},
        "phone_columns": ["phone", "mobile"],
        "email_columns": [],
        "attribute_columns": ["full_address", "category"],
        "identifier_columns": {"pj_listing_id": "pj_detail_url"},
        "pre_clean": "viticulteur_pj",
    },
    {
        "filename": "Copie de boulang_patisserie.xlsx",
        "sheet": "ACTIFS",
        "sector": "boulangerie",
        "pipeline_stage": "siret_matched",
        "data_source": "provider (boulangerie.csv, client_boulan, hr_boulang) + registry enrichment columns",
        "collected_at": None,
        "notes": ("10,685 rows nationwide. siren_enrichi 100% (the key); SIRET "
                  "91% (920 placeholders '0'); 508 e-mails with a space, 86 glued. "
                  "Contaminated: 6820B SCIs, 0161Z farms, 5610C restaurants — "
                  "the boulangerie rule set hard-excludes them."),
        "col_map": {
            **_PROVIDER_COMMON,
            "SOURCE_FILE":           "source_file_raw",
            "statut_enrichi":        "status_raw",
            "denomination_enrichi":  "legal_name",
            "siren_enrichi":         "siren",
            "siret_enrichi":         "siret_alt",
            "date_creation_enrichi": "creation_date_raw",
        },
        "required": {"SOCIETE", "CP", "VILLE", "TELEPHONE", "SIRET", "EMAIL",
                     "ACTIVITE", "siren_enrichi"},
        "phone_columns": ["TELEPHONE", "MOBILE", "PHONE_NUMBER"],
        "email_columns": ["EMAIL"],
        "attribute_columns": ["SOURCE_FILE", "EFFECTIF", "effectif_enrichi",
                              "dirigeants_enrichi", "NAF"],
        "identifier_columns": {},
        "pre_clean": "boulang_keys",
    },
    {
        "filename": "Copie de data_finale sans croisement .xlsx",
        "sheet": 0,
        "sector": "tourisme",
        "pipeline_stage": "raw",
        "data_source": "provider (19 source files, SOURCE_FILE column), hospitality",
        "collected_at": None,
        "notes": ("50,849 rows: chambres d'hôtes, gîtes, hôtels, résidences, "
                  "campings, centres équestres. SIRET 72% (14,175 placeholders "
                  "'0.0', 126 truncated); 1,181 glued e-mail domains; 390 "
                  "premium-rate phones; 2 fully shifted rows; ASCII-folded."),
        "col_map": {
            **_PROVIDER_COMMON,
            "SOURCE_FILE": "source_file_raw",
            "Unnamed: 6":  "department",
        },
        "required": {"SOCIETE", "CP", "VILLE", "TELEPHONE", "SIRET", "EMAIL",
                     "ACTIVITE", "NAF"},
        "phone_columns": ["TELEPHONE", "MOBILE"],
        "email_columns": ["EMAIL"],
        "attribute_columns": ["SOURCE_FILE", "EFFECTIF", "NAF"],
        "identifier_columns": {},
        "pre_clean": "provider_address",
    },
    {
        "filename": "Copie de Camping.xlsx",
        "sheet": 0,
        "sector": "tourisme",
        "pipeline_stage": "raw",
        "data_source": "provider, campings",
        "collected_at": None,
        "notes": ("SKIPPED (Ines, 2026-09-07): no SIRET, no e-mail, CP wrong on "
                  "26% of rows (phone fragments), 41.5% rows with irreversible "
                  "mojibake, 434 of 890 phones already in data_finale."),
        "col_map": {},
        "required": set(),
        "phone_columns": [],
        "email_columns": [],
        "attribute_columns": [],
        "identifier_columns": {},
        "pre_clean": None,
        "skip": True,
    },
]


def spec_for(filename: str) -> dict:
    for spec in FILE_SPECS:
        if spec["filename"] == filename:
            return spec
    raise SystemExit(f"No ingest spec for '{filename}'. Known files:\n  "
                     + "\n  ".join(s["filename"] for s in FILE_SPECS))
