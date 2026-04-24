# LegalAI Ship Log

Running record of shipped work. Newest entries at top. One entry per meaningful ship (migrations, features, infrastructure changes). Not a replacement for git log — a human-readable narrative layer on top.

---

## 2026-04-23 — Learning Lab Session 1 — Extraction model comparison infrastructure

**Commits:** `d3d1000`, `ea013cd`, `a3381e4`, `00c6bb8` on `legalai-api` `main` (all pushed to origin).

> R&D session. No changes to production schema, services, or client-facing routes. All work lives under `scripts/` in the `legalai-api` repo.

### What shipped

**`d3d1000`** — `feat(scripts): add model comparison harness for extraction R&D`
- File: `scripts/compare_extraction_models.py` (552 lines)
- Purpose: Single-document comparison across Claude (ground truth) + 4 Ollama models
- Architecture: provider abstraction via `_call_claude()` / `_call_ollama()`; lenient JSON parser that tolerates markdown-fenced responses; per-field + overall agreement scoring; markdown report output at `scripts/output/model_comparison_results.md`
- Config: `.gitignore` rule added for `scripts/output/*` (retains `.gitkeep`) so run outputs don't pollute `git status`
- Verification: `--help` renders; `--claude-only` smoke on `test_complaint.pdf` returned valid 8-field JSON in **2.21s**

**`ea013cd`** — `fix(scripts): disable Qwen thinking mode and lower temperature for deterministic extraction`
- +9 / −1 lines on `scripts/compare_extraction_models.py`
- Ollama payload now sets `"think": false` at top level and `"options": {"temperature": 0.1}`
- Trigger: pod testing showed Qwen 3.5:9b emits `<think>…</think>` monologue by default, blowing latency ~30× and corrupting `format: "json"` mode
- Safety: non-thinking models silently ignore the `think` flag — safe cross-family default

**`a3381e4`** — `fix(scripts): strip leaked think tags defensively in Ollama response parsing`
- +5 / −2 lines on `scripts/compare_extraction_models.py`
- Post-response `re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)` applied before the lenient parser, and reflected into `raw_response` so the report's raw-response section matches what got parsed
- Belt-and-suspenders: some Ollama adapters emit think tags even when `think=false`
- **This is the "pending defensive fix" referenced in the Session 1 Handoff — it shipped in Session 1, not deferred.**

**`00c6bb8`** — `feat(scripts): add batch extraction with resume, warmup, per-doc output`
- File: `scripts/batch_extract.py` (576 lines)
- Purpose: Batch extraction over a directory using a single chosen model
- Architecture: `asyncio.Semaphore(concurrency)` for bounded parallelism; warmup ping preloads weights before the batch loop; per-doc JSON output `{document, model, timestamp, latency_seconds, extraction, errors}`; `_summary.csv` at batch end; tqdm progress with `[N/total]` fallback; resume-by-default via skip-if-output-exists
- Import strategy: imports `EXTRACTION_PROMPT`, `EXTRACTION_FIELDS`, `load_document_text`, `parse_json_lenient` from `compare_extraction_models` via `sys.path` insert — deliberate; consolidation is Script 5's job (the `services/extraction.py` refactor)
- Verification: 5 docs × 4 models = **20/20 succeeded** on pod test, zero failures, all 8 fields populated

### Pod infrastructure

