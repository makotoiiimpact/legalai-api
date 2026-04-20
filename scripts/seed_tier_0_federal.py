#!/usr/bin/env python3
"""
Tier 0 Federal Seed Script for LegalAI

- Reads scripts/probe_output/garrett_parties_index.json (+ page dumps for case metadata)
- Writes to legalai-dev Supabase (cfiaxrvtafszmgraftbk) ONLY
- All entity rows idempotent via external_ids @> '{...}'::jsonb check
- All cases tagged data_tier='tier_0_public'
- Dry-run by default. Requires explicit --apply flag to write.

Refs: ADR-017 (attribution), ADR-018 (firm member), ADR-021 (ICP),
      ADR-022 (idempotency)

Deviations from spec — required to match live schema on cfiaxrvtafszmgraftbk:
  1. attorneys column is `full_name` NOT NULL, not `name`.
  2. cases requires `client_name` NOT NULL — derived from caseName in page dumps.
  3. case_attorneys requires `role` NOT NULL — 'defense_lead' for Garrett,
     'prosecution_lead' for AUSAs.

Notes surfaced during pre-flight:
  - Docket 64877115 is court_id='azd' (D. Arizona), NOT 'nvd'. It's an
    ancillary SEC v. Beasley filing in Arizona where Garrett's firm appears
    but his name does not. Still tier_0_public, still firm_level_only.
  - garrett_parties_index.json has NO judge data. Raw page dumps DO (Elayna
    J. Youchah on 3 dockets, Cristina D. Silva on 1). Per spec fallback:
    deferring judge seed to a later Ballotpedia pass.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv(".env")

EXPECTED_PROJECT_ID = "cfiaxrvtafszmgraftbk"

# Attribution map per 2026-04-20 reconciliation
GARRETT_DOCKET_ATTRIBUTION = {
    "6040567":  "attorney_verified",   # US v. Banuelos (nvd cr) — name-matched
    "63143665": "attorney_verified",   # US v. Beasley (nvd mj) — name-matched
    "63232063": "attorney_verified",   # SEC v. Beasley (nvd cv) — name-matched, civil
    "64877115": "firm_level_only",     # SEC v. Beasley (azd mc) — firm-only, ADR-017 case
    "66800213": "attorney_verified",   # US v. Beasley (nvd mj) — name-matched
}

INDEX_JSON = Path("scripts/probe_output/garrett_parties_index.json")
PAGES_GLOB = "scripts/probe_output/garrett_recap_page_*.json"


# ---------------------------------------------------------------- safety ----


def safety_check(url: str) -> None:
    if EXPECTED_PROJECT_ID not in url:
        sys.exit(
            f"SAFETY HALT: SUPABASE_URL does not contain {EXPECTED_PROJECT_ID}. Got: {url}"
        )


# ------------------------------------------------------- input reconstruct --


def load_index() -> dict[str, Any]:
    if not INDEX_JSON.exists():
        sys.exit(f"Missing input: {INDEX_JSON}")
    return json.loads(INDEX_JSON.read_text())


def load_case_metadata_from_pages(docket_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Pull caseName / docketNumber / court_id / parties for requested dockets."""
    wanted = {int(d) for d in docket_ids}
    out: dict[str, dict[str, Any]] = {}
    for path in sorted(glob.glob(PAGES_GLOB)):
        for r in json.loads(Path(path).read_text()).get("results") or []:
            did = r.get("docket_id")
            if did in wanted:
                out[str(did)] = {
                    "caseName": r.get("caseName"),
                    "docketNumber": r.get("docketNumber"),
                    "court_id": r.get("court_id"),
                    "dateFiled": r.get("dateFiled"),
                    "parties": r.get("party") or [],
                }
    return out


def derive_client_name(case_meta: dict[str, Any]) -> str:
    """For 'United States v. X' / 'SEC v. X' etc., return X.
    Fallback: second party after 'USA'/'United States', or first party name.
    """
    cn = case_meta.get("caseName") or ""
    for sep in [" v. ", " v "]:
        if sep in cn:
            return cn.split(sep, 1)[1].strip()
    parties = case_meta.get("parties") or []
    # Prefer non-government-looking party
    for p in parties:
        pl = p.lower()
        if pl not in ("usa", "united states", "united states of america"):
            return p
    return parties[0] if parties else "unknown"


# ----------------------------------------------------------- upsert helper --


def _fmt_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str)


