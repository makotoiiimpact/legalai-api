"""Seed demo entities (Kephart + Chen) for the Carlos Martinez demo flow.

Idempotent — uses full_name as the key and skips when a row already exists.
Also seeds the Clark County District Court + Clark County DA agency rows
that the judge + prosecutor FK into.

Usage:
    ./venv/bin/python scripts/seed_demo_entities.py

Targets dev only. Refuses to run against any Supabase URL that isn't
cfiaxrvtafszmgraftbk.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

load_dotenv(Path(__file__).parent.parent / ".env")

url = os.environ.get("SUPABASE_DEV_URL")
key = os.environ.get("SUPABASE_DEV_SERVICE_KEY")
if not url or not key:
    sys.exit("Missing SUPABASE_DEV_URL / SUPABASE_DEV_SERVICE_KEY in .env")
if "cfiaxrvtafszmgraftbk" not in url:
    sys.exit(f"Refusing to seed non-dev Supabase: {url}")

db = create_client(url, key)

# Tag seeded rows so a later cleanup script can find them. external_ids is
# NOT NULL jsonb on every entity table per ADR-021.
SEED_SOURCE = "legalai_demo_seed"


def upsert(
    table: str,
    full_name_col: str,
    full_name_val: str,
    payload: dict,
    soft_delete: bool = True,
) -> str:
    """Insert if no row with the same display name exists. Entity tables
    (judges/prosecutors/attorneys) have deleted_at; reference tables
    (courts/agencies) don't."""
    q = (
        db.table(table)
          .select(f"id, {full_name_col}")
          .eq(full_name_col, full_name_val)
    )
    if soft_delete:
        q = q.is_("deleted_at", "null")
    existing = q.limit(1).execute().data or []
    if existing:
        rid = existing[0]["id"]
        print(f"  [skip] {table:<13} {full_name_val!r:<40} {rid}")
        return rid
    result = db.table(table).insert(payload).execute()
    rid = result.data[0]["id"]
    print(f"  [new ] {table:<13} {full_name_val!r:<40} {rid}")
    return rid


print(f"Seeding demo entities on {url}")
print("-" * 70)

# 1. Clark County District Court — judges.court_id FK target.
court_id = upsert(
    "courts", "full_name", "Clark County District Court",
    {
        "full_name": "Clark County District Court",
        "jurisdiction": "Clark County, Nevada",
        "external_ids": {"source": SEED_SOURCE},
    },
    soft_delete=False,
)

# 2. Clark County District Attorney — prosecutors.agency_id FK target.
#    agencies table uses 'name' as its display column, not 'full_name'.
agency_id = upsert(
    "agencies", "name", "Clark County District Attorney",
    {
        "name": "Clark County District Attorney",
        "jurisdiction": "Clark County, Nevada",
        "external_ids": {"source": SEED_SOURCE},
    },
    soft_delete=False,
)

# 3. Judge William Kephart — Dept. XIV.
judge_id = upsert("judges", "full_name", "William Kephart", {
    "full_name": "William Kephart",
    "first_name": "William",
    "last_name": "Kephart",
    "department": "XIV",
    "court_id": court_id,
    "data_source": SEED_SOURCE,
    "external_ids": {"source": SEED_SOURCE},
})

# 4. Prosecutor Sarah Chen — Deputy District Attorney.
chen_id = upsert("prosecutors", "full_name", "Sarah Chen", {
    "full_name": "Sarah Chen",
    "first_name": "Sarah",
    "last_name": "Chen",
    "title": "Deputy District Attorney",
    "agency_id": agency_id,
    "active": True,
    "data_source": SEED_SOURCE,
    "external_ids": {"source": SEED_SOURCE},
})

# 5. Prosecutor Michael Rodriguez — DDA on the Davis test case so the
#    second demo case also resolves to a known prosecutor.
rodriguez_id = upsert("prosecutors", "full_name", "Michael Rodriguez", {
    "full_name": "Michael Rodriguez",
    "first_name": "Michael",
    "last_name": "Rodriguez",
    "title": "Deputy District Attorney",
    "agency_id": agency_id,
    "active": True,
    "data_source": SEED_SOURCE,
    "external_ids": {"source": SEED_SOURCE},
})

# 6. Judge Tierra Jones — Dept. IX. Powers drug-possession + second-DUI
#    demo cases so the matchup card shows judge variety.
jones_judge_id = upsert("judges", "full_name", "Tierra Jones", {
    "full_name": "Tierra Jones",
    "first_name": "Tierra",
    "last_name": "Jones",
    "department": "IX",
    "court_id": court_id,
    "data_source": SEED_SOURCE,
    "external_ids": {"source": SEED_SOURCE},
})

# 7. Judge Jerry Wiese — Dept. XXVI. Single-case judge in the demo so the
#    matchup card can show the "Not enough data yet" sparse tier.
wiese_id = upsert("judges", "full_name", "Jerry Wiese", {
    "full_name": "Jerry Wiese",
    "first_name": "Jerry",
    "last_name": "Wiese",
    "department": "XXVI",
    "court_id": court_id,
    "data_source": SEED_SOURCE,
    "external_ids": {"source": SEED_SOURCE},
})

# 8. Prosecutor Jessica Walsh — DDA on the drug-possession case.
walsh_id = upsert("prosecutors", "full_name", "Jessica Walsh", {
    "full_name": "Jessica Walsh",
    "first_name": "Jessica",
    "last_name": "Walsh",
    "title": "Deputy District Attorney",
    "agency_id": agency_id,
    "active": True,
    "data_source": SEED_SOURCE,
    "external_ids": {"source": SEED_SOURCE},
})

# 9. Prosecutor David Schwartz — DDA on the domestic-violence case.
schwartz_id = upsert("prosecutors", "full_name", "David Schwartz", {
    "full_name": "David Schwartz",
    "first_name": "David",
    "last_name": "Schwartz",
    "title": "Deputy District Attorney",
    "agency_id": agency_id,
    "active": True,
    "data_source": SEED_SOURCE,
    "external_ids": {"source": SEED_SOURCE},
})

print("-" * 70)
print("Done.")
print(f"  court_id:       {court_id}")
print(f"  agency_id:      {agency_id}")
print(f"  kephart_id:     {judge_id}")
print(f"  jones_judge_id: {jones_judge_id}")
print(f"  wiese_id:       {wiese_id}")
print(f"  chen_id:        {chen_id}")
print(f"  rodriguez_id:   {rodriguez_id}")
print(f"  walsh_id:       {walsh_id}")
print(f"  schwartz_id:    {schwartz_id}")
