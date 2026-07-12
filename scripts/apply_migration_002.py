"""Apply migration 002 — add collected_at and data_source to staging.source_files"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import psycopg2

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

conn = psycopg2.connect(os.getenv("SUPABASE_DB_URL"))
conn.autocommit = True
cur = conn.cursor()

# 1. Add columns if not present
cur.execute("""
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='staging' AND table_name='source_files' AND column_name='collected_at'
    ) THEN
        ALTER TABLE staging.source_files ADD COLUMN collected_at DATE;
        RAISE NOTICE 'Added collected_at column';
    ELSE
        RAISE NOTICE 'collected_at already exists';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='staging' AND table_name='source_files' AND column_name='data_source'
    ) THEN
        ALTER TABLE staging.source_files ADD COLUMN data_source TEXT;
        RAISE NOTICE 'Added data_source column';
    ELSE
        RAISE NOTICE 'data_source already exists';
    END IF;
END
$$;
""")
print("Step 1: ALTER TABLE done")

# 2. Backfill pilot files
cur.execute("""
UPDATE staging.source_files
SET
    collected_at = '2026-07-05',
    data_source  = 'CEO pilot files'
WHERE file_name IN (
    'Copie de agriculteurs total.xlsx',
    'Copie de Eleveurs_verified (liste de toutes les eleveurs avec Siret ).xlsx'
)
AND collected_at IS NULL;
""")
print(f"Step 2: {cur.rowcount} rows backfilled")

# 3. Verify
cur.execute("""
SELECT file_name, pipeline_stage, sector, collected_at::text, data_source, row_count_imported
FROM staging.source_files
ORDER BY created_at;
""")
rows = cur.fetchall()
print("\nVerification:")
print(f"{'file_name':<60} {'pipeline':<15} {'sector':<15} {'collected_at':<15} {'data_source':<20} {'rows'}")
print("-"*130)
for r in rows:
    print(f"{str(r[0]):<60} {str(r[1]):<15} {str(r[2]):<15} {str(r[3]):<15} {str(r[4]):<20} {r[5]}")

conn.close()
print("\nMigration 002 complete.")
