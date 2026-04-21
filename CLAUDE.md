# LegalAI — Project Context for Terminal Agents

This file is read by Claude Code and other AI coding agents when operating in this repo. Keep it accurate and concise. When something material changes (production URLs, Supabase project, major architecture shift), update this file in the same commit.

## Product
LegalAI is a behavioral intelligence platform for legal practice. Customer zero: Garrett T. Ogata (Las Vegas DUI attorney, 2,200+ cases). Positioning: "firms build their own IP." Architecture doctrine: harness engineering (agent = model + harness). See Notion for full strategy docs.

## Production Stack
- **Frontend:** https://legalai.iiimpact.ai (Vercel, Next.js 16 / React 19 / Tailwind v4)
- **API:** https://web-production-e379a8.up.railway.app (FastAPI on Railway)
- **Database:** Supabase project `legalai` — project ID `kapyskpusteokxuaquwo` (us-west-2, Postgres 17.6)
- **GitHub:** makotoiiimpact/legalai-api and makotoiiimpact/legalai-ui
- **Demo passcode:** ogata2024

## ⚠️ Supabase Project Discipline
Makoto operates multiple Supabase projects. Do NOT inherit a project ID from another repo's CLAUDE.md or from memory of another session.

- `kapyskpusteokxuaquwo` — **legalai** (this repo) — us-west-2
- `wlksqdorclrxjbulvvik` — **AI GC** (different product entirely: construction cost analyzer) — us-east-1

Before any Supabase MCP call, verify you are targeting `kapyskpusteokxuaquwo` for any work originating in this repo. If unsure, ask before running.

## Python Environment
- venv at `./venv` uses Python 3.14
- Known incompatibility: `python-dotenv.find_dotenv()` — use `load_dotenv('.env')` with an explicit path instead
- If more 3.14 bugs surface, downgrade to 3.12

## External Integrations
- **CourtListener API:** `COURTLISTENER_API_KEY` in Railway env + local `.env`. User-Agent: `LegalAI-IIIMPACT/0.1 (makoto@iiimpact.ai)`. Rate limit: 1.5s between calls.
- **Anthropic / OpenAI / Supabase keys:** in `.env` (gitignored)

## Route Architecture
- Old routes: top-level paths in main.py (/cases, /health, etc.)
  These hit prod (kapyskpusteokxuaquwo). Do not modify.
- New routes: routes/intake.py mounted at /api/v2 in main.py.
  These hit dev (cfiaxrvtafszmgraftbk). All UX Design v1
  contract endpoints live here.
- Field mappings (demo concessions documented in intake.py header):
  client_name→caseName, jurisdiction→court, incident_date→filedDate,
  charge (text)→Charge[] of length 1,
  attributionConfidence derived from review_status.

## Agent Workflow Rules (inherited from Makoto's agency OS)
1. **Committed ≠ deployed.** When reporting a task shipped, verify `git push` has run AND the Vercel/Railway deploy is live.
2. **No destructive actions without explicit approval.** Migrations, deletes, mass inserts — draft and request review before executing.
3. **Draft-don't-execute for schema migrations targeting prod** — UP + DOWN + review doc + human green-light. Dev applies are permitted after diagnostic verification.
4. **For any migration that FKs into an existing table:** the FIRST tool call verifies the referenced column's type via `information_schema`. Do not assume uuid vs bigint.

### 5 — Every schema change goes through a versioned migration file
No exceptions. No dashboard-driven DDL on prod. Every schema change to any Supabase environment (prod, dev, staging, anywhere) must land as a committed SQL file in supabase/migrations/. If you encounter unrecorded schema drift in an existing product, the first migration task is a retroactive baseline capturing current state. See ADR-012.

### 6 — Bootstrap a new Supabase env by mirroring prod, not stubbing
Before applying new migrations to a fresh Supabase project, inventory prod's schema (tables, extensions, functions, triggers, RLS state, custom types), draft a baseline migration that faithfully reproduces it, apply to the new env, and verify parity. Then layer new migrations on top. Stubs hide drift. See ADR-015.

### 7 — Canonical migration ordering for pgvector + SQL-language functions
When a migration includes SQL-language functions and/or pgvector columns, use this order: Extensions → Tables → Functions → Indexes → Triggers → Views. SQL-language function bodies are parsed at CREATE time (must be after referenced tables). pgvector columns require explicit dimension (vector(1536) for text-embedding-3-small, vector(3072) for text-embedding-3-large). Bare `vector` fails at ivfflat index creation. Static validators (sqlparse) don't catch either bug — only runtime apply does. See ADR-016.

## Current Active Work
V2 intake API routes shipped (routes/intake.py, 11 endpoints
under /api/v2). Frontend wired to Railway. Entity extraction
is stubbed (3s delay + mock entities). Matchup returns fixture
data on confirmed cases. Auth = none (demo passcode at frontend).

Backend delta migrations (001 + 002) applied to legalai-dev:
case_review_status enum, extraction_candidates review/match/
correction columns, case-documents storage bucket + RLS.

Known stubs awaiting separate specs:
- Real Claude extraction pipeline
- Real aggregation-driven matchup computation
- Auth middleware
- Schema alignment migration (case_name, court, court_dept,
  filed_date as proper columns; charges normalization;
  attribution_confidence on extraction_candidates;
  nullable client_name)
- Backfill Tier 0 seed cases review_status from 'shell'
- Rule 3 qualifier applied (see below)

---
_Last updated: 2026-04-21_
