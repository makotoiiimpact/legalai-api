-- 20260421_001_ux_design_deltas.down.sql
-- Reverses 20260421_001_ux_design_deltas.sql.
-- WARNING: DROP COLUMN destroys any data written to the new columns.
--          Only safe on dev/clean projects — do not apply to production with
--          active data.

BEGIN;

-- 4. Storage bucket. storage.objects references buckets via bucket_id; the
--    DELETE will fail if non-empty, which is desired behavior (we don't
--    want to silently lose uploaded documents on a rollback).
DELETE FROM storage.buckets WHERE id = 'case-documents';

-- 3. Restore capture_events.source CHECK to pre-migration values.
ALTER TABLE capture_events DROP CONSTRAINT IF EXISTS capture_events_source_check;
ALTER TABLE capture_events
  ADD CONSTRAINT capture_events_source_check
  CHECK (source = ANY (ARRAY[
    'document_upload'::text,
    'voice_memo'::text,
    'email_forward'::text,
    'manual'::text,
    'calendar_sync'::text
  ]));

-- 2. Drop extraction_candidates added columns and their types.
ALTER TABLE extraction_candidates
  DROP COLUMN IF EXISTS corrected_role,
  DROP COLUMN IF EXISTS corrected_entity_id,
  DROP COLUMN IF EXISTS correction_type,
  DROP COLUMN IF EXISTS alternative_matches,
  DROP COLUMN IF EXISTS matched_entity_id,
  DROP COLUMN IF EXISTS review_status;
DROP TYPE IF EXISTS extraction_correction_type;
DROP TYPE IF EXISTS extraction_review_status;

-- 1. Drop cases.review_status column and type.
ALTER TABLE cases DROP COLUMN IF EXISTS review_status;
DROP TYPE IF EXISTS case_review_status;

COMMIT;
