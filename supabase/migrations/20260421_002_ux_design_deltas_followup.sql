-- 20260421_002_ux_design_deltas_followup.sql
-- Follow-up to 20260421_001_ux_design_deltas.sql — closes gaps between
-- the truncated spec that informed 001 and the full Mac CC spec.
-- Target: legalai-dev (cfiaxrvtafszmgraftbk)
--
-- Adds:
--   1. extraction_candidates.matched_entity_type (paired with the
--      existing matched_entity_id from 001 to form the polymorphic
--      pre-confirmation match pointer — spec decision D4).
--   2. Two storage RLS policies on `case-documents` scoping each user
--      to their own folder by auth.uid() (spec decision D5).
--
-- Diagnostics verified before writing:
--   - pg_policies returned 0 rows for schema=storage (no conflicts)
--   - storage.foldername(text) -> text[] exists (native helper OK)
--
-- ADR-022: IF NOT EXISTS on column adds. Policies have no IF NOT EXISTS
-- in Postgres; we DROP IF EXISTS then CREATE for idempotency.

BEGIN;

-- 1. matched_entity_type pairs with matched_entity_id (added in 001).
ALTER TABLE extraction_candidates
  ADD COLUMN IF NOT EXISTS matched_entity_type text;

-- 2. RLS policies on storage.objects, scoped to `case-documents` bucket.
--    First-path-segment = auth.uid() means each user writes/reads inside
--    their own folder: /<uid>/<case_id>/<filename.pdf>.
--    No DELETE policy — service role only (D5).

DROP POLICY IF EXISTS "Users upload own case documents" ON storage.objects;
CREATE POLICY "Users upload own case documents"
  ON storage.objects FOR INSERT
  TO authenticated
  WITH CHECK (
    bucket_id = 'case-documents'
    AND (storage.foldername(name))[1] = auth.uid()::text
  );

DROP POLICY IF EXISTS "Users read own case documents" ON storage.objects;
CREATE POLICY "Users read own case documents"
  ON storage.objects FOR SELECT
  TO authenticated
  USING (
    bucket_id = 'case-documents'
    AND (storage.foldername(name))[1] = auth.uid()::text
  );

COMMIT;
