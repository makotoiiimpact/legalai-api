/*
 * LegalAI Behavioral Intelligence Layer - Schema Migration
 * Created: 2026-04-20
 * Status: DRAFT - DO NOT EXECUTE WITHOUT REVIEW
 * Review required by: Makoto
 * Affects: Existing tables (cases) will receive ALTER TABLE additions
 * Rollback plan: Include a corresponding DOWN migration file for reversing changes
 */

-- ============================================================================
-- LegalAI — Behavioral Intelligence Layer (UP migration)
--
-- Scope: adds the full entity graph behind the "Matchup Card" product — people
-- (judges, prosecutors, officers, attorneys, witnesses, experts), institutional
-- actors (courts, agencies, devices), event records (motions, hearings, plea
-- offers), precomputed intelligence aggregations, and the AI-assisted capture
-- pipeline (capture_events + extraction_candidates).
--
-- Architectural frame (see Notion: "Platform Architecture — Behavioral
-- Intelligence Layer", 2026-04-20): every table is tagged with a tier so the
-- UI can tell the attorney whether a pattern came from public records
-- (tier_0_public), AI extraction from firm documents (tier_1_ai_extracted),
-- direct paralegal/attorney capture (tier_2_manual), or cross-case graph
-- derivation (tier_3_graph_derived).
--
-- ───────────────────────────────────────────────────────────────────────────
-- DESIGN DECISIONS (read before editing this file)
-- ───────────────────────────────────────────────────────────────────────────
--
-- 1. Soft-deletes on sensitive entities (deleted_at), hard-deletes on events.
--
--    Sensitive *entity* tables — judges, prosecutors, officers, attorneys,
--    witnesses, experts, agencies, devices — use `deleted_at timestamptz`.
--    Why: entity merges and retractions are reversible by necessity. An
--    officer wrongly merged, a witness incorrectly profiled, or a judge's
--    firm-private annotation that needs to be un-deleted for audit all
--    require a recovery path. Also: officer intelligence is the highest
--    sensitivity surface (see Notion §Compliance); we must preserve an audit
--    trail of when a profile was disabled and by whom.
--
--    Event records (motions, hearings, plea_offers) use hard-delete. They are
--    append-only in normal flow; a row only gets removed if it was entered in
--    error, and in that case we want it gone from aggregations cleanly.
--
--    Aggregation tables (judge_motion_stats, prosecutor_patterns, etc.) use
--    hard-delete + regeneration — they're derived state, not facts of record.
--
-- 2. raw_docket_text is preserved on `motions` even after motion_type_id is
--    assigned.
--
--    Extraction is imperfect. The AI may tag a motion as "motion_to_suppress_
--    warrantless_search" when a closer read shows it's actually "motion_to_
--    suppress_breath_test_observation_period". We need the raw text around to
--    (a) support paralegal re-classification when needs_review=true, (b)
--    re-run extraction against the same corpus if we improve prompts or
--    taxonomies, and (c) give the "click through to source" UX that the
--    compliance framework requires ("every surfaced insight must be traceable
--    to its source").
--
-- 3. Confidence tier calculation is sample-size based and precomputed nightly.
--
--    confidence_tier ∈ {'confirmed', 'suggested', 'signal', 'inferred'}
--      - n ≥ 20  → 'confirmed'
--      - 5 ≤ n < 20 → 'suggested'
--      - n < 5   → 'signal'
--      - patterns derived from Tier 0 public data without direct firm
--        observation → 'inferred' regardless of n
--
--    We store the tier column on aggregation tables rather than computing it
--    in the query layer so that (a) the UI never surfaces a "confirmed"
--    pattern one second before the nightly job demotes it to "suggested" due
--    to a retracted case, and (b) queries stay fast. The nightly job lives in
--    scheduled functions (not in this migration).
--
-- 4. firm_id is nullable *on every firm-private table* pre-multi-tenant.
--
--    We're pre-multi-tenant today (Garrett is customer 1). Every firm-private
--    table carries `firm_id uuid` but allows NULL for now so we can backfill
--    from a single implicit firm. When the second customer signs, a follow-up
--    migration will:
--      - create a `firms` table
--      - backfill existing rows with Garrett's firm_id
--      - set firm_id NOT NULL
--      - tighten RLS policies from "authenticated users" to
--        `firm_id = auth.jwt() ->> 'firm_id'`
--    Every RLS policy in this migration is marked `-- TIGHTEN PRE-MULTI-TENANT`
--    at the points that need that revision.
--
-- 5. RLS is ON for every firm-private table, even pre-multi-tenant.
--
--    Habit over convenience: we want zero tables in this schema where RLS is
--    off by default. The current policies are permissive ("authenticated") but
--    the framework is in place and tightening is a policy change, not a
--    schema change.
--
-- ───────────────────────────────────────────────────────────────────────────
-- IDEMPOTENCY
-- ───────────────────────────────────────────────────────────────────────────
-- This migration uses `IF NOT EXISTS` everywhere it can so it can be re-run
-- against a partial apply. The DOWN migration is required to fully reverse.
-- ============================================================================

-- Extensions
create extension if not exists "pgcrypto";  -- gen_random_uuid()

-- ----------------------------------------------------------------------------
-- Shared: updated_at trigger function
-- ----------------------------------------------------------------------------
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- ============================================================================
-- 1. ALTER existing tables
-- ============================================================================

-- cases: add data_tier classification so the UI can distinguish what kind of
-- data backs the record (public baseline vs. AI-extracted vs. human-captured
-- vs. cross-case graph derivation).
alter table if exists public.cases
  add column if not exists data_tier text
    not null default 'tier_2_manual'
    check (data_tier in (
      'tier_0_public',
      'tier_1_ai_extracted',
      'tier_2_manual',
      'tier_3_graph_derived'
    ));

alter table if exists public.cases
  add column if not exists firm_id uuid;  -- nullable; tightened post-multi-tenant

create index if not exists idx_cases_data_tier on public.cases(data_tier);
create index if not exists idx_cases_firm_id on public.cases(firm_id);

-- ============================================================================
-- 2. Core entity tables
-- ============================================================================

-- ----------------------------------------------------------------------------
-- courts — the institutional context for every case.
--   Mostly Tier 0 from CourtListener (`/courts/`). Not firm-private; shared
--   across customers. No soft-delete — courts don't get "removed" by firms.
-- ----------------------------------------------------------------------------
create table if not exists public.courts (
  id uuid primary key default gen_random_uuid(),
  cl_court_id text unique,                         -- CourtListener id (e.g. 'nvd', 'nev')
  full_name text not null,
  short_name text,
  citation_string text,
  jurisdiction text,                               -- 'NV', 'F', 'FB', ... mirrors CL
  jurisdiction_level text check (jurisdiction_level in (
    'federal_trial', 'federal_appellate', 'state_supreme',
    'state_appellate', 'state_trial', 'municipal', 'specialty', 'other'
  )),
  parent_court_id uuid references public.courts(id) on delete set null,
  in_use boolean not null default true,
  data_source text not null default 'courtlistener',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_courts_cl_id on public.courts(cl_court_id);
create index if not exists idx_courts_jurisdiction on public.courts(jurisdiction);
drop trigger if exists trg_courts_updated_at on public.courts;
create trigger trg_courts_updated_at before update on public.courts
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- agencies — police departments, DA offices, forensic labs, other institutions.
--   Public baseline where available; firm-private for local/rural departments.
-- ----------------------------------------------------------------------------
create table if not exists public.agencies (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid,                                    -- null = shared public record
  name text not null,
  agency_type text check (agency_type in (
    'police_department', 'sheriff_office', 'state_patrol', 'federal_agency',
    'da_office', 'public_defender', 'forensic_lab', 'other'
  )),
  jurisdiction text,                               -- e.g. 'Clark County, NV'
  parent_agency_id uuid references public.agencies(id) on delete set null,
  data_source text not null default 'firm_entered',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_agencies_firm_id on public.agencies(firm_id);
create index if not exists idx_agencies_type on public.agencies(agency_type);
create index if not exists idx_agencies_name on public.agencies(name);
drop trigger if exists trg_agencies_updated_at on public.agencies;
create trigger trg_agencies_updated_at before update on public.agencies
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- devices — breathalyzer serials, radar units, bodycams. Intelligence-rich
--   because a single miscalibrated device can re-open suppression arguments
--   across multiple cases. Firm-private.
-- ----------------------------------------------------------------------------
create table if not exists public.devices (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid,
  device_type text not null check (device_type in (
    'intoxilyzer', 'other_breathalyzer', 'radar', 'lidar',
    'bodycam', 'dashcam', 'gps_tracker', 'blood_kit', 'other'
  )),
  make text,
  model text,
  serial_number text,
  agency_id uuid references public.agencies(id) on delete set null,
  last_calibration_date date,
  calibration_notes text,
  known_issues text,
  data_source text not null default 'firm_entered',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create unique index if not exists idx_devices_firm_serial
  on public.devices(firm_id, device_type, serial_number)
  where deleted_at is null and serial_number is not null;
create index if not exists idx_devices_agency_id on public.devices(agency_id);
drop trigger if exists trg_devices_updated_at on public.devices;
create trigger trg_devices_updated_at before update on public.devices
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- attorneys — firm's own + opposing defense counsel + co-defendant counsel +
--   plaintiff counsel (for civil cases later). Public baseline from bar
--   directories where available; firm-private annotations on top.
-- ----------------------------------------------------------------------------
create table if not exists public.attorneys (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid,                                    -- null = shared record
  full_name text not null,
  first_name text,
  last_name text,
  bar_number text,
  bar_state text,
  email text,
  phone text,
  firm_name text,                                  -- their firm if opposing
  specialties text[],
  cl_person_id bigint unique,                      -- CourtListener /people/{id}
  is_firm_member boolean not null default false,   -- true = attorney at our firm
  data_source text not null default 'firm_entered',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_attorneys_firm_id on public.attorneys(firm_id);
create index if not exists idx_attorneys_bar on public.attorneys(bar_state, bar_number);
create index if not exists idx_attorneys_cl_person on public.attorneys(cl_person_id);
create index if not exists idx_attorneys_name on public.attorneys(last_name, first_name);
drop trigger if exists trg_attorneys_updated_at on public.attorneys;
create trigger trg_attorneys_updated_at before update on public.attorneys
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- judges — public baseline from CourtListener + firm-private annotations.
--   Highest-value entity for matchup intelligence.
-- ----------------------------------------------------------------------------
create table if not exists public.judges (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid,                                    -- null = shared/public record
  full_name text not null,
  first_name text,
  middle_name text,
  last_name text,
  cl_person_id bigint unique,                      -- CourtListener person id
  court_id uuid references public.courts(id) on delete set null,
  department text,                                 -- e.g. "Dept. 12"
  bench_since date,
  bench_until date,
  political_affiliations text[],                   -- only as published
  aba_rating text,
  notes text,                                      -- firm-private annotations
  data_source text not null default 'courtlistener',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_judges_firm_id on public.judges(firm_id);
create index if not exists idx_judges_court_id on public.judges(court_id);
create index if not exists idx_judges_cl_person on public.judges(cl_person_id);
create index if not exists idx_judges_name on public.judges(last_name, first_name);
drop trigger if exists trg_judges_updated_at on public.judges;
create trigger trg_judges_updated_at before update on public.judges
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- prosecutors — no public DA directory for state trial courts. Almost entirely
--   firm-built via Tier 1/2 capture. Critical for plea offer patterns.
-- ----------------------------------------------------------------------------
create table if not exists public.prosecutors (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid,
  full_name text not null,
  first_name text,
  last_name text,
  agency_id uuid references public.agencies(id) on delete set null,  -- DA office
  title text,                                      -- ADA, Deputy DA, Chief Deputy, etc.
  unit text,                                       -- DUI Unit, Major Crimes, etc.
  bar_number text,
  bar_state text,
  active boolean not null default true,
  notes text,                                      -- firm-private
  data_source text not null default 'firm_entered',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_prosecutors_firm_id on public.prosecutors(firm_id);
create index if not exists idx_prosecutors_agency_id on public.prosecutors(agency_id);
create index if not exists idx_prosecutors_name on public.prosecutors(last_name, first_name);
drop trigger if exists trg_prosecutors_updated_at on public.prosecutors;
create trigger trg_prosecutors_updated_at before update on public.prosecutors
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- officers — MOST SENSITIVE table. Per compliance framework, data here is
--   labeled as "Public Record Profile" in the UI, NOT as a "credibility
--   rating". Only aggregate from publicly filed material. Per-firm disable
--   toggle lives in firm_settings (not in this migration).
-- ----------------------------------------------------------------------------
create table if not exists public.officers (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid,
  full_name text not null,
  first_name text,
  last_name text,
  badge_number text,
  agency_id uuid references public.agencies(id) on delete set null,
  unit text,                                       -- DUI task force, traffic, etc.
  rank text,
  years_on_force integer,
  notes text,                                      -- strictly from public filings
  public_record_only boolean not null default true, -- must be true per compliance rules
  data_source text not null default 'firm_entered',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_officers_firm_id on public.officers(firm_id);
create index if not exists idx_officers_agency_id on public.officers(agency_id);
create index if not exists idx_officers_badge on public.officers(agency_id, badge_number);
create index if not exists idx_officers_name on public.officers(last_name, first_name);
drop trigger if exists trg_officers_updated_at on public.officers;
create trigger trg_officers_updated_at before update on public.officers
  for each row execute function public.set_updated_at();

-- Compliance guardrail: officers table may only contain public-record-derived data.
-- NOT VALID: enforces on all future inserts; existing rows are grandfathered if any.
-- Product layer must also enforce this rule; DB-level is belt-and-suspenders.
alter table public.officers add constraint officers_public_record_only_check
  check (public_record_only = true) not valid;

-- ----------------------------------------------------------------------------
-- witnesses — civilian witnesses. Firm-private. Captures prior testimony
--   inconsistencies where publicly traceable.
-- ----------------------------------------------------------------------------
create table if not exists public.witnesses (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid,
  full_name text not null,
  first_name text,
  last_name text,
  witness_type text check (witness_type in (
    'civilian', 'lay_percipient', 'character', 'rebuttal', 'other'
  )),
  known_relationships text,                        -- freeform per compliance caveats
  notes text,
  data_source text not null default 'firm_entered',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_witnesses_firm_id on public.witnesses(firm_id);
create index if not exists idx_witnesses_name on public.witnesses(last_name, first_name);
drop trigger if exists trg_witnesses_updated_at on public.witnesses;
create trigger trg_witnesses_updated_at before update on public.witnesses
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- experts — expert witnesses. Firm-private base record + shared published
--   opinions/Daubert history where applicable.
-- ----------------------------------------------------------------------------
create table if not exists public.experts (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid,
  full_name text not null,
  first_name text,
  last_name text,
  specialty text,
  credentials text,
  typical_side text check (typical_side in ('plaintiff', 'defense', 'either', 'unknown')),
  daubert_exclusion_history text,
  published_opinions text,
  cl_person_id bigint,                             -- if CL has them
  notes text,
  data_source text not null default 'firm_entered',
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_experts_firm_id on public.experts(firm_id);
create index if not exists idx_experts_specialty on public.experts(specialty);
create index if not exists idx_experts_name on public.experts(last_name, first_name);
drop trigger if exists trg_experts_updated_at on public.experts;
create trigger trg_experts_updated_at before update on public.experts
  for each row execute function public.set_updated_at();

-- ============================================================================
-- 3. Relationship (join) tables — connect cases to people/institutions
-- ============================================================================

-- Every case has a defense team (ours), a prosecution team, possibly
-- co-defendant counsel, and possibly plaintiff counsel. A single attorney may
-- appear in multiple cases in different roles.
create table if not exists public.case_attorneys (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references public.cases(id) on delete cascade,
  attorney_id uuid not null references public.attorneys(id) on delete restrict,
  role text not null check (role in (
    'defense_lead', 'defense_associate', 'defense_of_counsel',
    'prosecution_lead', 'prosecution_second_chair',
    'co_defendant_counsel', 'plaintiff_lead', 'plaintiff_associate',
    'other'
  )),
  firm_id uuid,
  start_date date,
  end_date date,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (case_id, attorney_id, role)
);
create index if not exists idx_case_attorneys_case_id on public.case_attorneys(case_id);
create index if not exists idx_case_attorneys_attorney_id on public.case_attorneys(attorney_id);
create index if not exists idx_case_attorneys_role on public.case_attorneys(role);
create index if not exists idx_case_attorneys_firm_id on public.case_attorneys(firm_id);
drop trigger if exists trg_case_attorneys_updated_at on public.case_attorneys;
create trigger trg_case_attorneys_updated_at before update on public.case_attorneys
  for each row execute function public.set_updated_at();

create table if not exists public.case_officers (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references public.cases(id) on delete cascade,
  officer_id uuid not null references public.officers(id) on delete restrict,
  role text not null check (role in (
    'arresting', 'investigating', 'transporting', 'booking',
    'field_test', 'breath_operator', 'witness_only', 'other'
  )),
  firm_id uuid,
  bodycam_activated boolean,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (case_id, officer_id, role)
);
create index if not exists idx_case_officers_case_id on public.case_officers(case_id);
create index if not exists idx_case_officers_officer_id on public.case_officers(officer_id);
create index if not exists idx_case_officers_role on public.case_officers(role);
drop trigger if exists trg_case_officers_updated_at on public.case_officers;
create trigger trg_case_officers_updated_at before update on public.case_officers
  for each row execute function public.set_updated_at();

create table if not exists public.case_witnesses (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references public.cases(id) on delete cascade,
  witness_id uuid not null references public.witnesses(id) on delete restrict,
  role text not null check (role in (
    'prosecution_witness', 'defense_witness', 'rebuttal',
    'character', 'neutral', 'other'
  )),
  firm_id uuid,
  testified boolean,
  testimony_date date,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (case_id, witness_id, role)
);
create index if not exists idx_case_witnesses_case_id on public.case_witnesses(case_id);
create index if not exists idx_case_witnesses_witness_id on public.case_witnesses(witness_id);
drop trigger if exists trg_case_witnesses_updated_at on public.case_witnesses;
create trigger trg_case_witnesses_updated_at before update on public.case_witnesses
  for each row execute function public.set_updated_at();

create table if not exists public.case_devices (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references public.cases(id) on delete cascade,
  device_id uuid not null references public.devices(id) on delete restrict,
  role text check (role in (
    'evidence_breathalyzer', 'evidence_radar', 'evidence_bodycam',
    'evidence_dashcam', 'evidence_blood_kit', 'evidence_other'
  )),
  firm_id uuid,
  used_at timestamptz,
  result_summary text,
  chain_of_custody_concern boolean not null default false,
  notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_case_devices_case_id on public.case_devices(case_id);
create index if not exists idx_case_devices_device_id on public.case_devices(device_id);
drop trigger if exists trg_case_devices_updated_at on public.case_devices;
create trigger trg_case_devices_updated_at before update on public.case_devices
  for each row execute function public.set_updated_at();

-- ============================================================================
-- 4. Event tables
-- ============================================================================

-- ----------------------------------------------------------------------------
-- motion_types — evolving taxonomy. Seeded motions come from Nevada criminal
-- procedure; real-world usage updates `real_usage_count` via a nightly job.
-- If a seeded motion sees zero usage in 12 months, UI demotes it. If an
-- unseeded motion type emerges from extraction, it's created on the fly and
-- tagged is_seeded=false.
-- ----------------------------------------------------------------------------
create table if not exists public.motion_types (
  id uuid primary key default gen_random_uuid(),
  slug text unique not null,
  display_name text not null,
  category text not null check (category in (
    'suppress', 'dismiss', 'limine', 'continuance', 'sever', 'venue',
    'new_trial', 'reconsideration', 'stay', 'bail', 'discovery',
    'compel', 'quash', 'other'
  )),
  description text,
  is_seeded boolean not null default false,        -- true = preloaded NV criminal seed
  seeded_source text,                              -- e.g. 'NRS 174', 'NRS 179'
  real_usage_count integer not null default 0,    -- updated by nightly job
  deprecation_status text not null default 'active'
    check (deprecation_status in ('active', 'deprecated', 'merged')),
  merged_into_slug text,                           -- if deprecation_status='merged'
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_motion_types_category on public.motion_types(category);
create index if not exists idx_motion_types_deprecation on public.motion_types(deprecation_status);
drop trigger if exists trg_motion_types_updated_at on public.motion_types;
create trigger trg_motion_types_updated_at before update on public.motion_types
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- motions — every motion filed, ruled, or anticipated. Raw text is preserved
--   alongside the normalized motion_type_id (see design decision #2).
-- ----------------------------------------------------------------------------
create table if not exists public.motions (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references public.cases(id) on delete cascade,
  firm_id uuid,
  motion_type_id uuid references public.motion_types(id) on delete set null,
  raw_docket_text text,                            -- source of truth, always preserved
  filed_by_attorney_id uuid references public.attorneys(id) on delete set null,
  filed_at timestamptz,
  hearing_id uuid,                                 -- FK added after hearings created
  judge_id uuid references public.judges(id) on delete set null,
  ruling text check (ruling in (
    'granted', 'granted_in_part', 'denied', 'denied_without_prejudice',
    'withdrawn', 'mooted', 'pending', 'continued', 'other'
  )),
  ruling_date date,
  ruling_notes text,                               -- firm-private
  confidence text check (confidence in ('confirmed', 'suggested', 'signal', 'inferred')),
  needs_review boolean not null default false,    -- true = AI extraction wants human eye
  data_source text not null default 'firm_entered',
  data_tier text not null default 'tier_2_manual' check (data_tier in (
    'tier_0_public', 'tier_1_ai_extracted', 'tier_2_manual', 'tier_3_graph_derived'
  )),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_motions_case_id on public.motions(case_id);
create index if not exists idx_motions_type_id on public.motions(motion_type_id);
create index if not exists idx_motions_judge_id on public.motions(judge_id);
create index if not exists idx_motions_ruling on public.motions(ruling);
create index if not exists idx_motions_firm_id on public.motions(firm_id);
create index if not exists idx_motions_needs_review on public.motions(needs_review) where needs_review = true;
create index if not exists idx_motions_judge_type_ruling
  on public.motions(judge_id, motion_type_id, ruling);
drop trigger if exists trg_motions_updated_at on public.motions;
create trigger trg_motions_updated_at before update on public.motions
  for each row execute function public.set_updated_at();

-- ----------------------------------------------------------------------------
-- hearings — every scheduled appearance. Calendar sync target.
-- ----------------------------------------------------------------------------
create table if not exists public.hearings (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references public.cases(id) on delete cascade,
  firm_id uuid,
  hearing_type text check (hearing_type in (
    'arraignment', 'preliminary', 'status', 'motion_hearing', 'trial',
    'sentencing', 'revocation', 'appeal_oral_arg', 'other'
  )),
  court_id uuid references public.courts(id) on delete set null,
  judge_id uuid references public.judges(id) on delete set null,
  scheduled_at timestamptz,
  actual_at timestamptz,
  location text,
  outcome text,
  notes text,
  data_source text not null default 'firm_entered',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_hearings_case_id on public.hearings(case_id);
create index if not exists idx_hearings_judge_id on public.hearings(judge_id);
create index if not exists idx_hearings_court_id on public.hearings(court_id);
create index if not exists idx_hearings_scheduled_at on public.hearings(scheduled_at);
drop trigger if exists trg_hearings_updated_at on public.hearings;
create trigger trg_hearings_updated_at before update on public.hearings
  for each row execute function public.set_updated_at();

-- Back-fill the motion.hearing_id FK now that hearings exists.
do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'motions_hearing_id_fkey'
  ) then
    alter table public.motions
      add constraint motions_hearing_id_fkey
      foreign key (hearing_id) references public.hearings(id) on delete set null;
  end if;
end$$;
create index if not exists idx_motions_hearing_id on public.motions(hearing_id);

-- ----------------------------------------------------------------------------
-- plea_offers — atomic unit for prosecutor behavior intelligence.
-- ----------------------------------------------------------------------------
create table if not exists public.plea_offers (
  id uuid primary key default gen_random_uuid(),
  case_id uuid not null references public.cases(id) on delete cascade,
  firm_id uuid,
  prosecutor_id uuid references public.prosecutors(id) on delete set null,
  offered_at timestamptz,
  original_charges text,
  offered_charges text,
  terms jsonb,                                     -- {jail_days, fines, probation_months, etc.}
  response text check (response in (
    'accepted', 'rejected', 'counter_offered', 'pending', 'withdrawn'
  )),
  counter_offer_id uuid references public.plea_offers(id) on delete set null,
  rounds_to_final integer,                         -- 1 = opening offer, etc.
  notes text,                                      -- firm-private
  data_source text not null default 'firm_entered',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_plea_offers_case_id on public.plea_offers(case_id);
create index if not exists idx_plea_offers_prosecutor_id on public.plea_offers(prosecutor_id);
create index if not exists idx_plea_offers_offered_at on public.plea_offers(offered_at);
drop trigger if exists trg_plea_offers_updated_at on public.plea_offers;
create trigger trg_plea_offers_updated_at before update on public.plea_offers
  for each row execute function public.set_updated_at();

-- ============================================================================
-- 5. Intelligence aggregation tables (derived; regenerated nightly)
-- ============================================================================

-- judge_motion_stats: one row per (judge, motion_type).
create table if not exists public.judge_motion_stats (
  id uuid primary key default gen_random_uuid(),
  judge_id uuid not null references public.judges(id) on delete cascade,
  motion_type_id uuid not null references public.motion_types(id) on delete cascade,
  firm_id uuid,                                    -- null = cross-firm public aggregation
  total_count integer not null default 0,
  granted_count integer not null default 0,
  granted_in_part_count integer not null default 0,
  denied_count integer not null default 0,
  other_count integer not null default 0,
  grant_rate numeric(5,4),                         -- granted / (granted + denied)
  confidence_tier text not null check (confidence_tier in (
    'confirmed', 'suggested', 'signal', 'inferred'
  )),
  last_recomputed_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (judge_id, motion_type_id, firm_id)
);
create index if not exists idx_jms_judge on public.judge_motion_stats(judge_id);
create index if not exists idx_jms_motion_type on public.judge_motion_stats(motion_type_id);
create index if not exists idx_jms_firm_id on public.judge_motion_stats(firm_id);
create index if not exists idx_jms_confidence on public.judge_motion_stats(confidence_tier);
drop trigger if exists trg_jms_updated_at on public.judge_motion_stats;
create trigger trg_jms_updated_at before update on public.judge_motion_stats
  for each row execute function public.set_updated_at();

-- prosecutor_patterns: pattern_type discriminator on a jsonb payload keeps the
-- table general across "opening offer BAC drop", "plea rate by charge", etc.
create table if not exists public.prosecutor_patterns (
  id uuid primary key default gen_random_uuid(),
  prosecutor_id uuid not null references public.prosecutors(id) on delete cascade,
  firm_id uuid,
  pattern_type text not null check (pattern_type in (
    'opening_plea_offer', 'closing_plea_offer', 'negotiation_movement',
    'trial_vs_plea_rate', 'charge_reduction_rate', 'bail_position',
    'brady_compliance', 'escalation_trigger', 'other'
  )),
  pattern_value jsonb not null,
  sample_size integer not null default 0,
  confidence_tier text not null check (confidence_tier in (
    'confirmed', 'suggested', 'signal', 'inferred'
  )),
  last_recomputed_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (prosecutor_id, pattern_type, firm_id)
);
create index if not exists idx_pros_patterns_prosecutor on public.prosecutor_patterns(prosecutor_id);
create index if not exists idx_pros_patterns_type on public.prosecutor_patterns(pattern_type);
create index if not exists idx_pros_patterns_firm_id on public.prosecutor_patterns(firm_id);
drop trigger if exists trg_pros_patterns_updated_at on public.prosecutor_patterns;
create trigger trg_pros_patterns_updated_at before update on public.prosecutor_patterns
  for each row execute function public.set_updated_at();

-- matchup_history: head-to-head record between two attorneys (typically
-- one of ours vs. one prosecution attorney). Directional: attorney_a=our side.
create table if not exists public.matchup_history (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid,
  attorney_a_id uuid not null references public.attorneys(id) on delete cascade,
  attorney_b_id uuid not null references public.attorneys(id) on delete cascade,
  attorney_a_role text not null,                   -- e.g. 'defense_lead'
  attorney_b_role text not null,                   -- e.g. 'prosecution_lead'
  total_cases integer not null default 0,
  a_favorable_count integer not null default 0,
  a_unfavorable_count integer not null default 0,
  neutral_count integer not null default 0,
  outcomes jsonb,                                  -- detailed breakdown
  confidence_tier text not null check (confidence_tier in (
    'confirmed', 'suggested', 'signal', 'inferred'
  )),
  last_recomputed_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (attorney_a_id, attorney_b_id, firm_id),
  check (attorney_a_id <> attorney_b_id)
);
create index if not exists idx_matchup_a on public.matchup_history(attorney_a_id);
create index if not exists idx_matchup_b on public.matchup_history(attorney_b_id);
create index if not exists idx_matchup_firm on public.matchup_history(firm_id);
drop trigger if exists trg_matchup_updated_at on public.matchup_history;
create trigger trg_matchup_updated_at before update on public.matchup_history
  for each row execute function public.set_updated_at();

-- officer_patterns: SENSITIVE. Per compliance framework, pattern_value must
-- only cite publicly filed material (suppression grants, disciplinary filings).
create table if not exists public.officer_patterns (
  id uuid primary key default gen_random_uuid(),
  officer_id uuid not null references public.officers(id) on delete cascade,
  firm_id uuid,
  pattern_type text not null check (pattern_type in (
    'suppression_grants_against', 'hgn_procedural_errors',
    'observation_period_shortcut', 'bodycam_non_activation',
    'testimony_impeached', 'arrest_volume_mix', 'other'
  )),
  pattern_value jsonb not null,
  sample_size integer not null default 0,
  confidence_tier text not null check (confidence_tier in (
    'confirmed', 'suggested', 'signal', 'inferred'
  )),
  public_record_only boolean not null default true,
  last_recomputed_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (officer_id, pattern_type, firm_id)
);
create index if not exists idx_officer_patterns_officer on public.officer_patterns(officer_id);
create index if not exists idx_officer_patterns_type on public.officer_patterns(pattern_type);
create index if not exists idx_officer_patterns_firm on public.officer_patterns(firm_id);
drop trigger if exists trg_officer_patterns_updated_at on public.officer_patterns;
create trigger trg_officer_patterns_updated_at before update on public.officer_patterns
  for each row execute function public.set_updated_at();

-- entity_observations: firm-private freeform notes attached to any entity.
-- Uses a discriminator column (entity_type) rather than 8 separate join tables
-- because observations are the "long tail" input — attorneys just type things.
create table if not exists public.entity_observations (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid,
  entity_type text not null check (entity_type in (
    'judge', 'prosecutor', 'attorney', 'officer', 'witness', 'expert',
    'device', 'court', 'agency'
  )),
  entity_id uuid not null,                         -- polymorphic; integrity enforced in app
  observation text not null,
  tags text[],
  created_by uuid,                                 -- user_id FK when auth layer ready
  supersedes_id uuid references public.entity_observations(id) on delete set null,
  confidence text check (confidence in ('confirmed', 'suggested', 'signal', 'inferred')),
  deleted_at timestamptz,                          -- soft-delete; NULL means active. Nullable by design.
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_entity_obs_entity on public.entity_observations(entity_type, entity_id);
create index if not exists idx_entity_obs_firm on public.entity_observations(firm_id);
create index if not exists idx_entity_obs_tags on public.entity_observations using gin(tags);
create index if not exists idx_entity_observations_active
  on public.entity_observations (entity_type, entity_id)
  where deleted_at is null;
drop trigger if exists trg_entity_obs_updated_at on public.entity_observations;
create trigger trg_entity_obs_updated_at before update on public.entity_observations
  for each row execute function public.set_updated_at();

-- ============================================================================
-- 6. Capture pipeline (AI-assisted ingestion)
-- ============================================================================

-- capture_events: one row per raw capture attempt (doc upload, voice memo,
-- email forward, manual entry, calendar sync). Has a status column so the
-- paralegal review UI can filter for awaiting_review.
create table if not exists public.capture_events (
  id uuid primary key default gen_random_uuid(),
  firm_id uuid,
  case_id uuid references public.cases(id) on delete set null,
  source text not null check (source in (
    'document_upload', 'voice_memo', 'email_forward', 'manual', 'calendar_sync'
  )),
  source_metadata jsonb,                           -- original filename, email sender, phone, etc.
  raw_payload text,                                -- transcription, email body, freeform text
  raw_payload_url text,                            -- if too big for a column
  status text not null default 'received' check (status in (
    'received', 'extracting', 'awaiting_review', 'confirmed', 'rejected', 'error'
  )),
  created_by uuid,
  processing_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_capture_events_case on public.capture_events(case_id);
create index if not exists idx_capture_events_firm on public.capture_events(firm_id);
create index if not exists idx_capture_events_status on public.capture_events(status);
create index if not exists idx_capture_events_source on public.capture_events(source);
drop trigger if exists trg_capture_events_updated_at on public.capture_events;
create trigger trg_capture_events_updated_at before update on public.capture_events
  for each row execute function public.set_updated_at();

-- extraction_candidates: AI proposals pending human confirmation. Polymorphic
-- target entity like entity_observations.
create table if not exists public.extraction_candidates (
  id uuid primary key default gen_random_uuid(),
  capture_event_id uuid not null references public.capture_events(id) on delete cascade,
  firm_id uuid,
  candidate_type text not null check (candidate_type in (
    'attorney', 'judge', 'prosecutor', 'officer', 'witness', 'expert',
    'device', 'motion', 'hearing', 'plea_offer', 'observation', 'other'
  )),
  proposed_payload jsonb not null,                 -- the thing AI wants to create/update
  proposed_entity_match_id uuid,                   -- existing entity we think matches
  confidence_score numeric(5,4),                   -- 0..1 from the extractor
  status text not null default 'pending' check (status in (
    'pending', 'confirmed', 'rejected', 'merged', 'superseded'
  )),
  confirmed_entity_type text,
  confirmed_entity_id uuid,
  confirmed_by uuid,
  confirmed_at timestamptz,
  reviewer_notes text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_ec_capture on public.extraction_candidates(capture_event_id);
create index if not exists idx_ec_status on public.extraction_candidates(status);
create index if not exists idx_ec_type on public.extraction_candidates(candidate_type);
create index if not exists idx_ec_firm on public.extraction_candidates(firm_id);
drop trigger if exists trg_ec_updated_at on public.extraction_candidates;
create trigger trg_ec_updated_at before update on public.extraction_candidates
  for each row execute function public.set_updated_at();

-- ============================================================================
-- 7. Row-Level Security
-- ============================================================================
-- All firm-private tables: RLS ON, permissive policy for authenticated users
-- until multi-tenant auth ships. Every policy below is marked
-- `-- TIGHTEN PRE-MULTI-TENANT` at the exact site where it needs revision.
--
-- Tables that DO NOT need RLS:
--   courts, motion_types — cross-firm shared reference data. These are SELECT
--     public (anon ok), INSERT/UPDATE admin-only (enforced at API layer).
-- ============================================================================

alter table public.agencies enable row level security;
alter table public.devices enable row level security;
alter table public.attorneys enable row level security;
alter table public.judges enable row level security;
alter table public.prosecutors enable row level security;
alter table public.officers enable row level security;
alter table public.witnesses enable row level security;
alter table public.experts enable row level security;
alter table public.case_attorneys enable row level security;
alter table public.case_officers enable row level security;
alter table public.case_witnesses enable row level security;
alter table public.case_devices enable row level security;
alter table public.motions enable row level security;
alter table public.hearings enable row level security;
alter table public.plea_offers enable row level security;
alter table public.judge_motion_stats enable row level security;
alter table public.prosecutor_patterns enable row level security;
alter table public.matchup_history enable row level security;
alter table public.officer_patterns enable row level security;
alter table public.entity_observations enable row level security;
alter table public.capture_events enable row level security;
alter table public.extraction_candidates enable row level security;

-- TODO (multi-tenant migration): tighten firm_id IS NULL INSERT permission to
-- service_role only. Current policy allows any authenticated user to create
-- shared records, which is fine for single-firm launch but MUST be restricted
-- before onboarding second customer. Reference: MAKOTO_REVIEW.md Q7, scheduled
-- for the multi-tenant migration that also introduces the firms table.
-- TIGHTEN PRE-MULTI-TENANT:
--   replace `using (true)` below with
--     `using (firm_id = (auth.jwt() ->> 'firm_id')::uuid)`
--   and the same predicate in `with check`.
do $$
declare
  t text;
  policy_name text;
  firm_tables text[] := array[
    'agencies','devices','attorneys','judges','prosecutors','officers',
    'witnesses','experts','case_attorneys','case_officers','case_witnesses',
    'case_devices','motions','hearings','plea_offers','judge_motion_stats',
    'prosecutor_patterns','matchup_history','officer_patterns',
    'entity_observations','capture_events','extraction_candidates'
  ];
begin
  foreach t in array firm_tables loop
    policy_name := t || '_auth_all';
    execute format(
      'drop policy if exists %I on public.%I;',
      policy_name, t
    );
    execute format(
      'create policy %I on public.%I for all to authenticated using (true) with check (true);',
      policy_name, t
    );
  end loop;
end$$;

-- ============================================================================
-- 8. Seed motion_types (Nevada criminal procedure, is_seeded=true)
-- ============================================================================

insert into public.motion_types (slug, display_name, category, description, is_seeded, seeded_source) values
  ('motion_to_suppress_warrantless_search', 'Motion to Suppress — Warrantless Search', 'suppress', 'Fourth Amendment challenge to evidence obtained without warrant', true, 'NRS 179; US Const. Amend. IV'),
  ('motion_to_suppress_invalid_warrant', 'Motion to Suppress — Invalid Warrant', 'suppress', 'Challenge to warrant validity (probable cause, particularity, scope)', true, 'NRS 179.045'),
  ('motion_to_suppress_breath_test_observation', 'Motion to Suppress — Breath Test (Observation Period)', 'suppress', '20-minute observation rule challenge for breath tests', true, 'NRS 484C.150; NAC 484C'),
  ('motion_to_suppress_blood_draw', 'Motion to Suppress — Blood Draw', 'suppress', 'Challenge to blood draw procedure or warrant for DUI blood evidence', true, 'NRS 484C; Missouri v. McNeely'),
  ('motion_to_suppress_statements_miranda', 'Motion to Suppress — Statements (Miranda)', 'suppress', 'Miranda/Fifth Amendment challenge to custodial statements', true, 'Miranda v. Arizona'),
  ('motion_to_suppress_identification', 'Motion to Suppress — Identification', 'suppress', 'Challenge to suggestive lineup or show-up identification', true, 'Neil v. Biggers'),
  ('motion_to_suppress_chain_of_custody', 'Motion to Suppress — Chain of Custody', 'suppress', 'Challenge to evidence authenticity from chain of custody break', true, 'NRS 52'),
  ('motion_to_dismiss_speedy_trial', 'Motion to Dismiss — Speedy Trial', 'dismiss', 'Sixth Amendment / NRS speedy trial challenge', true, 'NRS 178.556'),
  ('motion_to_dismiss_insufficient_evidence', 'Motion to Dismiss — Insufficient Evidence', 'dismiss', 'Dismissal for insufficient evidence to proceed to trial', true, 'NRS 174.085'),
  ('motion_to_dismiss_double_jeopardy', 'Motion to Dismiss — Double Jeopardy', 'dismiss', 'Fifth Amendment double jeopardy challenge', true, 'US Const. Amend. V'),
  ('motion_to_dismiss_prosecutorial_misconduct', 'Motion to Dismiss — Prosecutorial Misconduct', 'dismiss', 'Dismissal for Brady/Giglio violations or vindictive prosecution', true, 'Brady v. Maryland'),
  ('motion_to_dismiss_defective_complaint', 'Motion to Dismiss — Defective Complaint', 'dismiss', 'Facial challenge to complaint/information sufficiency', true, 'NRS 173'),
  ('motion_in_limine_prior_convictions', 'Motion in Limine — Prior Convictions', 'limine', 'Exclude prior convictions under NRS 50.095/404(b)', true, 'NRS 50.095'),
  ('motion_in_limine_other_acts_404b', 'Motion in Limine — Other Acts (404(b))', 'limine', 'Exclude other-acts evidence under NRS 48.045(2)', true, 'NRS 48.045'),
  ('motion_in_limine_expert_daubert', 'Motion in Limine — Expert Testimony (Daubert)', 'limine', 'Exclude expert testimony under Daubert/Higgs standard', true, 'Higgs v. State'),
  ('motion_for_continuance', 'Motion for Continuance', 'continuance', 'Request to postpone hearing or trial', true, 'NRS 174.515'),
  ('motion_to_sever_defendants', 'Motion to Sever Defendants', 'sever', 'Separate trials for co-defendants', true, 'NRS 174.165'),
  ('motion_to_sever_counts', 'Motion to Sever Counts', 'sever', 'Separate trials for joined counts', true, 'NRS 174.165'),
  ('motion_for_change_of_venue', 'Motion for Change of Venue', 'venue', 'Transfer trial to different county for pretrial publicity', true, 'NRS 174.455'),
  ('motion_for_new_trial', 'Motion for New Trial', 'new_trial', 'Post-verdict motion for new trial', true, 'NRS 176.515'),
  ('motion_for_reconsideration', 'Motion for Reconsideration', 'reconsideration', 'Request court reconsider prior ruling', true, 'EDCR 2.24 / local rules'),
  ('motion_to_stay_pending_appeal', 'Motion to Stay Pending Appeal', 'stay', 'Stay execution of judgment while appeal pending', true, 'NRAP 8'),
  ('motion_to_reduce_bail', 'Motion to Reduce Bail', 'bail', 'Request bail reduction or release on own recognizance', true, 'NRS 178.484'),
  ('motion_for_discovery_brady', 'Motion for Discovery / Brady', 'discovery', 'Compel discovery under Brady/Giglio', true, 'NRS 174.235; Brady v. Maryland'),
  ('motion_to_compel', 'Motion to Compel', 'compel', 'Compel compliance with discovery, subpoena, or prior order', true, 'NRS 174'),
  ('motion_to_quash_subpoena', 'Motion to Quash Subpoena', 'quash', 'Quash or modify a subpoena duces tecum or ad testificandum', true, 'NRS 174.335')
on conflict (slug) do nothing;

-- ============================================================================
-- End UP migration.
-- ============================================================================
