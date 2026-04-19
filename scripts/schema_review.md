# Schema Self-Review — 2026-04-20

_Mac CC's own critique of `supabase/migrations/20260420_behavioral_intelligence_layer.sql`. Read this before running the migration._

## What's in the file

- **Core entities (9):** courts, agencies, devices, attorneys, judges, prosecutors, officers, witnesses, experts
- **Relationship tables (4):** case_attorneys, case_officers, case_witnesses, case_devices (each with role discriminator)
- **Event tables (4):** motions (with `raw_docket_text`, `motion_type_id`, `confidence`, `needs_review`), motion_types (with `is_seeded`, `seeded_source`, `real_usage_count`, `deprecation_status`), hearings, plea_offers
- **Aggregation tables (5):** judge_motion_stats, prosecutor_patterns, matchup_history, officer_patterns, entity_observations
- **Capture pipeline (2):** capture_events, extraction_candidates
- **ALTER on cases:** `data_tier` (enum), `firm_id` (nullable uuid)
- **Seeds:** 26 Nevada criminal procedure motion types (suppression variants, dismiss variants, limine, continuance, sever, venue, new trial, reconsideration, stay, bail, discovery, compel, quash)
- **RLS:** enabled on every firm-private table with a permissive `authenticated` policy, annotated `-- TIGHTEN PRE-MULTI-TENANT` at the tightening site
- **Triggers:** one shared `set_updated_at()` function used by 20+ tables
- **Indexes:** FK indexes everywhere + common-query composites (e.g., `motions(judge_id, motion_type_id, ruling)` for the Matchup Card query)

## Design decisions I made (documented in the SQL)

1. **Soft-delete on sensitive entity tables, hard-delete on events.** Officers, judges, prosecutors, attorneys, witnesses, experts, agencies, devices get `deleted_at`. Motions, hearings, plea_offers are append-only and hard-delete on bad-entry only.
2. **`raw_docket_text` preserved forever on `motions`** even when `motion_type_id` is assigned. Supports audit trail, re-extraction with better prompts, and the "click through to source" compliance requirement.
3. **Confidence tier is a stored column, not a computed expression.** The nightly recompute job writes `confidence_tier` to aggregation tables using n-based thresholds (n≥20 confirmed, n≥5 suggested, n<5 signal, plus 'inferred' for derived-from-public patterns). Query-time consistency beats query-time freshness here.
4. **Aggregation tables are physical tables, not views.** Views would be simpler but would collapse all query work onto read time and would fight our confidence-tier semantics (which have to be stable within a day).
5. **Polymorphic `entity_observations` and `extraction_candidates`** (discriminator `entity_type` + raw `entity_id uuid`). I traded relational purity for UI simplicity — observations are the "long tail" of whatever attorneys want to write about any entity, and 9 separate observation tables would explode the capture UI code. Downside: DB-level integrity is not enforced; the application layer must validate `(entity_type, entity_id)` on insert.
6. **`matchup_history` is directional.** `attorney_a` = "our side", `attorney_b` = "their side", with separate role columns. Avoids a confusing "pick the lower UUID" canonical-order rule and makes the matchup card query one clean lookup.

## Tradeoffs I chose (open for Makoto to reverse)

- **`data_source` is freeform text.** Could be an enum (`courtlistener`, `ballotpedia`, `firm_entered`, `ai_extracted`, `voice_memo`, `email_forward`, `calendar_sync`). Left as text for now because Tier 0 ingestion sources multiply quickly (Bar directories, NV appellate, news, etc.). If drift becomes a problem we add a check constraint later.
- **Polymorphic observations vs. per-entity tables.** See decision #5 — reverse if you want strict referential integrity.
- **Aggregation tables over materialized views.** Easy to reverse (swap for `create materialized view` + `refresh materialized view concurrently`). I chose tables because I wanted atomic writes per recompute job instead of full-view refresh stalls.
- **Nullable `firm_id` everywhere.** Required because we don't have a `firms` table yet. Makes multi-tenant RLS harder to tighten (every query needs a `firm_id IS NULL OR firm_id = ...` predicate). Alternative: create an implicit "Garrett" `firms` row as part of this migration and backfill it. I didn't, to keep the migration additive-only and keep Makoto's review surface small.
- **`created_by` columns are plain `uuid`, not FKs to `auth.users`.** Supabase convention is to reference `auth.users(id)` directly. I left it unreferenced to avoid coupling to the auth schema in this migration — easy to add later with a single `alter table ... add constraint`.
- **No `firms` table.** Spec said "nullable for now, required post-multi-tenant". The follow-up migration that introduces `firms` will handle backfill + NOT NULL + RLS tightening.

