# LegalAI Ship Log

Running record of shipped work. Newest entries at top. One entry per meaningful ship (migrations, features, infrastructure changes). Not a replacement for git log — a human-readable narrative layer on top.

---

## 2026-04-21 — Real Claude Extraction Pipeline

**Commits:** 2 (extraction pipeline + charges parser)
**Last pushed:** f7cdd00 on origin/main

### Shipped
- Real Claude PDF extraction replacing simulate_extraction() stub
- services/extraction.py: extract_text_from_pdf (pdfplumber) → extract_entities_with_claude (claude-sonnet-4-20250514) → match_entity_against_existing
- Entity types: judge, prosecutor, defense_attorney, defendant, case_number, court, filed_date, charges
- Confidence scoring 0.0-1.0 from Claude prompt, mapped to high/medium/low labels on read
- Entity matching against existing judges/prosecutors/attorneys tables (⚡ matched UX)
- Firm-member auto-confirm (is_firm_member=true → review_status='confirmed')
- Cases row populated from extraction: case_number, jurisdiction, incident_date, client_name, charge
- Charges written as semicolon-delimited string, parsed into individual Charge objects with statute extraction in build_charges()
- Error handling: ExtractionError for bad PDFs, malformed Claude JSON; unexpected errors caught; all surfaced via capture_events.status='error' + processing_error
- Extracted text cached in capture_events.raw_payload for re-extraction without re-download
- Test PDF generator: scripts/create_test_complaint.py (Nevada DUI complaint with Kephart/Chen/Ogata/Martinez)

### Not shipped (intentional)
- OCR for scanned PDFs (v2)
- Charges normalization migration (separate charge rows vs single text column)
- prior_case_count on entity matches (needs aggregation tables, post-demo)
- Prod promotion (dev only, cfiaxrvtafszmgraftbk)

### Design decisions
- Sonnet not Opus for extraction (cost + speed, structured data task)
- 2-arg signature preserved for BackgroundTasks compatibility
- No 'error' value on case_review_status enum — errors surfaced via capture_events only
- Charges as concatenated text in cases.charge, parsed into array on read (schema unchanged)

---

## 2026-04-20 — Tier 0 Federal Seed Applied + external_ids extension

### Shipped
- **Phase 1 migration (20260420_001)**: `attribution_confidence` enum (6 values), `case_attorneys.attribution_confidence` column, attorneys `is_firm_member` / `bar_number` / `bar_state` (IF NOT EXISTS — BIL migration already added them), `external_ids` jsonb + GIN indexes on all 8 entity tables.
- **Phase 2 schema extension (20260420_002)**: `cases.external_ids` jsonb + GIN index, closing the gap between Phase 1 migration and Phase 2 seed data map.
- **Phase 2 seed script (`scripts/seed_tier_0_federal.py`)**: 411 lines, idempotent via `external_ids` containment checks, applied cleanly. **23 rows written**: 1 Garrett attorney, 1 USAO agency, 5 AUSA firm-records, 5 cases (all `tier_0_public`), 5 Garrett defense_lead `case_attorneys` (4 `attorney_verified` + 1 `firm_level_only`), 6 AUSA prosecution_lead `case_attorneys` (all `firm_level_only`).
- **Idempotency verified empirically**: re-run of `--apply` produces 23 SKIPs, 0 INSERTs.
- All DDL + DML on **legalai-dev** (`cfiaxrvtafszmgraftbk`). Prod (`kapyskpusteokxuaquwo`) untouched.

### Decisions / ADRs Exercised
- **ADR-017 (`firm_level_only` attribution)** — validated end-to-end via docket 64877115 (ancillary SEC filing in D. Arizona where Garrett's firm appeared but his name didn't). Exact design case.
- **ADR-018 (firm member flag + bar credentials)** — Garrett seeded as `is_firm_member=true`, `bar_number=7469`, `bar_state=NV`. External AUSA rows `is_firm_member=false`.
- **ADR-021 (external_ids pattern)** — extended beyond the original 8 entity tables to also cover `cases` in migration 002.
- **ADR-022 (idempotent migration style)** — both migrations use `ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` where applicable; seed script guards every insert by containment check.

### Open Threads for Next Session
- Judge seeding (Elayna J. Youchah, Cristina D. Silva) deferred to Ballotpedia pass.
- Service-role key hygiene: `.env` currently carries one `SUPABASE_SERVICE_KEY`; `SUPABASE_DEV_SERVICE_KEY` separation flagged for a future cleanup.
- Prod promotion of `20260420_001` + `002` migrations pending explicit approval.
- Clark County state judge manual seed — real operational value, still not started.

