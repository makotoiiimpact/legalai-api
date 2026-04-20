"""Pull party rosters for each of Garrett Ogata's 89 RECAP dockets.

Read-only against CourtListener. No Supabase. Saves per-docket JSON to
scripts/probe_output/garrett_parties/<docket_id>.json (gitignored) and
writes a Markdown summary to scripts/probe_output/garrett_parties_summary.md.

Usage:
    python3 scripts/pull_garrett_parties.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

BASE_URL = "https://www.courtlistener.com/api/rest/v4"
RATE_LIMIT_SECONDS = 1.5
USER_AGENT = "LegalAI-IIIMPACT/0.1 (makoto@iiimpact.ai)"

SCRIPT_DIR = Path(__file__).parent
OUTPUT_DIR = SCRIPT_DIR / "probe_output"
RECAP_DIR = OUTPUT_DIR / "garrett_recap"
PARTIES_DIR = OUTPUT_DIR / "garrett_parties"
INDEX_PATH = OUTPUT_DIR / "garrett_recap_index.json"
SUMMARY_PATH = OUTPUT_DIR / "garrett_parties_summary.md"

AUSA_HINTS = [
    "united states attorney",
    "u.s. attorney",
    "us attorney",
    "assistant united states attorney",
    "ausa",
    "department of justice",
    " doj ",
    "dept. of justice",
]


def get_docket_ids() -> list[int]:
    """Prefer the index; fall back to scanning garrett_recap/*.json."""
    if INDEX_PATH.exists():
        try:
            data = json.loads(INDEX_PATH.read_text())
            if isinstance(data, dict) and isinstance(data.get("docket_ids"), list):
                return [int(x) for x in data["docket_ids"]]
        except Exception as exc:
            print(f"  (index parse failed: {exc}; falling back to glob)")

    if not RECAP_DIR.exists():
        return []
    ids: list[int] = []
    for p in sorted(RECAP_DIR.glob("*.json")):
        try:
            ids.append(int(p.stem))
        except ValueError:
            continue
    return ids


def load_court_lookup() -> dict[int, dict[str, str | None]]:
    """Map docket_id -> {court_id, court_name, case_name} from our existing files."""
    lookup: dict[int, dict[str, str | None]] = {}
    if not RECAP_DIR.exists():
        return lookup
    for p in RECAP_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text())
            did = d.get("docket_id")
            if did is not None:
                lookup[int(did)] = {
                    "court_id": d.get("court_id"),
                    "court_name": d.get("court_name"),
                    "case_name": d.get("case_name"),
                }
        except Exception:
            continue
    return lookup


def fetch_parties(docket_id: int, headers: dict[str, str]) -> tuple[int, list[dict[str, Any]], str]:
    """Return (final_status, all_parties_list, error_msg)."""
    all_parties: list[dict[str, Any]] = []
    url: str | None = f"{BASE_URL}/dockets/{docket_id}/parties/"
    params: dict[str, Any] | None = None
    status = 0
    err = ""

    while url:
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:
            err = f"request exception: {exc}"
            break

        status = resp.status_code
        if status != 200:
            err = f"HTTP {status}: {resp.text[:200]}"
            break

        try:
            data = resp.json()
        except ValueError:
            err = f"non-JSON body (first 200 chars): {resp.text[:200]}"
            break

        results = data.get("results") or []
        if isinstance(results, list):
            all_parties.extend(results)

        url = data.get("next")
        params = None
        if url:
            time.sleep(RATE_LIMIT_SECONDS)

    return status, all_parties, err


def attorney_is_ausa(atty: dict[str, Any]) -> bool:
    """Heuristic: check name/contact_raw/roles for DOJ/AUSA markers."""
    blob_parts: list[str] = []
    for key in ("name", "contact_raw", "organizations"):
        val = atty.get(key)
        if val is None:
            continue
        if isinstance(val, list):
            blob_parts.extend(str(x) for x in val)
        else:
            blob_parts.append(str(val))
    # Also scan roles for title/position text
    roles = atty.get("roles")
    if isinstance(roles, list):
        for r in roles:
            if isinstance(r, dict):
                blob_parts.extend(str(v) for v in r.values() if v)
    blob = " ".join(blob_parts).lower()
    return any(h in blob for h in AUSA_HINTS)


def extract_attorney_summary(atty: dict[str, Any]) -> tuple[str, str]:
    """Return (name, first_firm_line). Either may be ''."""
    name = str(atty.get("name") or "").strip()
    firm = ""
    contact = atty.get("contact_raw")
    if isinstance(contact, str) and contact.strip():
        # First non-empty line after the name is usually the firm
        lines = [l.strip() for l in contact.splitlines() if l.strip()]
        # Skip the name itself if it reappears as the first line
        for line in lines:
            if line.lower() != name.lower():
                firm = line
                break
    orgs = atty.get("organizations")
    if not firm and isinstance(orgs, list) and orgs:
        firm = str(orgs[0])
    return name, firm


def build_summary(
    queried: int,
    per_docket_counts: dict[int, int],
    attorney_counter: Counter[tuple[str, str]],
    ausa_counter: Counter[tuple[str, str]],
    court_party_counts: Counter[tuple[str, str]],
    errors: list[tuple[int, str]],
    court_lookup: dict[int, dict[str, str | None]],
) -> str:
    total_parties = sum(per_docket_counts.values())
    dockets_with_parties = sum(1 for n in per_docket_counts.values() if n > 0)

    lines: list[str] = []
    lines.append("# Garrett Ogata — RECAP party rosters\n")
    lines.append(f"_Generated {time.strftime('%Y-%m-%d %H:%M:%S %Z')} — read-only, no DB writes._\n")
    lines.append(f"**Dockets queried:** {queried}")
    lines.append(f"**Dockets returning ≥1 party:** {dockets_with_parties}")
    lines.append(f"**Total party rows collected:** {total_parties}")
    lines.append(f"**Unique attorneys (name + firm):** {len(attorney_counter)}")
    lines.append(f"**Unique AUSAs / DOJ-tagged attorneys:** {len(ausa_counter)}")
    lines.append(f"**Dockets with fetch errors:** {len(errors)}\n")

    lines.append("## Top 10 courts by total parties pulled")
    lines.append("| Court ID | Court | Parties |")
    lines.append("|---|---|---|")
    for (court_id, court_name), n in court_party_counts.most_common(10):
        lines.append(f"| `{court_id}` | {court_name} | {n} |")
    lines.append("")

    lines.append("## Top 100 attorneys by docket frequency")
    lines.append("_Name — firm (from contact_raw first non-name line) — docket count_")
    lines.append("")
    for (name, firm), n in attorney_counter.most_common(100):
        firm_str = f" — {firm}" if firm else ""
        lines.append(f"- {name}{firm_str} — {n}")
    lines.append("")

    lines.append("## AUSAs / DOJ-tagged attorneys (all, capped at 100)")
    if not ausa_counter:
        lines.append("_None surfaced — heuristic checks name/contact_raw/roles for 'U.S. Attorney', 'AUSA', 'DOJ', 'Department of Justice'. If this set looks empty and the RECAP dockets include federal criminal matters, the heuristic probably missed a phrasing._")
    else:
        for (name, firm), n in ausa_counter.most_common(100):
            firm_str = f" — {firm}" if firm else ""
            lines.append(f"- {name}{firm_str} — {n}")
    lines.append("")

    if errors:
        lines.append("## Dockets with fetch errors")
        lines.append("| Docket ID | Court | Error |")
        lines.append("|---|---|---|")
        for did, err in errors:
            court = court_lookup.get(did, {})
            court_str = court.get("court_id") or "?"
            lines.append(f"| {did} | `{court_str}` | {err[:120]} |")
        lines.append("")

    return "\n".join(lines)


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

    PARTIES_DIR.mkdir(parents=True, exist_ok=True)

    docket_ids = get_docket_ids()
    if not docket_ids:
        print("ERROR: no docket IDs found. Need garrett_recap/*.json or an index with docket_ids.", file=sys.stderr)
        return 2

    court_lookup = load_court_lookup()
    print(f"Preparing to fetch parties for {len(docket_ids)} dockets.")

    attorney_counter: Counter[tuple[str, str]] = Counter()
    ausa_counter: Counter[tuple[str, str]] = Counter()
    court_party_counts: Counter[tuple[str, str]] = Counter()
    per_docket_counts: dict[int, int] = {}
    errors: list[tuple[int, str]] = []

    for i, did in enumerate(docket_ids, start=1):
        print(f"[{i}/{len(docket_ids)}] docket {did}", end=" ")
        status, parties, err = fetch_parties(did, headers)
        per_docket_counts[did] = len(parties)

        if err:
            print(f"ERROR: {err[:80]}")
            errors.append((did, err))
            # still persist what we got
            out = {"docket_id": did, "status": status, "error": err, "parties": parties}
            (PARTIES_DIR / f"{did}.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
            time.sleep(RATE_LIMIT_SECONDS)
            continue

        print(f"{len(parties)} parties")

        out = {"docket_id": did, "status": status, "parties": parties}
        (PARTIES_DIR / f"{did}.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

        # Rollup
        court_info = court_lookup.get(did, {})
        court_key = (court_info.get("court_id") or "?", court_info.get("court_name") or "?")
        court_party_counts[court_key] += len(parties)

        for party in parties:
            if not isinstance(party, dict):
                continue
            attorneys = party.get("attorneys")
            if not isinstance(attorneys, list):
                continue
            for atty in attorneys:
                if not isinstance(atty, dict):
                    continue
                name, firm = extract_attorney_summary(atty)
                if not name:
                    continue
                key_tuple = (name, firm)
                attorney_counter[key_tuple] += 1
                if attorney_is_ausa(atty):
                    ausa_counter[key_tuple] += 1

        time.sleep(RATE_LIMIT_SECONDS)

    summary = build_summary(
        queried=len(docket_ids),
        per_docket_counts=per_docket_counts,
        attorney_counter=attorney_counter,
        ausa_counter=ausa_counter,
        court_party_counts=court_party_counts,
        errors=errors,
        court_lookup=court_lookup,
    )
    SUMMARY_PATH.write_text(summary, encoding="utf-8")
    print(f"\nSummary written to: {SUMMARY_PATH}")
    print(f"Per-docket JSONs in: {PARTIES_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
