# LegalAI Ship Log

Running record of shipped work. Newest entries at top. One entry per meaningful ship (migrations, features, infrastructure changes). Not a replacement for git log — a human-readable narrative layer on top.

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
