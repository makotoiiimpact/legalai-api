# Makoto Review — Behavioral Intelligence Layer

_Single-page executive brief for the 2026-04-19 autonomous session. Optimized for 15-minute skim._

---

## 1. Executive Summary

- **What got built:** a complete Supabase schema migration for the behavioral intelligence layer (24 new tables + ALTERs on `cases`), plus a full read-only CourtListener probe of filter whitelists and Garrett's 89 federal RECAP dockets. All draft, nothing applied.
- **Biggest wins:** (a) confirmed that chained CL filters (`positions__court__*`) are hard-rejected, so future probes have a clean whitelist to target; (b) the migration is self-documenting — the design-decision rationale lives at the top of the SQL so future Claude sessions (and human readers) can't accidentally undo intent.
- **Biggest risk:** **Q1 — we assumed `cases.id` is `uuid`.** If it's `bigint`, every foreign key in the migration breaks. 30-second verification required before UP runs. This is the only truly blocking question.
- **Needs your decision:** Q1 (blocking). Q2–Q10 are deferrable but clean up quickly — I have defaults and recommendations on all of them below.
- **Estimated time to green-light the migration:** ~20 minutes. 5 min to run Q1 verification + skim Q2–Q10 defaults. 10 min to apply UP in a Supabase branch and confirm clean apply. 5 min to promote.

---

## 2. The 10 Open Questions

| # | Question (verbatim from `schema_review.md`) | My recommendation | Tradeoff if you disagree | Blocking? |
|---|---|---|---|---|
| **Q1** | **`cases.id` PK type.** I assumed `uuid`. If `bigint`, every FK breaks. | Run `select data_type from information_schema.columns where table_name='cases' and column_name='id';`. If `bigint`, tell me and I'll swap all case FKs in one edit. | None — this has to be right. | ✅ **BLOCKING** |
| **Q2** | **Seed NV courts + judges in this migration, or separate script?** | **Separate script** (`scripts/seed_courts_judges.py`). Migration stays schema-only and fully reversible; seeds can be re-run against prod without a rollback penalty. | Bundling means one fewer artifact to track, but data in a migration makes the DOWN destructive. | Deferrable |
| **Q3** | **Implicit Garrett `firms` row now?** | **Wait.** Create in the multi-tenant migration when we actually need it. Nullable `firm_id` today costs nothing. | Creating it now makes RLS tightening a one-line change later instead of a backfill job. Small benefit, larger review surface. | Deferrable |
| **Q4** | **Motion type scope: criminal-only seed (26 types)?** | **Confirm criminal-only.** Garrett is the vertical. Add PI in Q3 when we expand. | Broader seed now = more dead rows in `motion_types` that `real_usage_count=0` filters can hide anyway. | Deferrable |
| **Q5** | **DB-level CHECK forcing `public_record_only = true` on officers?** | **No CHECK.** It doesn't gate anything the DB can see — product layer enforces the compliance rule. Soft default is fine. | DB-level would catch a rogue INSERT bypassing the app, but we already require service-role writes for officer patterns. | Deferrable |
| **Q6** | **`entity_observations` deletion — soft or hard?** | **Add `deleted_at` (soft).** Attorneys will want to retract. Audit value is high. One-line change. | Hard-delete is simpler to reason about; soft-delete means every query needs `where deleted_at is null`. | Deferrable |
| **Q7** | **Cross-firm shared records (`firm_id IS NULL`) — who can write?** | **Service role only, post-launch.** Right now any authenticated user could INSERT with `firm_id=null`. Flagged in SQL but not enforced. Tighten before second customer. | Permissive now enables fast demo; mistake-proofing needed before multi-tenant. | Deferrable |
| **Q8** | **Confidence-tier recompute cadence: nightly vs. real-time?** | **Nightly.** The Matchup Card doesn't need sub-hour freshness. Trigger-based maintenance is a year-2 problem. | Real-time adds complexity (compensating decrements on soft-delete, row locks). Not worth it yet. | Deferrable |
| **Q9** | **Prosecutor `pattern_value` jsonb shape — schemaless or typed?** | **Keep jsonb.** Pattern shapes differ per type. Normalize the stable ones later. | Typed tables give referential clarity but balloon the table count before we know which patterns matter. | Deferrable |
| **Q10** | **Partitioning strategy for large tables?** | **Defer to 2027.** `motions` / `hearings` / `entity_observations` won't exceed a few million rows in year one. | Earlier partitioning = no backfill pain later, but premature for one-firm scale. | Deferrable |

