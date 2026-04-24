/*
 * LegalAI Baseline Migration — Retroactive Prod Snapshot
 * Created: 2026-04-19
 * Status: DRAFT — do not execute against production (see below).
 *
 * PURPOSE
 * -------
 * Captures the state of the prod Supabase project (kapyskpusteokxuaquwo)
 * as of 2026-04-19. Prior to this file, the prod schema was built ad-hoc
 * via the Supabase dashboard SQL editor and was not under migration
 * version control. Per ADR-012, this file establishes the first tracked
 * migration — all future schema changes go through migration files, not
 * ad-hoc DDL.
 *
 * USAGE
 * -----
 * - Apply to any EMPTY legalai database (dev, CI, local) to reproduce
 *   the current prod schema.
 * - DO NOT apply to prod (kapyskpusteokxuaquwo) — prod already has these
 *   objects. Running would produce `relation already exists` errors on
 *   non-IF-NOT-EXISTS statements; even with the guards present, it's a
 *   belt-and-suspenders violation of the "no ad-hoc DDL in prod" rule.
 *   If prod needs to be marked as having this migration applied, do it
 *   through the Supabase migration history table directly.
 *
 * RLS POSTURE (ADR-013)
 * ---------------------
 * This baseline intentionally does NOT enable RLS or create policies on
 * any of the 6 tables. That matches prod's current state (rowsecurity=false
 * across the board, zero policies). The API layer relies on service-role
 * keys for all DB access. Enabling RLS is a separate initiative that must
 * land AFTER this baseline; it's not a quiet side-effect of "just getting
 * dev in sync."
 *
 * ORDER OF OPERATIONS
 * -------------------
 *   1. Extensions           (pgcrypto, uuid-ossp, vector, pg_stat_statements)
 *   2. Tables (FK-safe)     (cases → documents → chunks → findings → disposition_memos → audit_log)
 *   3. Functions            (update_updated_at, match_chunks — MUST come after tables
 *                            because match_chunks is a SQL-language function whose body
 *                            is parsed at CREATE time and references `chunks`)
 *   4. Non-PK indexes       (7 total including pgvector ivfflat)
 *   5. Triggers             (1: cases_updated_at)
 *   6. Views                (1: confidence_by_check_type)
 *
 * IDEMPOTENCY
 * -----------
 * Every DDL uses IF NOT EXISTS or CREATE OR REPLACE where supported so
 * re-running against a partially-seeded dev environment is safe. The
 * intended use is still a single clean apply on an empty project.
 */

-- ============================================================================
-- 1. Extensions
-- ============================================================================
-- Supabase-managed extensions (plpgsql, pg_graphql, supabase_vault) are
-- NOT declared here — they're provisioned by the platform and re-declaring
-- them is a no-op at best and a permission error at worst.
create extension if not exists "pgcrypto";
create extension if not exists "uuid-ossp";
create extension if not exists "vector";
create extension if not exists "pg_stat_statements";

-- ============================================================================
-- 2. Tables (created in FK-safe order)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- cases — top of the graph. Every other table either references this or the
-- transitive closure of it (via documents).
-- ----------------------------------------------------------------------------
create table if not exists public.cases (
  id              uuid primary key default gen_random_uuid(),
  case_number     text not null,
  client_name     text not null,
  case_type       text,
  charge          text,
  charge_severity text,
  incident_date   date,
  jurisdiction    text default 'Clark County, NV'::text,
  status          text default 'intake'::text,
  paralegal_id    text,
  notes           text,
  created_at      timestamptz default now(),
  updated_at      timestamptz default now(),
  constraint cases_case_number_key unique (case_number)
);

-- ----------------------------------------------------------------------------
-- documents — uploaded files attached to a case. Raw text extracted on ingest.
-- ----------------------------------------------------------------------------
create table if not exists public.documents (
  id            uuid primary key default gen_random_uuid(),
  case_id       uuid references public.cases(id) on delete cascade,
  name          text not null,
  doc_type      text,
  storage_path  text,
  file_size_kb  integer,
  page_count    integer,
  indexed       boolean default false,
  indexed_at    timestamptz,
  chunk_count   integer default 0,
  raw_text      text,
  created_at    timestamptz default now()
);

