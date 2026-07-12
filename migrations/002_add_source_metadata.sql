-- ─────────────────────────────────────────────────────────────────────────────
-- Migration 002 — Add collection metadata to staging.source_files
-- ─────────────────────────────────────────────────────────────────────────────
-- Adds two new columns:
--   collected_at  DATE  : the real-world date the file was collected/received
--                         (NOT the DB insertion date, which is created_at)
--   data_source   TEXT  : human label for who produced the data
--                         e.g. 'CEO pilot files', 'web scrape', 'purchased list'
--
-- Backfills the 2 pilot files already ingested (folder = "05 juillet 2026").
-- ─────────────────────────────────────────────────────────────────────────────

-- 1. Add columns (idempotent via IF NOT EXISTS equivalent pattern)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'staging'
          AND table_name   = 'source_files'
          AND column_name  = 'collected_at'
    ) THEN
        ALTER TABLE staging.source_files ADD COLUMN collected_at DATE;
        COMMENT ON COLUMN staging.source_files.collected_at IS
            'Real-world date the file was collected or received. Distinct from created_at (DB insert date).';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'staging'
          AND table_name   = 'source_files'
          AND column_name  = 'data_source'
    ) THEN
        ALTER TABLE staging.source_files ADD COLUMN data_source TEXT;
        COMMENT ON COLUMN staging.source_files.data_source IS
            'Human-readable label describing who produced or provided this file, e.g. CEO pilot files, web scrape, purchased list.';
    END IF;
END
$$;

-- 2. Backfill the two pilot files (folder name = "Data Globale 05 juillet 2026")
UPDATE staging.source_files
SET
    collected_at = '2026-07-05',
    data_source  = 'CEO pilot files'
WHERE file_name IN (
    'Copie de agriculteurs total.xlsx',
    'Copie de Eleveurs_verified (liste de toutes les eleveurs avec Siret ).xlsx'
)
  AND collected_at IS NULL;

-- 3. Verify
SELECT
    file_name,
    pipeline_stage,
    sector,
    collected_at,
    data_source,
    row_count_imported,
    created_at
FROM staging.source_files
ORDER BY created_at;
