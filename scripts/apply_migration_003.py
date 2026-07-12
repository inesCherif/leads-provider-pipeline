"""Apply migration 003 — add SIRENE enrichment tracking columns to staging.companies"""
import os
from pathlib import Path
from dotenv import load_dotenv
import psycopg2

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")
conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"))
conn.autocommit = True
cur = conn.cursor()

cur.execute("""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='staging' AND table_name='companies' AND column_name='sirene_last_checked_at') THEN
        ALTER TABLE staging.companies ADD COLUMN sirene_last_checked_at TIMESTAMPTZ;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='staging' AND table_name='companies' AND column_name='sirene_etat') THEN
        ALTER TABLE staging.companies ADD COLUMN sirene_etat CHAR(1) CHECK (sirene_etat IN ('A','F'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema='staging' AND table_name='companies' AND column_name='creation_date') THEN
        ALTER TABLE staging.companies ADD COLUMN creation_date DATE;
    END IF;
END
$$;
""")
print("Columns added OK")

cur.execute("""
CREATE INDEX IF NOT EXISTS idx_companies_not_enriched
    ON staging.companies(siren)
    WHERE siren IS NOT NULL AND sirene_last_checked_at IS NULL;
""")
print("Index created OK")

cur.execute("""
SELECT column_name FROM information_schema.columns
WHERE table_schema='staging' AND table_name='companies'
ORDER BY ordinal_position;
""")
print("staging.companies columns:", [r[0] for r in cur.fetchall()])
conn.close()
print("Migration 003 complete.")