-- ----------------------------------------------------------------------------
-- chunks — pgvector embeddings for RAG. case_id IS NULL rows are the global
-- knowledge base (NRS, bench book, etc.); case_id IS NOT NULL rows are tied
-- to a specific case's documents. match_chunks() unions both.
-- ----------------------------------------------------------------------------
create table if not exists public.chunks (
  id          uuid primary key default gen_random_uuid(),
  document_id uuid references public.documents(id) on delete cascade,
  case_id     uuid references public.cases(id) on delete cascade,
  content     text not null,
  embedding   vector(1536),                       -- OpenAI text-embedding-3-small; matches prod exactly
  chunk_index integer,
  token_count integer,
  metadata    jsonb,
  created_at  timestamptz default now()
);

-- ----------------------------------------------------------------------------
-- findings — AI-produced answers to case-analysis checks. HIL review state
-- lives here (hil_status, edited_answer, reviewed_by/at).
-- ----------------------------------------------------------------------------
create table if not exists public.findings (
  id                uuid primary key default gen_random_uuid(),
  case_id           uuid references public.cases(id) on delete cascade,
  check_type        text not null,
  label             text not null,
  ai_answer         text,
  source_chunk_ids  uuid[],
  source_excerpts   jsonb,
  confidence        double precision,
  hil_status        text,
  edited_answer     text,
  reviewed_by       text,
  reviewed_at       timestamptz,
  priority_flag     boolean default false,
  run_id            uuid,
  created_at        timestamptz default now()
);

-- ----------------------------------------------------------------------------
-- disposition_memos — generated memo drafts + attorney approval state.
-- ----------------------------------------------------------------------------
create table if not exists public.disposition_memos (
  id                 uuid primary key default gen_random_uuid(),
  case_id            uuid references public.cases(id) on delete cascade,
  draft_content      text,
  recommended_path   text,
  priority_findings  jsonb,
  attorney_approved  boolean default false,
  attorney_notes     text,
  approved_at        timestamptz,
  approved_by        text,
  version            integer default 1,
  created_at         timestamptz default now()
);

-- ----------------------------------------------------------------------------
-- audit_log — append-only log. Intentionally NO FKs on case_id/finding_id/
-- document_id so records survive the CASCADE deletes on those rows (audit
-- trail outlives the referenced entity). If you want integrity, add a soft
-- FK in a follow-up migration.
-- ----------------------------------------------------------------------------
create table if not exists public.audit_log (
  id          uuid primary key default gen_random_uuid(),
  case_id     uuid,
  finding_id  uuid,
  document_id uuid,
  action      text not null,
  actor       text not null,
  actor_name  text,
  note        text,
  metadata    jsonb,
  created_at  timestamptz default now()
);

-- ============================================================================
-- 3. Functions
-- ============================================================================
-- Declared AFTER tables because match_chunks is a SQL-language function whose
-- body references the `chunks` table and is validated at CREATE time. See
-- header note under ORDER OF OPERATIONS.

-- update_updated_at: BEFORE UPDATE trigger fn that stamps NEW.updated_at.
-- Only attached to `cases` in prod today (see Triggers section below).
CREATE OR REPLACE FUNCTION public.update_updated_at()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
$function$;

-- match_chunks: the retrieval RPC used by /cases/{id}/analyze. Returns the
-- top-N most-similar chunks, preferring case-specific chunks (case_id =
-- target) and falling back to global knowledge-base chunks (case_id IS NULL,
-- used for NRS statutes, bench book content, template motions, etc.).
CREATE OR REPLACE FUNCTION public.match_chunks(query_embedding vector, target_case_id uuid, match_count integer DEFAULT 8, similarity_threshold double precision DEFAULT 0.3)
 RETURNS TABLE(id uuid, content text, similarity double precision, metadata jsonb, case_id uuid, document_id uuid)
 LANGUAGE sql
