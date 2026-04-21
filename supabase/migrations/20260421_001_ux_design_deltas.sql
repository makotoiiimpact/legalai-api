-- 20260421_001_ux_design_deltas.sql
-- Backend deltas from UX Design v1 spec (2026-04-20).
-- Target: legalai-dev (cfiaxrvtafszmgraftbk)
-- ADR-022: Rule 9 idempotent house style — IF NOT EXISTS on ALTERs.
--          ENUMs use bare CREATE TYPE per spec: loud failure on accidental
--          re-create is preferred.
--
-- Deviation from spec Step 3: the UX spec referenced a "source_type enum",
-- but on this project `capture_events.source` is `text` constrained by a
-- CHECK constraint (not a real enum type). We swap the CHECK constraint
-- instead of ALTER TYPE ADD VALUE. Same intent, correct mechanics.

BEGIN;

-- 1. review_status enum for cases table.
CREATE TYPE case_review_status AS ENUM (
  'processing', 'needs_review', 'in_review', 'confirmed', 'shell'
);
ALTER TABLE cases
  ADD COLUMN IF NOT EXISTS review_status case_review_status DEFAULT 'shell';

-- 2. extraction_candidates review columns.
CREATE TYPE extraction_review_status AS ENUM (
  'pending', 'confirmed', 'rejected', 'edited'
);
CREATE TYPE extraction_correction_type AS ENUM (
  'wrong_person', 'wrong_role', 'not_entity'
);
ALTER TABLE extraction_candidates
  ADD COLUMN IF NOT EXISTS review_status extraction_review_status DEFAULT 'pending',
  ADD COLUMN IF NOT EXISTS matched_entity_id uuid,
  ADD COLUMN IF NOT EXISTS alternative_matches jsonb DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS correction_type extraction_correction_type,
  ADD COLUMN IF NOT EXISTS corrected_entity_id uuid,
  ADD COLUMN IF NOT EXISTS corrected_role text;
-- matched_entity_id is intentionally NOT FK'd — it's polymorphic (could point
-- to attorneys, judges, prosecutors, officers, witnesses, experts). Same
-- pattern as entity_observations.

-- 3. Add 'image_upload' to capture_events.source valid values.
--    See file header for deviation note.
ALTER TABLE capture_events DROP CONSTRAINT IF EXISTS capture_events_source_check;
ALTER TABLE capture_events
  ADD CONSTRAINT capture_events_source_check
  CHECK (source = ANY (ARRAY[
    'document_upload'::text,
    'voice_memo'::text,
    'email_forward'::text,
    'manual'::text,
    'calendar_sync'::text,
    'image_upload'::text
  ]));

-- 4. Supabase Storage bucket for case documents (private).
--    Private per security posture for attorney case documents.
INSERT INTO storage.buckets (id, name, public)
VALUES ('case-documents', 'case-documents', false)
ON CONFLICT (id) DO NOTHING;

COMMIT;
