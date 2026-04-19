"""Read-only probe of the CourtListener v4 API.

Writes nothing to Supabase. Saves raw JSON responses under scripts/probe_output/
so we can design the schema against what CL actually returns.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import requests
from dotenv import load_dotenv

BASE_URL = "https://www.courtlistener.com/api/rest/v4"
RATE_LIMIT_SECONDS = 1.5
USER_AGENT = "LegalAI-IIIMPACT/0.1 (makoto@iiimpact.ai)"
OUTPUT_DIR = Path(__file__).parent / "probe_output"


def summarize_result(endpoint: str, result: dict[str, Any]) -> str:
    if endpoint == "search_o":
        name = result.get("caseName") or result.get("case_name") or "<no case name>"
        cid = result.get("cluster_id") or result.get("id")
        court = result.get("court") or result.get("court_id") or ""
        return f"[opinion] {name} (cluster {cid}) — {court}"
    if endpoint == "search_r":
        name = result.get("caseName") or result.get("case_name") or "<no case name>"
        did = result.get("docket_id") or result.get("id")
        court = result.get("court") or result.get("court_id") or ""
        return f"[docket] {name} (docket {did}) — {court}"
    if endpoint == "search_p":
        name = result.get("name") or result.get("name_full") or "<no name>"
        pid = result.get("id")
        return f"[person] {name} (id {pid})"
    if endpoint == "courts":
        return (
            f"[court] {result.get('full_name') or result.get('short_name')} "
            f"(id {result.get('id')}, juris {result.get('jurisdiction')})"
        )
    if endpoint == "people":
        parts = [result.get("name_first"), result.get("name_middle"), result.get("name_last")]
        name = " ".join(p for p in parts if p) or "<no name>"
        return f"[judge] {name} (id {result.get('id')})"
    if endpoint == "person_detail":
        parts = [result.get("name_first"), result.get("name_middle"), result.get("name_last")]
        name = " ".join(p for p in parts if p) or "<no name>"
        pos_count = len(result.get("positions") or [])
        return f"[person_detail] {name} (id {result.get('id')}, {pos_count} positions)"
    if endpoint == "position_detail":
        return (
            f"[position] id {result.get('id')} — court {result.get('court')} "
            f"— position_type {result.get('position_type')}"
        )
    return json.dumps(result)[:200]


def api_get(
    path_or_url: str,
    params: dict[str, Any],
    headers: dict[str, str],
    call_num: str,
    description: str,
    endpoint_kind: str,
    out_file: Path,
) -> dict[str, Any] | None:
    is_absolute = path_or_url.startswith("http")
    target = path_or_url if is_absolute else f"{BASE_URL}{path_or_url}"
    query = urlencode(params, doseq=True) if params else ""
    full_url = f"{target}?{query}" if query else target

    print("============================================")
    print(f"CALL {call_num}: {description}")
    print(f"URL: {full_url}")

    try:
        resp = requests.get(target, params=params, headers=headers, timeout=30)
    except requests.RequestException as exc:
        print(f"Status: EXCEPTION — {exc}")
        print("Count: N/A")
        print("First 3 result summaries: (none — request failed)")
        print("============================================")
        return None

    print(f"Status: {resp.status_code}")

    try:
        data = resp.json()
    except ValueError:
        data = {"_non_json_body": resp.text[:1000]}

    out_file.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    if resp.status_code != 200:
        err = data if isinstance(data, dict) else {"error": str(data)}
        print("Count: N/A")
        print(f"Error body: {json.dumps(err)[:500]}")
        print("============================================")
        return data

    count = data.get("count") if isinstance(data, dict) else None
    print(f"Count: {count if count is not None else 'N/A'}")

    results: list[dict[str, Any]] = []
    if isinstance(data, dict):
        if "results" in data and isinstance(data["results"], list):
            results = data["results"]
        elif endpoint_kind in ("person_detail", "position_detail"):
            results = [data]

    print("First 3 result summaries:")
    if not results:
        print("  (no results)")
    else:
        for i, r in enumerate(results[:3], start=1):
            try:
                print(f"  {i}. {summarize_result(endpoint_kind, r)}")
            except Exception as exc:
                print(f"  {i}. <summary failed: {exc}>")

    print("============================================")
    return data


def main() -> int:
    load_dotenv(".env")
    key = os.getenv("COURTLISTENER_API_KEY")
    if not key:
        print("ERROR: COURTLISTENER_API_KEY not set in .env", file=sys.stderr)
        return 1

    headers = {
        "Authorization": f"Token {key}",
        "User-Agent": USER_AGENT,
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    counts: dict[str, Any] = {
        "opinions": "N/A",
        "recap": "N/A",
        "people": "N/A",
        "nevada_named_courts": "N/A",
        "nev_id_courts": "N/A",
        "nv_supreme_ogata": "N/A",
        "clark_judges": "N/A",
        "nevada_judges": "N/A",
    }

    # CALL 1 — opinions search
    f1 = OUTPUT_DIR / "01_opinions_search.json"
    d1 = api_get(
        "/search/",
        {"q": "Garrett Ogata", "type": "o"},
        headers, "1",
        'Opinion search for "Garrett Ogata"',
        "search_o",
        f1,
    )
    if isinstance(d1, dict) and d1.get("count") is not None:
        counts["opinions"] = d1["count"]
    time.sleep(RATE_LIMIT_SECONDS)

    # CALL 2 — RECAP search
    f2 = OUTPUT_DIR / "02_recap_search.json"
    d2 = api_get(
        "/search/",
        {"q": "Garrett Ogata", "type": "r"},
        headers, "2",
        'RECAP docket search for "Garrett Ogata"',
        "search_r",
        f2,
    )
    if isinstance(d2, dict) and d2.get("count") is not None:
        counts["recap"] = d2["count"]
    time.sleep(RATE_LIMIT_SECONDS)

    # CALL 3 — people search
    f3 = OUTPUT_DIR / "03_people_search.json"
    d3 = api_get(
        "/search/",
        {"q": "Garrett Ogata", "type": "p"},
        headers, "3",
        'People search for "Garrett Ogata"',
        "search_p",
        f3,
    )
    if isinstance(d3, dict) and d3.get("count") is not None:
        counts["people"] = d3["count"]
    time.sleep(RATE_LIMIT_SECONDS)

    # CALL 4 — courts by full_name icontains Nevada
    f4 = OUTPUT_DIR / "04_nevada_courts.json"
    d4 = api_get(
        "/courts/",
        {"full_name__icontains": "Nevada", "page_size": 50},
        headers, "4",
        'Courts with "Nevada" in full_name',
        "courts",
        f4,
    )
    if isinstance(d4, dict) and d4.get("count") is not None:
        counts["nevada_named_courts"] = d4["count"]
    time.sleep(RATE_LIMIT_SECONDS)

    # CALL 5 — courts with id startswith 'nev'
    f5 = OUTPUT_DIR / "05_nev_courts_by_id.json"
    d5 = api_get(
        "/courts/",
        {"id__startswith": "nev", "page_size": 50},
        headers, "5",
        "Courts with id starting with 'nev'",
        "courts",
        f5,
    )
    if isinstance(d5, dict) and d5.get("count") is not None:
        counts["nev_id_courts"] = d5["count"]
    time.sleep(RATE_LIMIT_SECONDS)

    # CALL 6 — NV supreme Ogata search
    f6 = OUTPUT_DIR / "06_nv_supreme_ogata.json"
    d6 = api_get(
        "/search/",
        {"q": "Ogata", "type": "o", "court": "nev"},
        headers, "6",
        'Nevada Supreme Court opinions mentioning "Ogata"',
        "search_o",
        f6,
    )
    if isinstance(d6, dict) and d6.get("count") is not None:
        counts["nv_supreme_ogata"] = d6["count"]
    time.sleep(RATE_LIMIT_SECONDS)

    # CALL 7 — Clark County judges
    f7 = OUTPUT_DIR / "07_clark_county_judges.json"
    d7 = api_get(
        "/people/",
        {
            "name_last__icontains": "",
            "positions__court__full_name__icontains": "Clark County",
        },
        headers, "7",
        "Judges whose position court full_name icontains 'Clark County'",
        "people",
        f7,
    )
    if isinstance(d7, dict) and d7.get("count") is not None:
        counts["clark_judges"] = d7["count"]
    time.sleep(RATE_LIMIT_SECONDS)

    # CALL 8 — judges at courts whose id starts with 'nev'
    f8 = OUTPUT_DIR / "08_nevada_judges.json"
    d8 = api_get(
        "/people/",
        {"positions__court__id__startswith": "nev", "page_size": 20},
        headers, "8",
        "Judges with position at court id starting with 'nev'",
        "people",
        f8,
    )
    if isinstance(d8, dict) and d8.get("count") is not None:
        counts["nevada_judges"] = d8["count"]
    time.sleep(RATE_LIMIT_SECONDS)

    # CALL 9 — sample judge detail + first position (only if CALL 8 had results)
    first_judge: dict[str, Any] | None = None
    if isinstance(d8, dict):
        results = d8.get("results") or []
        if results and isinstance(results[0], dict):
            first_judge = results[0]

    if first_judge is not None:
        judge_id = first_judge.get("id")
        f9 = OUTPUT_DIR / "09_sample_judge_detail.json"
        d9 = api_get(
            f"/people/{judge_id}/",
            {},
            headers, "9",
            f"Sample judge detail for person id {judge_id}",
            "person_detail",
            f9,
        )

        # Find first position URL
        position_url: str | None = None
        if isinstance(d9, dict):
            positions = d9.get("positions") or []
            if positions:
                first_pos = positions[0]
                if isinstance(first_pos, str):
                    position_url = first_pos
                elif isinstance(first_pos, dict):
                    position_url = first_pos.get("resource_uri") or first_pos.get("url")

        if position_url:
            time.sleep(RATE_LIMIT_SECONDS)
            f9b = OUTPUT_DIR / "09b_sample_position.json"
            api_get(
                position_url,
                {},
                headers, "9b",
                f"Sample position detail ({position_url})",
                "position_detail",
                f9b,
            )
        else:
            print("============================================")
            print("CALL 9b: Sample position detail")
            print("URL: (skipped — no position URL on sample judge)")
            print("============================================")
    else:
        print("============================================")
        print("CALL 9: Sample judge detail")
        print("URL: (skipped — no judges returned in CALL 8)")
        print("============================================")

    # Summary
    print()
    print("================================")
    print("PROBE SUMMARY")
    print("================================")
    print(f"Garrett Ogata opinions: {counts['opinions']}")
    print(f"Garrett Ogata RECAP dockets: {counts['recap']}")
    print(f"Garrett Ogata people records: {counts['people']}")
    print(f"Nevada-named courts: {counts['nevada_named_courts']}")
    print(f"Courts with 'nev' ID prefix: {counts['nev_id_courts']}")
    print(f"Ogata in NV Supreme Court: {counts['nv_supreme_ogata']}")
    print(f"Clark County judges: {counts['clark_judges']}")
    print(f"Nevada judges (by court ID): {counts['nevada_judges']}")
    print()
    print("Raw responses saved to: scripts/probe_output/")
    print("================================")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
