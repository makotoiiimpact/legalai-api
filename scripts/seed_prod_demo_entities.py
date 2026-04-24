"""Seed demo entities on LegalAI prod for the Carlos Martinez demo flow.

Targets prod only (kapyskpusteokxuaquwo). Refuses to run against any other
Supabase URL and refuses any key whose JWT 'role' claim != 'service_role'.
Default mode is dry-run; pass --execute to actually write.

Seeds 1 court, 1 agency, 3 judges, 4 prosecutors, 1 attorney. Idempotent:
SELECT-then-INSERT keyed on display name (+ parent FK where applicable),
filtered by deleted_at IS NULL on tables that have it.

Usage:
    ./venv/bin/python scripts/seed_prod_demo_entities.py            # dry-run
    ./venv/bin/python scripts/seed_prod_demo_entities.py --execute  # write
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from supabase import Client, create_client

# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

PROD_PROJECT_REF = "kapyskpusteokxuaquwo"
SEED_SOURCE = "legalai_demo_seed"

REPO_ROOT = Path(__file__).parent.parent
load_dotenv(REPO_ROOT / ".env")


def _decode_jwt_payload(token: str) -> dict:
    """Decode JWT middle segment. No network, no signature check."""
    try:
        _, payload_b64, _ = token.split(".")
    except ValueError:
        sys.exit("SUPABASE_PROD_SERVICE_KEY is not a JWT (expected 3 segments).")
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError) as exc:
        sys.exit(f"Could not decode JWT payload: {exc}")


def assert_prod_creds() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_PROD_URL")
    key = os.environ.get("SUPABASE_PROD_SERVICE_KEY")
    if not url or not key:
        sys.exit(
            "Missing SUPABASE_PROD_URL / SUPABASE_PROD_SERVICE_KEY. "
            "Set them in .env — prod-only names so a shared shell can't "
            "accidentally feed dev creds to this script."
        )
    if PROD_PROJECT_REF not in url:
        sys.exit(
            f"Refusing to run: SUPABASE_PROD_URL does not contain "
            f"{PROD_PROJECT_REF!r}: {url}"
        )
    payload = _decode_jwt_payload(key)
    role = payload.get("role")
    if role != "service_role":
        sys.exit(
            f"Refusing to run: SUPABASE_PROD_SERVICE_KEY role is {role!r}, "
            f"expected 'service_role'."
        )
    ref = payload.get("ref")
    if ref and ref != PROD_PROJECT_REF:
        sys.exit(
            f"Refusing to run: SUPABASE_PROD_SERVICE_KEY ref={ref!r}, "
            f"expected {PROD_PROJECT_REF!r}."
        )
    return url, key


# ---------------------------------------------------------------------------
# Snapshot + assertions
# ---------------------------------------------------------------------------

ENTITY_TABLES = ("courts", "agencies", "judges", "prosecutors", "attorneys")

# courts has no deleted_at column — filter only where it exists.
HAS_SOFT_DELETE = {"courts": False, "agencies": True, "judges": True,
                   "prosecutors": True, "attorneys": True}

EXPECTED_POST = {
    "courts": 1,
    "agencies": 1,
    "judges": 3,
    "prosecutors": 4,
    "attorneys": 1,
}


def count_live(db: Client, table: str) -> int:
    q = db.table(table).select("id", count="exact")
    if HAS_SOFT_DELETE[table]:
        q = q.is_("deleted_at", "null")
    return q.execute().count or 0


def snapshot(db: Client) -> dict[str, int]:
    return {t: count_live(db, t) for t in ENTITY_TABLES}


def assert_post_counts(db: Client) -> dict[str, int]:
    actual = snapshot(db)
    errors: list[str] = []
    for table, expected in EXPECTED_POST.items():
        if actual[table] != expected:
            errors.append(f"  {table}: expected {expected}, got {actual[table]}")
    ogata = (
        db.table("attorneys")
          .select("id, bar_number, bar_state, is_firm_member")
          .eq("full_name", "Garrett T. Ogata")
          .is_("deleted_at", "null")
          .limit(1).execute().data
    )
    if not ogata:
        errors.append("  attorneys: Garrett T. Ogata row missing after seed")
    else:
        row = ogata[0]
        if row.get("bar_number") != "7469":
            errors.append(f"  attorneys/Ogata: bar_number={row.get('bar_number')!r}, expected '7469'")
        if row.get("bar_state") != "NV":
            errors.append(f"  attorneys/Ogata: bar_state={row.get('bar_state')!r}, expected 'NV'")
        if row.get("is_firm_member") is not True:
            errors.append(f"  attorneys/Ogata: is_firm_member={row.get('is_firm_member')!r}, expected True")
    if errors:
        sys.exit("Post-run assertion failures:\n" + "\n".join(errors))
    return actual


# ---------------------------------------------------------------------------
# Idempotent upsert primitive
# ---------------------------------------------------------------------------

def _fetch_existing(
    db: Client, table: str, filters: list[tuple[str, str]], soft_delete: bool,
) -> str | None:
    q = db.table(table).select("id")
    for col, val in filters:
        q = q.eq(col, val)
    if soft_delete:
        q = q.is_("deleted_at", "null")
    rows = q.limit(1).execute().data or []
    return rows[0]["id"] if rows else None


def upsert(
    db: Client,
    table: str,
    filters: list[tuple[str, str]],
    payload: dict,
    label: str,
    dry_run: bool,
    status: list[dict],
) -> str | None:
    soft_delete = HAS_SOFT_DELETE[table]
    existing = _fetch_existing(db, table, filters, soft_delete)
    if existing:
        status.append({"table": table, "label": label, "status": "ALREADY_EXISTS", "id": existing})
        print(f"  [skip]    {table:<12} {label:<42} {existing}")
        return existing
    if dry_run:
        status.append({"table": table, "label": label, "status": "PLANNED_INSERT", "id": None})
        print(f"  [plan]    {table:<12} {label:<42} (would insert)")
        return None
    result = db.table(table).insert(payload).execute()
    new_id = result.data[0]["id"]
    status.append({"table": table, "label": label, "status": "CREATED", "id": new_id})
    print(f"  [created] {table:<12} {label:<42} {new_id}")
    return new_id


# ---------------------------------------------------------------------------
# Seed plan
# ---------------------------------------------------------------------------

def run_seed(db: Client, dry_run: bool) -> list[dict]:
    status: list[dict] = []

    # 1. Clark County District Court — note: key column is 'full_name',
    #    not 'name'; spec's mention of 'name' is a typo per schema inspection.
    court_id = upsert(
        db, "courts",
        filters=[("full_name", "Clark County District Court")],
        payload={
            "full_name": "Clark County District Court",
            "jurisdiction": "Clark County, Nevada",
            "jurisdiction_level": "state_trial",
            "external_ids": {"source": SEED_SOURCE},
        },
        label="Clark County District Court",
        dry_run=dry_run, status=status,
    )

    # 2. Clark County DA.
    agency_id = upsert(
        db, "agencies",
        filters=[("name", "Clark County DA")],
        payload={
            "name": "Clark County DA",
            "agency_type": "da_office",
            "jurisdiction": "Clark County, Nevada",
            "external_ids": {"source": SEED_SOURCE},
        },
        label="Clark County DA",
        dry_run=dry_run, status=status,
    )

    # 3. Judges — verified via Clark County elections page (March 2026 roster).
    #    Keyed on (last_name, department, court_id) so a future seed with the
    #    same last_name in a different dept cannot collide.
    judges = [
        {"last_name": "Gall",  "first_name": "Maria",      "department": "9"},
        {"last_name": "Bluth", "first_name": "Jacqueline", "department": "6"},
        {"last_name": "Krall", "first_name": "Nadia",      "department": "4"},
    ]
    for j in judges:
        full_name = f"{j['first_name']} {j['last_name']}"
        filters = [
            ("last_name", j["last_name"]),
            ("department", j["department"]),
        ]
        if court_id:
            filters.append(("court_id", court_id))
        upsert(
            db, "judges",
            filters=filters,
            payload={
                "full_name": full_name,
                "first_name": j["first_name"],
                "last_name": j["last_name"],
                "department": j["department"],
                "court_id": court_id,
                "external_ids": {"source": SEED_SOURCE},
            },
            label=f"{full_name} (Dept. {j['department']})",
            dry_run=dry_run, status=status,
        )

    # 4. Prosecutors — last-name-only per demo spec (first names not in spec,
    #    better to be blank than wrong). Keyed on (last_name, agency_id).
    pros = [
        {"last_name": "Chen"},
        {"last_name": "Rodriguez"},
        {"last_name": "Walsh"},
        {"last_name": "Schwartz"},
    ]
    for p in pros:
        full_name = p["last_name"]
        filters = [("last_name", p["last_name"])]
        if agency_id:
            filters.append(("agency_id", agency_id))
        upsert(
            db, "prosecutors",
            filters=filters,
            payload={
                "full_name": full_name,
                "first_name": None,
                "last_name": p["last_name"],
                "title": "Deputy District Attorney",
                "agency_id": agency_id,
                "active": True,
                "external_ids": {"source": SEED_SOURCE},
            },
            label=f"DDA {p['last_name']}",
            dry_run=dry_run, status=status,
        )

    # 5. Attorney — Garrett T. Ogata, firm member.
    upsert(
        db, "attorneys",
        filters=[("full_name", "Garrett T. Ogata")],
        payload={
            "full_name": "Garrett T. Ogata",
            "first_name": "Garrett",
            "last_name": "Ogata",
            "bar_number": "7469",
            "bar_state": "NV",
            "is_firm_member": True,
            "firm_name": "Law Offices of Garrett T. Ogata",
            "firm_id": None,
            "data_source": "firm_entered",
            "external_ids": {"source": SEED_SOURCE},
        },
        label="Garrett T. Ogata",
        dry_run=dry_run, status=status,
    )

    return status


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def record_audit(db: Client, pre: dict[str, int], post: dict[str, int]) -> None:
    db.table("audit_log").insert({
        "action": "seed_prod_demo_entities",
        "actor": "service_role",
        "actor_name": "seed_prod_demo_entities.py",
        "note": "Seeded Carlos Martinez demo entities on prod (Phase 4).",
        "metadata": {
            "script": "seed_prod_demo_entities.py",
            "project_ref": PROD_PROJECT_REF,
            "pre_counts": pre,
            "post_counts": post,
            "entities_seeded": EXPECTED_POST,
        },
    }).execute()


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def _print_summary(pre: dict[str, int], post: dict[str, int] | None,
                   status: list[dict]) -> None:
    print("\nSummary:")
    print(f"  {'table':<12} {'pre':>5} {'post':>5}  statuses")
    print(f"  {'-' * 12} {'-' * 5} {'-' * 5}  {'-' * 50}")
    by_table: dict[str, list[str]] = {}
    for row in status:
        by_table.setdefault(row["table"], []).append(row["status"])
    for table in ENTITY_TABLES:
        post_val = f"{post[table]:>5}" if post is not None else "    -"
        states = ", ".join(by_table.get(table, ["(none)"]))
        print(f"  {table:<12} {pre[table]:>5} {post_val}  {states}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually perform INSERTs. Default is DRY-RUN (no writes).",
    )
    args = parser.parse_args()
    dry_run = not args.execute

    url, key = assert_prod_creds()
    db = create_client(url, key)
    host = urlparse(url).netloc

    print("=" * 72)
    print(f"Target host:  {host}")
    print(f"Project ref:  {PROD_PROJECT_REF}")
    print(f"Mode:         {'DRY RUN (no writes)' if dry_run else 'EXECUTE (writes enabled)'}")
    print("=" * 72)

    print("\nPre-flight row counts (WHERE deleted_at IS NULL where applicable):")
    pre = snapshot(db)
    for table, count in pre.items():
        print(f"  {table:<12} {count}")

    print("\nSeed plan:")
    print("-" * 72)
    status = run_seed(db, dry_run)
    print("-" * 72)

    if dry_run:
        print("\nDRY RUN complete. Re-run with --execute to actually write.")
        _print_summary(pre, None, status)
        return 0

    print("\nPost-run row counts + assertions:")
    post = assert_post_counts(db)
    for table, count in post.items():
        print(f"  {table:<12} {count}")

    print("\nRecording audit_log entry...")
    record_audit(db, pre, post)
    print("  done.")

    _print_summary(pre, post, status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