def upsert_by_external_id(
    supabase: Client,
    table: str,
    external_key: str,
    external_value: str,
    insert_payload: dict[str, Any],
    dry_run: bool,
) -> str | None:
    """Idempotent: look up via external_ids @> {key:value}; insert if absent."""
    existing = (
        supabase.table(table)
        .select("id,external_ids")
        .contains("external_ids", {external_key: external_value})
        .execute()
    )
    if existing.data:
        row_id = existing.data[0]["id"]
        print(f"  [SKIP] {table}: exists (id={row_id}) for {external_key}={external_value}")
        return row_id

    if dry_run:
        print(f"  [DRY-RUN INSERT] {table} ({external_key}={external_value}):")
        print(f"    payload: {_fmt_payload(insert_payload)}")
        return None

    result = supabase.table(table).insert(insert_payload).execute()
    row_id = result.data[0]["id"]
    print(f"  [INSERT] {table}: new id={row_id}")
    return row_id


# ------------------------------------------------------------------- seeds --


def seed_garrett(supabase: Client, dry_run: bool) -> str | None:
    print("\n=== Seeding Garrett as firm-member attorney ===")
    payload = {
        "full_name": "Garrett Tanji Ogata",
        "first_name": "Garrett",
        "last_name": "Ogata",
        "is_firm_member": True,
        "bar_number": "7469",
        "bar_state": "NV",
        "external_ids": {
            "nv_bar_id": "7469",
            "courtlistener_name_match_dockets": ["63143665", "66800213", "6040567"],
        },
        "firm_id": None,
    }
    return upsert_by_external_id(supabase, "attorneys", "nv_bar_id", "7469", payload, dry_run)


def seed_usao_agency(supabase: Client, dry_run: bool) -> str | None:
    print("\n=== Seeding US Attorney's Office (D. Nevada) ===")
    payload = {
        "name": "US Attorney's Office, District of Nevada",
        "agency_type": "da_office",
        "jurisdiction": "D. Nevada",
        "external_ids": {"court_id": "nvd"},
        "firm_id": None,
    }
    return upsert_by_external_id(supabase, "agencies", "court_id", "nvd", payload, dry_run)


def seed_ausa_attorneys(
    supabase: Client,
    data: dict[str, Any],
    agency_id: str | None,
    dry_run: bool,
) -> dict[str, str | None]:
    """Returns {firm_name: attorney_id or None}."""
    print("\n=== Seeding AUSA firm-record attorneys (per ADR-017 firm_level_only) ===")
    firm_to_id: dict[str, str | None] = {}
    for ausa in data.get("ausas_identified") or []:
        firm_name = ausa["firm"]
        external_ids_payload = {"courtlistener_firm": firm_name}
        if agency_id is not None:
            external_ids_payload["linked_agency_id"] = agency_id
        payload = {
            "full_name": firm_name,          # name-as-full_name for firm records
            "is_firm_member": False,
            "external_ids": external_ids_payload,
            "firm_id": None,
        }
        aid = upsert_by_external_id(
            supabase, "attorneys", "courtlistener_firm", firm_name, payload, dry_run
        )
        firm_to_id[firm_name] = aid
    return firm_to_id


def seed_cases(
    supabase: Client,
    dockets: list[str],
    case_meta: dict[str, dict[str, Any]],
    dry_run: bool,
) -> dict[str, str | None]:
    print("\n=== Seeding 5 cases (all data_tier='tier_0_public') ===")
    case_ids: dict[str, str | None] = {}
    for docket_id in dockets:
        meta = case_meta.get(docket_id) or {}
        client_name = derive_client_name(meta)
        case_number = f"cl-{docket_id}"
        payload = {
            "case_number": case_number,
            "client_name": client_name,
            "data_tier": "tier_0_public",
            "jurisdiction": meta.get("court_id"),
            "external_ids": {
                "courtlistener_docket_id": docket_id,
                "courtlistener_case_name": meta.get("caseName"),
                "courtlistener_docket_number": meta.get("docketNumber"),
                "courtlistener_court_id": meta.get("court_id"),
            },
            "firm_id": None,
        }
        cid = upsert_by_external_id(
            supabase, "cases", "courtlistener_docket_id", docket_id, payload, dry_run
        )
        case_ids[docket_id] = cid
    return case_ids


def seed_case_attorneys_for_garrett(
    supabase: Client,
    garrett_id: str | None,
    case_ids: dict[str, str | None],
    dry_run: bool,
) -> None:
    print("\n=== Seeding case_attorneys for Garrett (5 rows, mixed attribution) ===")
    for docket_id, case_id in case_ids.items():
        attribution = GARRETT_DOCKET_ATTRIBUTION.get(docket_id, "inferred")
        if dry_run and (garrett_id is None or case_id is None):
            print(
                f"  [DRY-RUN INSERT] case_attorneys: docket={docket_id} -> Garrett "
                f"(role=defense_lead, attribution={attribution}) [case_id/attorney_id resolve on --apply]"
            )
            continue

        existing = (
            supabase.table("case_attorneys")
            .select("id")
            .match({"case_id": case_id, "attorney_id": garrett_id, "role": "defense_lead"})
            .execute()
        )
        if existing.data:
            print(f"  [SKIP] case_attorneys: Garrett already linked to case {case_id} as defense_lead")
            continue

        payload = {
            "case_id": case_id,
            "attorney_id": garrett_id,
            "role": "defense_lead",
            "attribution_confidence": attribution,
        }
        result = supabase.table("case_attorneys").insert(payload).execute()
        print(
            f"  [INSERT] case_attorneys: docket={docket_id} -> Garrett "
            f"(role=defense_lead, attribution={attribution}) id={result.data[0]['id']}"
        )