- Ollama **0.21.1** on RunPod RTX 4090 (24 GB VRAM)
- **4 model tags** pulled onto `/workspace` persistent volume (~39 GB total): `qwen3.5:9b`, `llama3.1:8b`, `mistral-nemo:12b`, `qwen3:14b`
- Recovery script at `/workspace/reinstall_ollama.sh` for container resets (pod container is ephemeral; models are not)
- Separate R&D Anthropic API key **`legalai-rnd-pod`** with monthly spend cap — see [Product Build Discipline Rule 11](https://www.notion.so/3473764230fa8171b95ef5cac71717c0)

### ADRs referenced

- [ADR-023 — LegalAI R&D model fleet selection (2026-04-23)](https://www.notion.so/34c3764230fa819eb5d6fb3b0ca309af) — fleet choice, `qwen3:32b` rejection rationale (4.3 tok/s, VRAM-bound), mandatory Ollama payload defaults

### Applied to

- `legalai-api` `main` ✅ 4 commits pushed to origin (`d3d1000 → ea013cd → a3381e4 → 00c6bb8`)
- Local Mac smoke testing ✅ Script 1 `--claude-only` + Script 2 2-doc Claude batch + resume re-run
- RunPod RTX 4090 (R&D pod) ✅ Script 2 full-fleet run — 20/20 docs succeeded
- `legalai-dev` (cfiaxrvtafszmgraftbk) — untouched
- `legalai` prod (kapyskpusteokxuaquwo) — untouched
- AI GC (wlksqdorclrxjbulvvik) — untouched

### Pending → resolved

- **Script 1 defensive `<think>` tag strip** flagged as "pending" in the Session 1 Handoff. **Confirmed: shipped as `a3381e4` on origin/main before session close.** Not a Session 2 blocker.

### Next

- Session 2 opening-move decision: quality-score first (Path A, recommended) vs Script 3 first (Path B) — see [Learning Lab Session 1 Handoff](https://www.notion.so/34c3764230fa8192ad55ce91930819b8)
- Script 3 / Script 4 / Script 5 remaining in the 5-commit sequence. Script 5 (`services/extraction.py` model-agnostic refactor) carries mandatory tests per `legalai-api/CLAUDE.md` test policy

### Cross-links

- [Learning Lab — Session 1 Handoff](https://www.notion.so/34c3764230fa8192ad55ce91930819b8)
- [ADR-023 — LegalAI R&D model fleet selection](https://www.notion.so/34c3764230fa819eb5d6fb3b0ca309af)
- [Product Build Discipline Rule 11 — R&D API key isolation](https://www.notion.so/3473764230fa8171b95ef5cac71717c0) *(new this session)*
- Repo: `scripts/compare_extraction_models.py`, `scripts/batch_extract.py`, `scripts/output/`

---

## 2026-04-23 — Surface Extraction Errors + Cap Polling

**Commits:** 1 api (e00b1b3) + 1 ui (c804ba6 in legalai-ui)
**Last pushed:** e00b1b3 on origin/main
**Migration applied:** 20260423_002_case_review_status_error.sql on legalai-dev (cfiaxrvtafszmgraftbk). Prod (kapyskpusteokxuaquwo) deferred to prod promotion session per Rule 3.

### Shipped
- `case_review_status` enum gained 'error' value on dev. Verified: `processing, needs_review, in_review, confirmed, shell, error`.
- `services/extraction.py::_mark_capture_error` now updates both `capture_events.status='error'` AND `cases.review_status='error'`. Signature gained `case_id`; all 3 call sites updated.
- `routes/intake.py::get_extraction` state derivation: `processing → "extracting"`, `error → "error"`, otherwise `"complete"`. Frontend's existing `state === "error"` branch at processing/page.tsx:78 now fires.
- legalai-ui processing page: `MAX_POLLS = 120` (≈108s at 900ms) + `isTimedOut` state + amber "taking longer than expected" banner. Covers the network-death / Railway-crash case where backend never updates.

### Reverses prior design decision
- 2026-04-21 entry said: "No 'error' value on case_review_status enum — errors surfaced via capture_events only." That decision left the UI polling forever on any Claude failure because `get_extraction` only reads `cases.review_status`, not `capture_events.status`. This ship adds the enum value and flows it through.

### Not shipped (intentional)
- Prod migration apply — needs Rule 3 review in the prod promotion session.
- "Try again" button wiring — button exists at processing/page.tsx:142 but has no onClick yet (separate spec).

### Verification
- tsc clean, eslint clean, Python imports clean.
- Manual smoke test per spec (kill Railway / break ANTHROPIC_API_KEY → confirm error banner instead of spinner) pending on a running dev deploy.

---

## 2026-04-21 — Real Claude Extraction Pipeline

**Commits:** 3 (extraction pipeline + charges parser + CHECK-value fix)
**Last pushed:** 5c36dc1 on origin/main

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
- Pipeline verified end-to-end on Railway (5c36dc1): test_complaint.pdf → Claude extraction → 4 entity cards (Kephart, Chen, Ogata auto-confirmed, Martinez) + 2 charges with statutes + case fields populated. CHECK constraint fix (extracting/awaiting_review status values) shipped mid-session.

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
