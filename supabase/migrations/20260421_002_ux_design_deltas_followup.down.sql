-- 20260421_002_ux_design_deltas_followup.down.sql
-- Reverses 20260421_002_ux_design_deltas_followup.sql.

BEGIN;

-- 2. Drop RLS policies.
DROP POLICY IF EXISTS "Users read own case documents" ON storage.objects;
DROP POLICY IF EXISTS "Users upload own case documents" ON storage.objects;

-- 1. Drop matched_entity_type column.
ALTER TABLE extraction_candidates DROP COLUMN IF EXISTS matched_entity_type;

COMMIT;
