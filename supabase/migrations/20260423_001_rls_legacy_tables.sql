-- =============================================
-- RLS Migration: Legacy Tables
-- ADR-013 resolution (partial — USING(true) until multi-tenant)
-- Applied to: cfiaxrvtafszmgraftbk (dev)
-- =============================================

-- =============================================
-- TIER A: Firm-private tables
-- Pattern: authenticated can CRUD, no anon access
-- No delete policy — soft-delete via deleted_at
-- =============================================

-- CASES (most critical — contains client data)
ALTER TABLE cases ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'cases' AND policyname = 'cases_select_authenticated'
  ) THEN
    CREATE POLICY cases_select_authenticated ON cases
      FOR SELECT TO authenticated USING (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'cases' AND policyname = 'cases_insert_authenticated'
  ) THEN
    CREATE POLICY cases_insert_authenticated ON cases
      FOR INSERT TO authenticated WITH CHECK (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'cases' AND policyname = 'cases_update_authenticated'
  ) THEN
    CREATE POLICY cases_update_authenticated ON cases
      FOR UPDATE TO authenticated
      USING (true) WITH CHECK (true);
  END IF;
END $$;

-- DOCUMENTS
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'documents' AND policyname = 'documents_select_authenticated'
  ) THEN
    CREATE POLICY documents_select_authenticated ON documents
      FOR SELECT TO authenticated USING (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'documents' AND policyname = 'documents_insert_authenticated'
  ) THEN
    CREATE POLICY documents_insert_authenticated ON documents
      FOR INSERT TO authenticated WITH CHECK (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'documents' AND policyname = 'documents_update_authenticated'
  ) THEN
    CREATE POLICY documents_update_authenticated ON documents
      FOR UPDATE TO authenticated
      USING (true) WITH CHECK (true);
  END IF;
END $$;

-- CHUNKS
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'chunks' AND policyname = 'chunks_select_authenticated'
  ) THEN
    CREATE POLICY chunks_select_authenticated ON chunks
      FOR SELECT TO authenticated USING (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'chunks' AND policyname = 'chunks_insert_authenticated'
  ) THEN
    CREATE POLICY chunks_insert_authenticated ON chunks
      FOR INSERT TO authenticated WITH CHECK (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'chunks' AND policyname = 'chunks_update_authenticated'
  ) THEN
    CREATE POLICY chunks_update_authenticated ON chunks
      FOR UPDATE TO authenticated
      USING (true) WITH CHECK (true);
  END IF;
END $$;

-- FINDINGS
ALTER TABLE findings ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'findings' AND policyname = 'findings_select_authenticated'
  ) THEN
    CREATE POLICY findings_select_authenticated ON findings
      FOR SELECT TO authenticated USING (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'findings' AND policyname = 'findings_insert_authenticated'
  ) THEN
    CREATE POLICY findings_insert_authenticated ON findings
      FOR INSERT TO authenticated WITH CHECK (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'findings' AND policyname = 'findings_update_authenticated'
  ) THEN
    CREATE POLICY findings_update_authenticated ON findings
      FOR UPDATE TO authenticated
      USING (true) WITH CHECK (true);
  END IF;
END $$;

-- DISPOSITION_MEMOS
ALTER TABLE disposition_memos ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'disposition_memos' AND policyname = 'disposition_memos_select_authenticated'
  ) THEN
    CREATE POLICY disposition_memos_select_authenticated ON disposition_memos
      FOR SELECT TO authenticated USING (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'disposition_memos' AND policyname = 'disposition_memos_insert_authenticated'
  ) THEN
    CREATE POLICY disposition_memos_insert_authenticated ON disposition_memos
      FOR INSERT TO authenticated WITH CHECK (true);
  END IF;
END $$;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'disposition_memos' AND policyname = 'disposition_memos_update_authenticated'
  ) THEN
    CREATE POLICY disposition_memos_update_authenticated ON disposition_memos
      FOR UPDATE TO authenticated
      USING (true) WITH CHECK (true);
  END IF;
END $$;

-- =============================================
-- TIER B: Reference data tables
-- Pattern: authenticated can SELECT only
-- INSERT/UPDATE/DELETE = service role only
-- =============================================

-- COURTS
ALTER TABLE courts ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'courts' AND policyname = 'courts_select_authenticated'
  ) THEN
    CREATE POLICY courts_select_authenticated ON courts
      FOR SELECT TO authenticated USING (true);
  END IF;
END $$;

-- MOTION_TYPES
ALTER TABLE motion_types ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'motion_types' AND policyname = 'motion_types_select_authenticated'
  ) THEN
    CREATE POLICY motion_types_select_authenticated ON motion_types
      FOR SELECT TO authenticated USING (true);
  END IF;
END $$;

-- =============================================
-- TIER C: Audit log — service role only
-- No policies = no authenticated access
-- =============================================

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
-- Intentionally no policies. Service role bypasses RLS.
-- Authenticated users cannot read or write audit_log directly.

-- =============================================
-- Storage policy fixes (same session per spec)
-- - Wrap auth.uid() in subquery for performance
-- - Add DELETE policy for case-documents
-- =============================================

DROP POLICY IF EXISTS "Users read own case documents" ON storage.objects;
CREATE POLICY "Users read own case documents" ON storage.objects
  FOR SELECT USING (
    bucket_id = 'case-documents'
    AND (storage.foldername(name))[1] = (SELECT auth.uid())::text
  );

DROP POLICY IF EXISTS "Users upload own case documents" ON storage.objects;
CREATE POLICY "Users upload own case documents" ON storage.objects
  FOR INSERT WITH CHECK (
    bucket_id = 'case-documents'
    AND (storage.foldername(name))[1] = (SELECT auth.uid())::text
  );

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'storage'
      AND tablename = 'objects'
      AND policyname = 'Users delete own case documents'
  ) THEN
    CREATE POLICY "Users delete own case documents" ON storage.objects
      FOR DELETE USING (
        bucket_id = 'case-documents'
        AND (storage.foldername(name))[1] = (SELECT auth.uid())::text
      );
  END IF;
END $$;
