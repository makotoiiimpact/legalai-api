/*
 * LegalAI Behavioral Intelligence Layer - DOWN Migration
 * Created: 2026-04-20
 * Status: DRAFT - DO NOT EXECUTE WITHOUT REVIEW
 * Reverses: 20260420_behavioral_intelligence_layer.sql
 *
 * WARNING: This is DESTRUCTIVE. Running this migration will drop every table,
 * column, index, and trigger created by the UP migration. Any data in those
 * tables will be lost.
 *
 * Before running DOWN:
 *   1. Snapshot the database (Supabase dashboard → Database → Backups → "Create snapshot")
 *   2. Export any firm-private data you want to retain (entity_observations,
 *      motions, plea_offers, captures) — these are the highest-value rows and
 *      there is no recovery without a backup
 *   3. Confirm no downstream application code references the dropped columns
 *      on `cases` (data_tier, firm_id) — DOWN removes them
 *   4. Run in a Supabase branch / staging project first
 *
 * The DOWN migration does NOT drop the pgcrypto extension (it's commonly
 * used elsewhere) and does NOT drop the set_updated_at() function (other
 * migrations may depend on it). If those need to go, remove them manually
 * after verifying nothing else uses them.
 */

-- ============================================================================
-- LegalAI — Behavioral Intelligence Layer (DOWN migration)
-- Drop order is the reverse of creation to respect FK dependencies.
-- ============================================================================

-- 1. Capture pipeline
drop table if exists public.extraction_candidates cascade;
drop table if exists public.capture_events cascade;

-- 2. Intelligence aggregation
drop table if exists public.entity_observations cascade;
drop table if exists public.officer_patterns cascade;
drop table if exists public.matchup_history cascade;
drop table if exists public.prosecutor_patterns cascade;
drop table if exists public.judge_motion_stats cascade;

-- 3. Event tables (drop hearings FK on motions first)
do $$
begin
  if exists (
    select 1 from pg_constraint where conname = 'motions_hearing_id_fkey'
  ) then
    alter table public.motions drop constraint motions_hearing_id_fkey;
  end if;
end$$;

drop table if exists public.plea_offers cascade;
drop table if exists public.hearings cascade;
drop table if exists public.motions cascade;
drop table if exists public.motion_types cascade;

-- 4. Relationship tables
drop table if exists public.case_devices cascade;
drop table if exists public.case_witnesses cascade;
drop table if exists public.case_officers cascade;
drop table if exists public.case_attorneys cascade;

-- 5. Core entity tables (reverse of creation order so FKs drop cleanly)
drop table if exists public.experts cascade;
drop table if exists public.witnesses cascade;
alter table if exists public.officers drop constraint if exists officers_public_record_only_check;
drop table if exists public.officers cascade;
drop table if exists public.prosecutors cascade;
drop table if exists public.judges cascade;
drop table if exists public.attorneys cascade;
drop table if exists public.devices cascade;
drop table if exists public.agencies cascade;
drop table if exists public.courts cascade;

-- 6. Reverse ALTER TABLE additions on cases
alter table if exists public.cases drop column if exists data_tier;
alter table if exists public.cases drop column if exists firm_id;
drop index if exists public.idx_cases_data_tier;
drop index if exists public.idx_cases_firm_id;

-- 7. Leave pgcrypto and set_updated_at() alone (shared utility).
--    If you want to drop them, uncomment the lines below after confirming no
--    other migrations depend on them:
--
--    drop function if exists public.set_updated_at() cascade;
--    drop extension if exists "pgcrypto";

-- ============================================================================
-- End DOWN migration.
-- ============================================================================
