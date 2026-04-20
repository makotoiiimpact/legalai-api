-- ============================================================================
-- Migration: 20260420_002_cases_external_ids
-- Purpose: Add external_ids to cases for cross-source reconciliation
-- References: ADR-021 (ICP/external-ids pattern), Phase 2 seed script spec
-- Applies to: legalai-dev (cfiaxrvtafszmgraftbk) first; prod promotion later
--
-- Notes:
--   Phase 1 migration (20260420_001_attorney_seed_prep) added external_ids to
--   the 8 entity tables (attorneys, judges, prosecutors, officers, courts,
--   agencies, witnesses, experts). `cases` wasn't in that list. Phase 2 seed
--   needs cases.external_ids to carry {courtlistener_docket_id, caseName,
--   docketNumber, court_id} for future reconciliation against firm-internal
--   case numbers, Clark County state docket numbers, etc.
-- ============================================================================

BEGIN;

ALTER TABLE cases
  ADD COLUMN external_ids jsonb NOT NULL DEFAULT '{}'::jsonb;

COMMENT ON COLUMN cases.external_ids IS
  'Multi-source identity map for the case. Example: {"courtlistener_docket_id":"66800213","courtlistener_docket_number":"2:22-mj-00171","courtlistener_court_id":"nvd"}. Enables reconciliation with Clark County state dockets, firm-internal case numbering, etc.';

CREATE INDEX idx_cases_external_ids ON cases USING GIN (external_ids);

COMMIT;