**Summary: 1 blocking, 9 deferrable.** Every deferrable item has a default you can accept by silence.

---

## 3. Schema Highlights

### Top 5 design choices to validate

1. **Soft-delete on sensitive entity tables; hard-delete on events.** Officers, judges, prosecutors, attorneys, witnesses, experts, agencies, devices get `deleted_at`. Motions, hearings, plea offers are append-only with hard-delete for error correction only. Rationale: preserves audit trail on the highest-sensitivity surface (officer profiles) while keeping event math clean.
2. **`raw_docket_text` is preserved on every `motion` even after AI assigns `motion_type_id`.** Supports (a) paralegal re-classification when `needs_review=true`, (b) re-extraction with better prompts, (c) click-through-to-source per compliance framework.
3. **Confidence tier is a *stored* column, recomputed nightly.** Thresholds are n≥20 confirmed / n≥5 suggested / n<5 signal / 'inferred' for public-baseline patterns. Query consistency beats query freshness.
4. **Aggregation tables, not materialized views.** Atomic per-row recompute beats full-view refresh stalls. Easy to reverse if wrong.
5. **Polymorphic `entity_observations` and `extraction_candidates`.** One table per entity type would explode the capture UI. DB integrity is weakened; application layer validates.

### Counts & anomalies

| Artifact | Count |
|---|---|
| New tables | 24 (9 core, 4 case-joins, 4 events, 5 aggregations, 2 capture) |
| Cases ALTERs | 2 (`data_tier`, `firm_id`) |
| Seeded motion types | 26 (NV criminal) |
| RLS-enabled tables | 22 |
| Indexes | FK + common-query composites (e.g. `motions(judge_id, motion_type_id, ruling)`) |
| sqlparse statements | 183, clean split |

**Alignment with the Notion architecture doc:** every entity named in the Notion "Entity Catalog" section has a home. Four tiers of data (0/1/2/3) map to the `data_tier` column. The four capture principles (AI-does-80%, capture-where-work-happens, system-learns) map to the `capture_events` + `extraction_candidates` pair. One deliberate departure: **I did not create a `firms` table** — deferred to the multi-tenant migration (Q3).

---

## 4. Garrett RECAP — Key Findings

**89 dockets pulled.** API reported 88; pagination returned one extra (cursor drift). Date range: **1997-06-23 → 2025-08-20**.

### Top courts

| Court ID | Court | Dockets |
|---|---|---|
| `nvd` | D. Nevada (federal trial) | 22 |
| `deb` | D. Delaware Bankruptcy | 18 |
| `nysb` | S.D.N.Y. Bankruptcy | 8 |
| `cacd` | C.D. California | 6 |
| `nvb` | D. Nevada Bankruptcy | 2 |

**Nevada federal footprint: 24 cases (22 `nvd` + 2 `nvb`).**

### Year peaks

2020 (13), 2022 (12) — clear activity spikes. 2015 and 2008 each saw 6. Steady baseline of 2–4 cases/year otherwise.

### Top 5 federal judges

| Judge | Dockets | Court |
|---|---|---|
| Brendan L. Shannon | 5 | `deb` (Delaware Bankruptcy) |
| Martin Glenn | 5 | `nysb` |
| Elayna J. Youchah (incl. referred) | 6 combined | `nvd` |
| James C. Mahan | 4 | `nvd` |
| Karen Owens | 4 | `deb` |

**Important:** these are **federal** judges. Garrett's day-to-day is Nevada state DUI/criminal — those judges do NOT appear in this set because RECAP is federal-only.

### Sensitive flags