def seed_case_attorneys_for_ausas(
    supabase: Client,
    ausa_firm_to_id: dict[str, str | None],
    ausa_records: list[dict[str, Any]],
    case_ids: dict[str, str | None],
    dry_run: bool,
) -> None:
    """Link each AUSA firm-record to every Garrett-case that appears in that
    firm's `dockets` list in the index. All firm_level_only per ADR-017.
    """
    print("\n=== Seeding case_attorneys for AUSAs (all firm_level_only, ADR-017) ===")
    seeded_dockets = {str(d) for d in case_ids.keys()}
    total = 0
    for record in ausa_records:
        firm_name = record["firm"]
        firm_dockets = {str(d) for d in record.get("dockets") or []}
        intersect = sorted(firm_dockets & seeded_dockets)
        if not intersect:
            print(f"  (no Garrett-case overlap for firm: {firm_name})")
            continue
        ausa_attorney_id = ausa_firm_to_id.get(firm_name)
        for docket_id in intersect:
            case_id = case_ids.get(docket_id)
            total += 1

            if dry_run and (ausa_attorney_id is None or case_id is None):
                print(
                    f"  [DRY-RUN INSERT] case_attorneys: docket={docket_id} -> "
                    f"AUSA '{firm_name}' (role=prosecution_lead, attribution=firm_level_only)"
                )
                continue

            existing = (
                supabase.table("case_attorneys")
                .select("id")
                .match(
                    {
                        "case_id": case_id,
                        "attorney_id": ausa_attorney_id,
                        "role": "prosecution_lead",
                    }
                )
                .execute()
            )
            if existing.data:
                print(
                    f"  [SKIP] case_attorneys: '{firm_name}' already linked to case {case_id}"
                )
                continue

            payload = {
                "case_id": case_id,
                "attorney_id": ausa_attorney_id,
                "role": "prosecution_lead",
                "attribution_confidence": "firm_level_only",
            }
            result = supabase.table("case_attorneys").insert(payload).execute()
            print(
                f"  [INSERT] case_attorneys: docket={docket_id} -> '{firm_name}' "
                f"(firm_level_only) id={result.data[0]['id']}"
            )
    print(f"  (total AUSA linkage rows planned/inserted: {total})")


# --------------------------------------------------------------------- main --


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write to DB (default: dry-run)")
    args = parser.parse_args()

    dry_run = not args.apply
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Tier 0 Federal Seed ({mode}) ===")

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        sys.exit("ERROR: SUPABASE_URL or SUPABASE_SERVICE_KEY missing from .env")
    safety_check(url)
    print(f"Target: {url}")

    data = load_index()
    garrett_dockets = [str(d) for d in data.get("garrett_as_counsel_dockets") or []]
    print(f"Dockets to seed as cases: {garrett_dockets}")

    case_meta = load_case_metadata_from_pages(garrett_dockets)
    missing = set(garrett_dockets) - set(case_meta.keys())
    if missing:
        print(f"WARNING: no page-dump metadata found for dockets: {sorted(missing)}")

    print("\n--- Case metadata preview ---")
    for did in garrett_dockets:
        m = case_meta.get(did) or {}
        client = derive_client_name(m)
        print(
            f"  {did}: court={m.get('court_id')!r}  "
            f"case_number={m.get('docketNumber')!r}  "
            f"caseName={m.get('caseName')!r}  "
            f"derived_client={client!r}"
        )

    supabase: Client = create_client(url, key)

    garrett_id = seed_garrett(supabase, dry_run)
    agency_id = seed_usao_agency(supabase, dry_run)
    ausa_firm_to_id = seed_ausa_attorneys(
        supabase, data, agency_id, dry_run
    )
    case_ids = seed_cases(supabase, garrett_dockets, case_meta, dry_run)
    seed_case_attorneys_for_garrett(supabase, garrett_id, case_ids, dry_run)
    seed_case_attorneys_for_ausas(
        supabase,
        ausa_firm_to_id,
        data.get("ausas_identified") or [],
        case_ids,
        dry_run,
    )

    print(f"\n=== {mode} COMPLETE ===")
    if dry_run:
        print("\nNOTE: Judge data is NOT seeded in this pass. The index JSON doesn't")
        print("carry judge records, and raw page dumps aren't loaded by this script.")
        print("Judges (Elayna J. Youchah, Cristina D. Silva) to be seeded in a later")
        print("Ballotpedia pass per spec fallback.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
