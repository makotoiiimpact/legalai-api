-- DOWN migration for 20260420_001_attorney_seed_prep
BEGIN;

DROP INDEX IF EXISTS idx_attorneys_external_ids;
DROP INDEX IF EXISTS idx_judges_external_ids;
DROP INDEX IF EXISTS idx_prosecutors_external_ids;
DROP INDEX IF EXISTS idx_officers_external_ids;
DROP INDEX IF EXISTS idx_courts_external_ids;
DROP INDEX IF EXISTS idx_agencies_external_ids;
DROP INDEX IF EXISTS idx_witnesses_external_ids;
DROP INDEX IF EXISTS idx_experts_external_ids;

ALTER TABLE attorneys   DROP COLUMN IF EXISTS external_ids;
ALTER TABLE judges      DROP COLUMN IF EXISTS external_ids;
ALTER TABLE prosecutors DROP COLUMN IF EXISTS external_ids;
ALTER TABLE officers    DROP COLUMN IF EXISTS external_ids;
ALTER TABLE courts      DROP COLUMN IF EXISTS external_ids;
ALTER TABLE agencies    DROP COLUMN IF EXISTS external_ids;
ALTER TABLE witnesses   DROP COLUMN IF EXISTS external_ids;
ALTER TABLE experts     DROP COLUMN IF EXISTS external_ids;

ALTER TABLE attorneys DROP COLUMN IF EXISTS bar_state;
ALTER TABLE attorneys DROP COLUMN IF EXISTS bar_number;
ALTER TABLE attorneys DROP COLUMN IF EXISTS is_firm_member;

ALTER TABLE case_attorneys DROP COLUMN IF EXISTS attribution_confidence;

DROP TYPE IF EXISTS attribution_confidence;

COMMIT;
