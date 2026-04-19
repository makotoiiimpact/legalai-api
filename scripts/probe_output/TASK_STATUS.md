# Autonomous Probe Status — 2026-04-19

_All three tasks complete. Nothing committed. Nothing touched in Supabase._

---

## Completed

### Task 1 — CourtListener filter discovery ✅
- **Script:** `scripts/probe_filter_discovery.py`
- **Raw OPTIONS responses:** `scripts/probe_output/options_{people,courts,positions,dockets,search}.json`
- **Consolidated summary:** `scripts/probe_output/options_summary.json`

**Key findings (filter whitelists):**

| Endpoint | Allowed filters | Gotchas |
|---|---|---|
| `/people/` | `id`, `name_first`, `name_last`, `name_middle`, `name_suffix`, `positions` (FK), `dob_state`, `fjc_id`, `race`, dates | No `__icontains` / `__startswith`. No chained `positions__court__*`. |
| `/courts/` | `id` (exact), `jurisdiction`, `full_name`, `short_name`, `in_use` | Exact match only. |
| `/positions/` | `court` (exact id), `person`, `position_type`, `job_title`, `location_state` | Exact match only. |
| `/dockets/` | `court`, `assigned_to` (judge person_id), `parties`, `docket_number`, `date_filed`, `nature_of_suit` | — |
| `/search/` | OPTIONS empty — elastic-backed, use `q`/`type`/`court`/`case_name` | Undocumented via OPTIONS; test empirically. |

**Takeaway:** To get Nevada judges, go through `/positions/?court=nev` (or `nvd`/`nvb`/etc.) → pivot to `/people/{person_id}/`. Chained filters were the source of every 400 in the earlier probe.

### Task 2 — Garrett Ogata RECAP pull ✅
- **Script:** `scripts/pull_garrett_recap.py`
- **Per-docket JSON:** `scripts/probe_output/garrett_recap/<docket_id>.json` (89 files)
- **Markdown summary:** `scripts/probe_output/garrett_recap_summary.md`
- **Raw pages:** `scripts/probe_output/garrett_recap_page_{1..5}.json`
- **Index:** `scripts/probe_output/garrett_recap_index.json`

**Highlights:**
- **89 dockets** pulled (API count was 88 — one appears via pagination cursor drift; all 89 saved without dedup)
- **Date range:** 1997-06-23 → 2025-08-20
- **Top courts:** `nvd` 22, `deb` 18, `nysb` 8, `cacd` 6
- **Nevada federal footprint:** 24 (22 `nvd` + 2 `nvb`)
- **69 unique judges** across the set (frequent: Brendan L. Shannon 5, Martin Glenn 5, Elayna J. Youchah 6 combined, James C. Mahan 4)
- **is_sensitive flagged:** 0 — as expected. The 2022 NV Supreme Court discipline matter is an opinion, not a RECAP docket.
- **Attorneys / firms fields:** empty from elastic search. Full rosters need follow-up `/dockets/{id}/parties/` enrichment.

**Caveats:**
- Name-collision noise is high (other "Ogata" defendants, "Garrett" as first name). Manual labeling needed to distinguish Garrett-as-counsel from Garrett-as-subject from unrelated cases.
- RECAP = federal PACER only. Nevada state criminal / DUI courts (where Garrett practices) are not here — a different data path (NV Courts public records) is required.

### Task 3 — Behavioral Intelligence schema migration ✅
- **UP migration:** `supabase/migrations/20260420_behavioral_intelligence_layer.sql` (DRAFT)
- **DOWN migration:** `supabase/migrations/20260420_behavioral_intelligence_layer_DOWN.sql` (DRAFT)
- **Self-review:** `scripts/schema_review.md` (design decisions, tradeoffs, 10 open questions)

**What's in it (counts):**
- 24 new tables: 9 core entities, 4 case-relationship tables, 4 event tables, 5 aggregation tables, 2 capture tables
- 2 ALTERs on `cases`: `data_tier`, `firm_id`
- 26 seeded Nevada criminal motion types (`is_seeded=true`)
- RLS enabled on all 22 firm-private tables (permissive policy now, tightening site annotated)
- Indexes on every FK plus Matchup-Card composites like `motions(judge_id, motion_type_id, ruling)`
- Every table uses the shared `set_updated_at()` trigger

**Sanity checks passed:**
- `sqlparse` splits 183 statements cleanly
- All 6 `$$` markers balanced
- 24 `create trigger` / 24 `drop trigger if exists`
- Longest RLS policy name: 30 chars (under 63-char PG identifier limit)
- 26 motion_types seeded (matches design target)

