"""Discover allowed filter and ordering parameters on CourtListener v4 endpoints.

OPTIONS requests return the DRF metadata schema including the `filters` whitelist
and `ordering` whitelist. We save the raw JSON and print a clean summary so future
probes don't waste calls on rejected filter names.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

BASE_URL = "https://www.courtlistener.com/api/rest/v4"
RATE_LIMIT_SECONDS = 1.5
USER_AGENT = "LegalAI-IIIMPACT/0.1 (makoto@iiimpact.ai)"
OUTPUT_DIR = Path(__file__).parent / "probe_output"

ENDPOINTS = [
    ("/people/", "options_people.json"),
    ("/courts/", "options_courts.json"),
    ("/positions/", "options_positions.json"),
    ("/dockets/", "options_dockets.json"),
    ("/search/", "options_search.json"),
]


def options_request(path: str, headers: dict[str, str]) -> tuple[int, dict[str, Any] | str]:
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.options(url, headers=headers, timeout=30)
    except requests.RequestException as exc:
        return 0, f"<exception: {exc}>"
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, resp.text[:2000]


def _extract_filters(data: dict[str, Any]) -> list[str]:
    for key in ("filters", "filter_fields"):
        val = data.get(key)
        if isinstance(val, list):
            return [str(x) for x in val]
        if isinstance(val, dict):
            return sorted(val.keys())
    # DRF `filter_class` style sometimes nests under actions.GET
    actions = data.get("actions", {})
    if isinstance(actions, dict):
        get_action = actions.get("GET", {})
        if isinstance(get_action, dict):
            for key in ("filters", "filter_fields"):
                val = get_action.get(key)
                if isinstance(val, list):
                    return [str(x) for x in val]
                if isinstance(val, dict):
                    return sorted(val.keys())
    return []


def _extract_ordering(data: dict[str, Any]) -> list[str]:
    for key in ("ordering", "ordering_fields"):
        val = data.get(key)
        if isinstance(val, list):
            return [str(x) for x in val]
    actions = data.get("actions", {})
    if isinstance(actions, dict):
        get_action = actions.get("GET", {})
        if isinstance(get_action, dict):
            for key in ("ordering", "ordering_fields"):
                val = get_action.get(key)
                if isinstance(val, list):
                    return [str(x) for x in val]
    return []


def main() -> int:
    load_dotenv(".env")
    key = os.getenv("COURTLISTENER_API_KEY")
    if not key:
        print("ERROR: COURTLISTENER_API_KEY not set in .env", file=sys.stderr)
        return 1

    headers = {
        "Authorization": f"Token {key}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    summary: dict[str, dict[str, Any]] = {}

    for path, filename in ENDPOINTS:
        print("============================================")
        print(f"OPTIONS {BASE_URL}{path}")
        status, data = options_request(path, headers)
        print(f"Status: {status}")

        out_path = OUTPUT_DIR / filename
        if isinstance(data, dict):
            out_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            filters = _extract_filters(data)
            ordering = _extract_ordering(data)
            description = data.get("description") or data.get("name") or ""
            print(f"Description: {str(description)[:120]}")
            print(f"Allowed filters ({len(filters)}):")
            for f in filters:
                print(f"  - {f}")
            print(f"Allowed ordering ({len(ordering)}):")
            for o in ordering:
                print(f"  - {o}")
            summary[path] = {"filters": filters, "ordering": ordering, "raw_keys": sorted(data.keys())}
        else:
            out_path.write_text(json.dumps({"_raw_text_or_error": data}, indent=2), encoding="utf-8")
            print(f"Non-JSON body (first 400 chars): {str(data)[:400]}")
            summary[path] = {"filters": [], "ordering": [], "error": True}

        print("============================================")
        time.sleep(RATE_LIMIT_SECONDS)

    # Also save a consolidated summary for programmatic use later
    (OUTPUT_DIR / "options_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print("\nConsolidated summary saved: scripts/probe_output/options_summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