## Questions for Makoto

> **Q1. `cases.id` PK type.** I assumed `cases.id` is `uuid`. If it's `bigint`, every FK from `case_attorneys`, `case_officers`, `case_witnesses`, `case_devices`, `motions`, `hearings`, `plea_offers`, `capture_events` is broken. **Please verify before running UP.** One-liner: `select data_type from information_schema.columns where table_name='cases' and column_name='id';`

> **Q2. Seed Nevada courts + judges in this migration?** I did not seed any court or judge rows. The schema is ready, but we need a separate ingestion script that pulls from CourtListener and writes. Do you want the courts/judges seed bundled into this migration (more complete but heavier rollback), or kept in a `scripts/seed_courts_judges.py` followup?

> **Q3. Implicit Garrett firm row.** Related to Q2. If I create a `firms` table + a single Garrett row as part of this migration, I can backfill `firm_id` on `cases` and all new rows, which makes RLS tightening a one-line change later. Cost: one more ALTER in this migration, bigger rollback surface. **I default to: wait, do it in the multi-tenant migration.** Confirm?

> **Q4. Motion type scope.** I seeded 26 NV-specific criminal motions. No civil / PI / family law. That's aligned with phase 1 (criminal defense = Garrett's vertical). When we expand to PI (Q3 2026 per Notion), we add 15–20 more with `is_seeded=true`. Confirm the criminal-only seed is correct for now?

> **Q5. `public_record_only` on officers and officer_patterns.** I set default `true`. Per the compliance framework this is mandatory. Should we also add a DB-level CHECK that it can never be set to false? Or is this a product-layer enforcement? I left it as a soft default for now — CHECK feels too strong for a flag that doesn't actually gate anything the DB can see.

> **Q6. `entity_observations` deletion semantics.** Currently hard-delete. Should these be soft-deleted too? Attorneys may want to retract observations without losing the audit trail. I'd lean soft-delete. Say the word and I add `deleted_at`.

> **Q7. Cross-firm shared records.** `firm_id IS NULL` means "shared/public". Currently the permissive RLS policy doesn't distinguish — an authenticated user can INSERT with `firm_id = null`. Pre-launch we should restrict shared-write to admin/service role. Flagged but not fixed in this migration.

> **Q8. Confidence-tier recompute cadence.** I assumed nightly. If we need "5 minute" confidence freshness for the Matchup Card, we'd need trigger-based stat maintenance instead. Nightly seems right for now — confirm?

> **Q9. Prosecutor pattern_value jsonb shape.** I deliberately left this schemaless because different `pattern_type`s have different shapes (`opening_plea_offer` is money+charges, `brady_compliance` is a list of incidents). Downside: no type safety. Alternative: normalize each pattern_type into its own table. I'd prefer to build out a handful of patterns in jsonb, then normalize the shape-stable ones later. OK?

> **Q10. Partitioning / archival strategy.** Not addressed. As a firm accumulates 10K+ cases, `motions`, `hearings`, and `entity_observations` will be the largest tables. PARTITION BY RANGE on `created_at` (yearly) would help, but premature for now. Flagged for 2027.

## What I did NOT do

- **Did not execute the migration.** Both files are in `supabase/migrations/` as DRAFT.
- **Did not touch Supabase.** No API calls to `wlksqdorclrxjbulvvik`.
- **Did not commit to git or push to Railway.**
- **Did not create a `firms` table.** Deferred to multi-tenant migration.
- **Did not populate any entity rows.** Only motion_types seed.

## Suggested review sequence for Makoto

1. Run `select data_type from information_schema.columns where table_name='cases' and column_name='id';` — verify uuid (30 sec).
2. Skim the design-decision header block in the UP SQL file (5 min).
3. Skim Qs 1–10 above and mark the ones that need changes (5 min).
4. Eyeball the motion_types seed list for coverage holes (3 min).
5. If green-lit, run UP in a Supabase branch first (not prod).