**Design decisions documented in the SQL header:**
1. Soft-delete on sensitive entities, hard-delete on events
2. `raw_docket_text` always preserved on `motions` for re-extraction and audit
3. `confidence_tier` is a stored column, recomputed nightly with n-based thresholds (n≥20 confirmed, n≥5 suggested, n<5 signal, 'inferred' for Tier-0-derived)
4. Every firm-private table is `firm_id uuid` nullable (implicit single-firm mode until a `firms` table is added in a follow-up migration)
5. RLS is on everywhere with the tightening site annotated `-- TIGHTEN PRE-MULTI-TENANT`

---

## Blocked / Questions for Makoto

### Task 1/2 — data-coverage questions
- **Nevada state trial courts are not in CourtListener.** Confirmed. The behavioral intelligence layer must be populated via Tier 1 (AI extraction from Garrett's documents) and Tier 2 (manual capture), not public baseline. The architecture already anticipates this — no schema change needed, but the ingestion roadmap should not assume CL covers state court.
- **RECAP attorney fields are empty.** Elastic search returns `attorney: []`. To get AUSAs and opposing counsel, follow-up probe needs `/dockets/{id}/parties/` or `/attorneys/`. Not in current budget but easy to add.

### Task 3 — schema open questions (full detail in `scripts/schema_review.md`)

> Quick-decision items, listed in priority order for review efficiency:

1. **`cases.id` PK type.** I assumed `uuid`. If it's `bigint`, every FK breaks. Verify before UP: `select data_type from information_schema.columns where table_name='cases' and column_name='id';`
2. **Seed NV courts + judges in this migration, or separate script?** I did neither — schema only. Default recommendation: separate `scripts/seed_courts_judges.py` so the migration stays reversible without data loss.
3. **Create implicit `firms` table + Garrett row now, or wait for multi-tenant migration?** I waited. Confirm.
4. **Nightly vs. real-time confidence recompute.** I assumed nightly. Confirm.
5. **Q5–Q10 in `scripts/schema_review.md`** — pattern_value jsonb shape, observation soft-delete, cross-firm shared-write policy, etc.

---

## Recommended next steps (when you return)

**Immediate (15 min):**
1. Answer Q1 (`cases.id` type). If `bigint`, grep-swap FKs in the UP migration.
2. Skim `scripts/schema_review.md` questions Q2–Q10; mark your choices.
3. Run the UP migration in a **Supabase branch** (not prod) to verify it applies cleanly.
4. If clean, commit and run in prod. Start with `supabase migration new` conventions or just apply via SQL editor.

**Same-day (2 hours):**
5. Write `scripts/seed_courts_judges.py` — pulls Nevada federal courts (`nvd`, `nvb`, `circtdnv`) and associated judges from CL, writes to the new `courts` and `judges` tables with `data_source='courtlistener'` and `data_tier='tier_0_public'`.
6. Write `scripts/seed_garrett_recap.py` — reuses `scripts/probe_output/garrett_recap/*.json` and populates `cases` (if not already there), `motions` (from docket entries), `case_attorneys`, `case_officers`, etc. as Tier 0 baseline.
7. Start the Matchup Card API route using the aggregation tables (will be empty until seeds run — UI can show "not enough data" states).

**This week (phase 1A per Notion):**
8. Build the document-upload → extraction_candidates pipeline (AI does 80%, paralegal confirms 20%)
9. Build the Matchup Card UI component
10. Demo video for Garrett

**Do not skip:** tighten RLS policies BEFORE multi-tenant launch. Every `-- TIGHTEN PRE-MULTI-TENANT` annotation in the UP migration marks a mandatory revision point.

---

## File manifest for this session

```
scripts/
├── probe_filter_discovery.py          (Task 1)
├── pull_garrett_recap.py              (Task 2)
├── schema_review.md                   (Task 3 — self-critique)
└── probe_output/
    ├── TASK_STATUS.md                 (this file)
    ├── options_{people,courts,positions,dockets,search}.json
    ├── options_summary.json
    ├── garrett_recap_page_{1..5}.json
    ├── garrett_recap_summary.md
    ├── garrett_recap_index.json
    └── garrett_recap/
        └── <docket_id>.json           (89 files)

supabase/migrations/
├── 20260420_behavioral_intelligence_layer.sql       (UP, DRAFT)
└── 20260420_behavioral_intelligence_layer_DOWN.sql  (DOWN, DRAFT)
```

Nothing committed. Nothing pushed. Nothing applied to Supabase.
