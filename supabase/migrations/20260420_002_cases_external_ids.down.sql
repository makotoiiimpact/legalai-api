-- DOWN migration for 20260420_002_cases_external_ids
BEGIN;

DROP INDEX IF EXISTS idx_cases_external_ids;
ALTER TABLE cases DROP COLUMN IF EXISTS external_ids;

COMMIT;
