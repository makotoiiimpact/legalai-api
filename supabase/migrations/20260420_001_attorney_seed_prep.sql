-- ============================================================================
-- Migration: 20260420_001_attorney_seed_prep
-- Purpose: Schema prep for Tier 0 federal seed
-- References: ADR-017 (attribution_confidence), ADR-018 (firm member flag),
--             ADR-021 (ICP thesis), Decision memo 2026-04-20 (external_ids jsonb)
-- Applies to: legalai-dev (cfiaxrvtafszmgraftbk) first; prod promotion later
--
-- Note on Step 3 (attorneys columns): is_firm_member, bar_number, bar_state
-- were added inline to the CREATE TABLE in yesterday's behavioral intelligence
-- layer migration rather than in a separate ADR-018 prep migration. This file
-- uses ADD COLUMN IF NOT EXISTS so the intent is captured in a dedicated
-- migration (matching the ADR-018 record) while still applying cleanly against
-- environments where the BIL migration already created them.
-- ============================================================================

BEGIN;

-- 1. attribution_confidence enum (ADR-017, expanded per 2026-04-20 decision)
CREATE TYPE attribution_confidence AS ENUM (
  'unverified',
  'firm_level_only',
  'inferred',
  'paralegal_verified',
  'attorney_verified',
  'client_confirmed'
);

-- 2. case_attorneys.attribution_confidence column
ALTER TABLE case_attorneys
  ADD COLUMN attribution_confidence attribution_confidence
  NOT NULL DEFAULT 'unverified';

COMMENT ON COLUMN case_attorneys.attribution_confidence IS
  'Tier-aware trust signal. unverified=default; firm_level_only=know the firm not the attorney (Tier 0 CourtListener free tier); inferred=AI-suggested no human check (Tier 1); paralegal_verified=firm paralegal confirmed (Tier 2); attorney_verified=attorney confirmed (Tier 2); client_confirmed=client confirmed highest trust.';

-- 3. attorneys: firm-member flag + bar credentials (ADR-018)
-- Columns may already exist from the BIL migration (see header note); IF NOT
-- EXISTS makes this a no-op in that case while keeping ADR-018's intent in
-- a dedicated migration file.
ALTER TABLE attorneys
  ADD COLUMN IF NOT EXISTS is_firm_member boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS bar_number text,
  ADD COLUMN IF NOT EXISTS bar_state text;

COMMENT ON COLUMN attorneys.is_firm_member IS
  'True if this attorney row represents a member of the firm using LegalAI. Externals default false. Future users table will reference attorney_id.';

-- 4. external_ids jsonb across all 8 entity tables (ADR-021, 2026-04-20 decision)
ALTER TABLE attorneys   ADD COLUMN external_ids jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE judges      ADD COLUMN external_ids jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE prosecutors ADD COLUMN external_ids jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE officers    ADD COLUMN external_ids jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE courts      ADD COLUMN external_ids jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE agencies    ADD COLUMN external_ids jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE witnesses   ADD COLUMN external_ids jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE experts     ADD COLUMN external_ids jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN attorneys.external_ids IS
  'Multi-source identity map. Example: {"courtlistener_id":"4428","ballotpedia_id":"jane_doe","nv_bar_id":"7469"}. Enables cross-source reconciliation without coupling our UUIDs to any single provider.';

-- 5. GIN indexes on external_ids for fast lookups
CREATE INDEX idx_attorneys_external_ids   ON attorneys   USING GIN (external_ids);
CREATE INDEX idx_judges_external_ids      ON judges      USING GIN (external_ids);
CREATE INDEX idx_prosecutors_external_ids ON prosecutors USING GIN (external_ids);
CREATE INDEX idx_officers_external_ids    ON officers    USING GIN (external_ids);
CREATE INDEX idx_courts_external_ids      ON courts      USING GIN (external_ids);
CREATE INDEX idx_agencies_external_ids    ON agencies    USING GIN (external_ids);
CREATE INDEX idx_witnesses_external_ids   ON witnesses   USING GIN (external_ids);
CREATE INDEX idx_experts_external_ids     ON experts     USING GIN (external_ids);

COMMIT;
