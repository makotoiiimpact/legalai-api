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

## Agent Workflow Rules (inherited from Makoto's agency OS)
1. **Committed ≠ deployed.** When reporting a task shipped, verify `git push` has run AND the Vercel/Railway deploy is live.
2. **No destructive actions without explicit approval.** Migrations, deletes, mass inserts — draft and request review before executing.
3. **Draft, don't execute, for schema migrations.** Write UP + DOWN + a review doc. Wait for human green-light.
4. **For any migration that FKs into an existing table:** the FIRST tool call verifies the referenced column's type via `information_schema`. Do not assume uuid vs bigint.

## Current Active Work
Behavioral intelligence layer schema migration drafted (24 tables + ALTERs on `cases`). See `supabase/migrations/20260420_*.sql` and `scripts/schema_review.md`.

---
_Last updated: 2026-04-19_