AS $function$
  -- First: case-specific chunks
  (
    SELECT
      c.id, c.content,
      1 - (c.embedding <=> query_embedding) AS similarity,
      c.metadata, c.case_id, c.document_id
    FROM chunks c
    WHERE c.case_id = target_case_id
      AND 1 - (c.embedding <=> query_embedding) > similarity_threshold
    ORDER BY c.embedding <=> query_embedding
    LIMIT match_count
  )
  UNION ALL
  -- Then: static knowledge base (NRS, rules, motions)
  (
    SELECT
      c.id, c.content,
      1 - (c.embedding <=> query_embedding) AS similarity,
      c.metadata, c.case_id, c.document_id
    FROM chunks c
    WHERE c.case_id IS NULL
      AND 1 - (c.embedding <=> query_embedding) > similarity_threshold
    ORDER BY c.embedding <=> query_embedding
    LIMIT match_count
  )
  ORDER BY similarity DESC
  LIMIT match_count;
$function$;

-- ============================================================================
-- 4. Non-PK indexes
-- ============================================================================

-- audit_log
create index if not exists audit_log_case_id_idx
  on public.audit_log using btree (case_id);
create index if not exists audit_log_created_at_idx
  on public.audit_log using btree (created_at desc);

-- chunks
create index if not exists chunks_case_id_idx
  on public.chunks using btree (case_id);
-- pgvector ivfflat index — requires the vector extension. lists=100 matches
-- prod; tuning this up requires a REINDEX (separate migration).
create index if not exists chunks_embedding_idx
  on public.chunks using ivfflat (embedding vector_cosine_ops) with (lists = 100);

-- findings
create index if not exists findings_case_id_idx
  on public.findings using btree (case_id);
create index if not exists findings_check_type_idx
  on public.findings using btree (check_type);

-- (cases_case_number_key is created as a UNIQUE CONSTRAINT inline above;
--  no separate CREATE INDEX statement needed — Postgres materializes one
--  automatically with the same name.)

-- ============================================================================
-- 5. Triggers
-- ============================================================================

-- Only one trigger in prod: cases_updated_at. `documents`, `chunks`, `findings`,
-- etc. do not have updated_at triggers (nor updated_at columns in most cases).
drop trigger if exists cases_updated_at on public.cases;
create trigger cases_updated_at
  before update on public.cases
  for each row execute function public.update_updated_at();

-- ============================================================================
-- 6. Views
-- ============================================================================

create or replace view public.confidence_by_check_type as
  select check_type,
    count(*) as total_findings,
    count(*) filter (where hil_status = 'confirmed'::text) as confirmed,
    count(*) filter (where hil_status = 'edited'::text) as edited,
    count(*) filter (where hil_status = 'rejected'::text) as rejected,
    count(*) filter (where hil_status is not null) as reviewed,
    round(
      (count(*) filter (where hil_status = 'confirmed'::text))::numeric
        / nullif(count(*) filter (where hil_status is not null), 0)::numeric
        * 100::numeric,
      1
    ) as confirmed_pct,
    round(
      (count(*) filter (where hil_status = any (array['confirmed'::text, 'edited'::text])))::numeric
        / nullif(count(*) filter (where hil_status is not null), 0)::numeric
        * 100::numeric,
      1
    ) as accuracy_pct
  from findings
  where hil_status is not null
  group by check_type
  order by count(*) desc;

-- ============================================================================
-- End baseline UP migration.
-- ============================================================================

-- ───────────────────────────────────────────────────────
-- 2026-04-23 PROD PROMOTION NOTE
-- This file was NOT executed against prod kapyskpusteokxuaquwo.
-- Per this file's own header and ADR-012, the migration was
-- recorded in supabase_migrations.schema_migrations directly,
-- AFTER migrations 2-8 were applied, so the tracking-row format
-- could be matched to what Supabase actually writes.
-- See session handoff for decision rationale.
-- ───────────────────────────────────────────────────────
