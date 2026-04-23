-- 20260423_002_case_review_status_error.sql
-- Add 'error' to case_review_status enum so run_extraction's error path
-- can mark the case as failed. Without this value, cases.review_status
-- is left at 'processing' on failure and the UI's /extraction endpoint
-- maps that back to state:"extracting" — the frontend spins forever
-- instead of showing the existing error banner at processing/page.tsx.
--
-- Target: legalai-dev (cfiaxrvtafszmgraftbk) now.
-- Prod (kapyskpusteokxuaquwo) applies during the prod promotion session
-- per Rule 3 — draft-don't-execute for prod migrations.
--
-- PG 12+ permits ALTER TYPE ADD VALUE inside a transaction, but the new
-- value cannot be referenced in the same transaction. No BEGIN/COMMIT
-- wrapper here so downstream callers can use 'error' immediately.

ALTER TYPE case_review_status ADD VALUE IF NOT EXISTS 'error';
