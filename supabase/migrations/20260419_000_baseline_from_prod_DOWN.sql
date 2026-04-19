/*
 * LegalAI Baseline Migration — DOWN (reverses 20260419_000_baseline_from_prod.sql)
 * Created: 2026-04-19
 * Status: DRAFT
 *
 * PURPOSE
 * -------
 * Returns the database to a completely empty `public` schema. Drops every
 * object created by the baseline UP migration, in reverse dependency order.
 *
 * WARNING — DESTRUCTIVE
 * ---------------------
 * Running this drops every table in the app's core data model. Any rows
 * in cases / documents / chunks / findings / disposition_memos / audit_log
 * are lost. Take a snapshot first:
 *   Supabase Dashboard → Database → Backups → Create snapshot
 *
 * NEVER run this against prod (kapyskpusteokxuaquwo). It exists only so
 * dev and CI can reset to a clean slate when re-running migration chains.
 *
 * EXTENSIONS ARE LEFT IN PLACE
 * ----------------------------
 * Intentional. Dropping pgcrypto, uuid-ossp, vector, or pg_stat_statements
 * would break other migrations that depend on them, plus the vector types
 * used by `chunks.embedding` may have dependent operators whose removal
 * cascades unpredictably. Extensions are cheap; keeping them is free.
 * If a fully clean teardown is required, drop the database itself.
 */

-- ============================================================================
-- 1. Views first (depend on tables)
-- ============================================================================
drop view if exists public.confidence_by_check_type;

-- ============================================================================
-- 2. Triggers (depend on tables + functions)
-- ============================================================================
drop trigger if exists cases_updated_at on public.cases;

-- ============================================================================
-- 3. Indexes — most drop automatically with their tables, but the ivfflat
-- and the unique-constraint-backing indexes are called out for clarity.
-- (The explicit drops are no-ops after the table drops below; they're here
-- in case a partial apply leaves orphan indexes.)
-- ============================================================================
drop index if exists public.audit_log_case_id_idx;
drop index if exists public.audit_log_created_at_idx;
drop index if exists public.chunks_case_id_idx;
drop index if exists public.chunks_embedding_idx;
drop index if exists public.findings_case_id_idx;
drop index if exists public.findings_check_type_idx;

-- ============================================================================
-- 4. Tables (reverse FK order — audit_log first since it has no outbound FKs,
--    then the leaf FK-holders, then cases last)
-- ============================================================================
drop table if exists public.audit_log cascade;
drop table if exists public.disposition_memos cascade;
drop table if exists public.findings cascade;
drop table if exists public.chunks cascade;
drop table if exists public.documents cascade;
drop table if exists public.cases cascade;

-- ============================================================================
-- 5. Functions
-- ============================================================================
drop function if exists public.match_chunks(
  query_embedding vector,
  target_case_id uuid,
  match_count integer,
  similarity_threshold double precision
);
drop function if exists public.update_updated_at();

-- ============================================================================
-- 6. Extensions — NOT dropped (see header comment).
-- ============================================================================

-- ============================================================================
-- End baseline DOWN migration.
-- ============================================================================