### Environment State After Ship
- legalai-dev (`cfiaxrvtafszmgraftbk`): 30 tables, 23 rows of real Tier 0 data.
- legalai prod (`kapyskpusteokxuaquwo`): untouched.
- origin/main: this commit is the only delta since yesterday's session close.

---

## 2026-04-19 — Behavioral Intelligence Layer + Baseline

### Shipped
- **Retroactive baseline migration** captured prod schema as of today's date. 6 tables (cases, documents, chunks, findings, disposition_memos, audit_log), 4 extensions (pgcrypto, uuid-ossp, vector, pg_stat_statements), 2 functions (match_chunks, update_updated_at), 1 trigger, 1 view, 1 UNIQUE constraint, pgvector ivfflat index on chunks.embedding. Applied clean to legalai-dev. Commit: f5a6c7d.
- **Behavioral intelligence layer migration** adds 24 new tables forming the firm-owned-IP infrastructure: 9 entity tables (attorneys/judges/prosecutors/officers/witnesses/experts/courts/agencies/devices), 4 case-join tables, 4 event tables, 5 aggregation tables, 2 capture tables. 26 seeded NV criminal motion types. DB-level CHECK constraint on officers.public_record_only. Soft-delete on entity_observations. Cases table extended with data_tier + firm_id. Applied clean to legalai-dev (30 tables total). Commit: c816673.
- **Supabase dev environment** spun up as separate project (`legalai-dev`, cfiaxrvtafszmgraftbk, us-west-2). Prod stays at kapyskpusteokxuaquwo.
- **CourtListener probe and extraction** surfaced 8 federal criminal cases, 6 federal NV judges, 5 AUSA firm variants for Garrett. Output in scripts/probe_output/garrett_parties_nv_criminal.md. Paywall prevented proper /parties/ endpoint access — extraction done from existing search page dumps instead.

### Decisions Made (ADRs 12-18)
- **ADR-012** — Retroactive baseline migration for existing prod schema. No more dashboard DDL.
- **ADR-013** — Prod has no RLS (documented, pending explicit product decision before multi-tenant).
- **ADR-014** — Precondition surprises trigger full investigation, not workaround. Process rule.
- **ADR-015** — When bootstrapping a new Supabase env, mirror prod (don't stub).
- **ADR-016** — Baseline migration ordering (Extensions → Tables → Functions → Indexes → Triggers → Views) and explicit pgvector dimensions.
- **ADR-017** — CourtListener data tier constraints and attribution caveats. Don't pay to upgrade — wrong data. Add case_attorneys.attribution_confidence column for firm-level-only attribution.
- **ADR-018** — Firm members are attorneys (is_firm_member flag) plus a future user layer on top. Enables firm-vs-firm matchup math and future associate/paralegal roles.

### Discipline Rules Added
- Every schema change goes through a versioned migration file — no dashboard DDL on prod.
- Bootstrap a new Supabase env by mirroring prod, not stubbing.
- Baseline migrations use canonical ordering: Extensions → Tables → Functions → Indexes → Triggers → Views.

### Open Threads for Next Session
- Natural key strategy for seeded entities (CL IDs vs our own UUIDs) — leaning Option B
- Prep migration scope (one migration for ADR-017 + ADR-018, or separate) — leaning one
- Draft 20260420_001_attorney_seed_prep.sql
- Draft scripts/seed_tier_0_federal.py
- Clark County state judge manual seeding (Ballotpedia) — real operational value, not yet started
- RLS posture decision on existing 6 prod tables (ADR-013 is open)

### Environment State After Ship
- legalai (prod): 6 tables. Untouched. legalai.iiimpact.ai still running Phase 1 product.
- legalai-dev: 30 tables. Full baseline + behavioral intelligence. Empty of data.
- origin/main: 2 commits ahead compared to start of day (now current).

### Related Session Artifacts
- [Session Retro 2026-04-19](https://www.notion.so/3483764230fa810ab404f01c62904abe)
- [Architecture Decision Log](https://www.notion.so/3473764230fa81f193bec4e2f9bf6ae4) (18 entries)
- [Schema & Data Model](https://www.notion.so/3473764230fa8179865ac25d381feef4)
- [Product Build Discipline](https://www.notion.so/3473764230fa8171b95ef5cac71717c0)

---