**Zero.** The `is_sensitive` pattern matched nothing — as expected, because the 2022 NV Supreme Court discipline matter (*In Re: Discipline Of Garrett Tanji Ogata*, Bar No. 7469) is an **opinion**, not a RECAP docket. It lives in the earlier `/opinions/` probe result (`01_opinions_search.json`, result #1).

### Data-quality caveats

- **Attorney/firm fields are empty** from elastic search results. Full AUSA and opposing-counsel rosters require follow-up `/dockets/{id}/parties/` calls. Easy to add in a future probe.
- **Name-collision noise is high.** Many results match other "Ogata" defendants or "Garrett" as a first name elsewhere. Before seeding these into `cases` as Tier 0 baseline, we need a labeling pass to distinguish Garrett-as-counsel vs. Garrett-as-subject vs. unrelated.

---

## 5. Recommended Next Session Sequence

| # | Step | Who | Time |
|---|---|---|---|
| 1 | Verify `cases.id` type (Q1). Accept defaults on Q2–Q10 or override. | **Makoto** | 5 min |
| 2 | If `cases.id = bigint`, swap case FKs across UP + DOWN files. | Mac CC | 5 min |
| 3 | Apply UP to Supabase **branch** (not prod). Confirm 183 statements execute. | Mac CC (you watch) | 10 min |
| 4 | Promote branch → prod. | **Makoto** (push button) | 2 min |
| 5 | Write `scripts/seed_courts_judges.py` — pulls federal NV courts + judges from CL, writes Tier 0 baseline. | Mac CC alone | 30 min |
| 6 | Write `scripts/label_garrett_recap.py` — human-assisted pass to mark each of the 89 dockets as `garrett_counsel` / `garrett_subject` / `name_collision`. | Mac CC + **Makoto** review | 20 min |
| 7 | Write `scripts/seed_garrett_recap.py` — ingests the labeled subset as Tier 0 `cases` + `motions` + `case_attorneys`. | Mac CC alone | 45 min |
| 8 | Build Matchup Card API route. Aggregation tables will be empty — UI shows "not enough data" states gracefully. | Mac CC alone | 90 min |
| 9 | Build capture_events → extraction_candidates pipeline for document uploads. | Mac CC alone | 2 hrs |
| 10 | Phase 1A demo video for Garrett (per Notion schedule). | **Makoto** drives | 1 hr |

**Critical dependency:** step 2 blocks 3, which blocks 5 onward. Everything after step 4 runs in parallel if needed.

---

## 6. What I Would Do Differently In Hindsight

1. **I should have verified `cases.id` type first** before writing a single FK. That one query was the cheapest thing in the whole session and would have removed all remaining risk from the migration. Process lesson: for any migration that FKs into an existing table, the FIRST tool call should verify the referenced column's type.

2. **I wrote the RLS policy loop with a `%I_auth_all` format string that was silently broken** (literal `_auth_all` outside the quoted identifier). Caught it on re-read, fixed it, but had I not re-read I would have shipped a migration that generated policies named `"agencies_auth_all"_auth_all` — parse error at apply time. Lesson: for any `format()` or templated SQL, compose the full identifier in a variable first, then pass as a single `%I` arg.

3. **I could have enriched the RECAP dockets** with `/dockets/{id}/parties/` calls to get the attorney roster. 89 dockets × 1.5s = 2 min. Would have given a real opposing-counsel / AUSA inventory. Skipped for scope but this was the highest-ROI thing I could have added.

4. **The schema leans "one big migration" rather than 3 sequential migrations** (entities → events → aggregation+capture). One file is easier to review as a whole but harder to roll back partially. In hindsight I'd split — if the UP fails midway on step 7 of a 3-file sequence, you only roll back steps 1–7, not the whole thing.

5. **I should have spent 10 minutes writing one integration test** that applies the UP against an empty local Postgres container and asserts `information_schema.tables` matches expectations. Static sqlparse checks are not the same as "will it actually apply." Flagged for step 3 in the sequence above.

---

_End of review. Full source: `scripts/probe_output/TASK_STATUS.md`, `scripts/schema_review.md`, `supabase/migrations/20260420_*.sql`._
