"""Pull all RECAP dockets for Garrett Ogata from CourtListener.

Read-only. Paginates through /search/?q=Garrett%20Ogata&type=r, extracts the
structured fields we need per docket, and saves one JSON per docket under
scripts/probe_output/garrett_recap/<docket_id>.json. Also writes a Markdown
summary rolling the set up by court, year, judge, and party.

No Supabase. No git. No writes outside scripts/probe_output/.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

BASE_URL = "https://www.courtlistener.com/api/rest/v4"
RATE_LIMIT_SECONDS = 1.5
USER_AGENT = "LegalAI-IIIMPACT/0.1 (makoto@iiimpact.ai)"
OUTPUT_DIR = Path(__file__).parent / "probe_output"
DOCKET_DIR = OUTPUT_DIR / "garrett_recap"
SUMMARY_PATH = OUTPUT_DIR / "garrett_recap_summary.md"

# Patterns that indicate a discipline / bar matter concerning Garrett personally.
SENSITIVE_PATTERNS = [
    re.compile(r"discipline\s+of\s+garrett", re.IGNORECASE),
    re.compile(r"\bin\s+re\s+.{0,40}\bogata\b", re.IGNORECASE),
    re.compile(r"\bbar\s+no\.?\s*7469\b", re.IGNORECASE),
    re.compile(r"disciplinary\s+(board|proceeding|matter)", re.IGNORECASE),
]


def is_sensitive(case_name: str) -> bool:
    if not case_name:
        return False
    return any(p.search(case_name) for p in SENSITIVE_PATTERNS)


def extract_docket(result: dict[str, Any]) -> dict[str, Any]:
    case_name = result.get("caseName") or result.get("case_name_full") or ""
    attorneys = result.get("attorney") or []
    firms = result.get("firm") or []
    parties = result.get("party") or []
    return {
        "docket_id": result.get("docket_id"),
        "case_name": case_name,
        "docket_number": result.get("docketNumber"),
        "pacer_case_id": result.get("pacer_case_id"),
        "court_id": result.get("court_id"),
        "court_name": result.get("court"),
        "court_citation_string": result.get("court_citation_string"),
        "jurisdiction_type": result.get("jurisdictionType"),
        "nature_of_suit": result.get("suitNature"),
        "cause": result.get("cause"),
        "chapter": result.get("chapter"),
        "jury_demand": result.get("juryDemand"),
        "date_filed": result.get("dateFiled"),
        "date_terminated": result.get("dateTerminated"),
        "date_argued": result.get("dateArgued"),
        "assigned_judge_name": result.get("assignedTo"),
        "assigned_judge_id": result.get("assigned_to_id"),
        "referred_judge_name": result.get("referredTo"),
        "referred_judge_id": result.get("referred_to_id"),
        "attorneys": attorneys,
        "attorney_ids": result.get("attorney_id") or [],
        "firms": firms,
        "firm_ids": result.get("firm_id") or [],
        "parties": parties,
        "party_ids": result.get("party_id") or [],
        "trustee_str": result.get("trustee_str"),
        "recap_document_count": len(result.get("recap_documents") or []),
        "docket_absolute_url": result.get("docket_absolute_url"),
        "is_sensitive": is_sensitive(case_name),
        "source": "courtlistener_search_v4_recap",
    }


def paginate_search(headers: dict[str, str]) -> list[dict[str, Any]]:
    all_results: list[dict[str, Any]] = []
    url: str | None = f"{BASE_URL}/search/"
    params: dict[str, Any] | None = {"q": "Garrett Ogata", "type": "r"}
    page = 0

    while url:
        page += 1
        print(f"--- page {page} ---")
        print(f"GET {url}")
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=30)
        except requests.RequestException as exc:
            print(f"  EXCEPTION: {exc}")
            break
        print(f"  Status: {resp.status_code}")
        if resp.status_code != 200:
            print(f"  Body: {resp.text[:500]}")
            break

        data = resp.json()
        results = data.get("results") or []
        print(f"  Results on page: {len(results)}  (running total: {len(all_results) + len(results)} / {data.get('count')})")
        all_results.extend(results)

        # Save each raw page too
        raw_path = OUTPUT_DIR / f"garrett_recap_page_{page}.json"
        raw_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

        url = data.get("next")
        params = None  # `next` is a full URL with cursor embedded
        if url:
            time.sleep(RATE_LIMIT_SECONDS)

    return all_results


def build_summary(dockets: list[dict[str, Any]]) -> str:
    total = len(dockets)
    by_court: Counter[str] = Counter()
    by_year: Counter[str] = Counter()
    judges: Counter[str] = Counter()
    attorneys_all: Counter[str] = Counter()
    firms_all: Counter[str] = Counter()
    parties_all: Counter[str] = Counter()
    dates_filed: list[str] = []
    sensitive_dockets: list[dict[str, Any]] = []

    for d in dockets:
        if d["court_id"]:
            by_court[f"{d['court_id']} — {d['court_name']}"] += 1
        year = (d.get("date_filed") or "")[:4]
        if year:
            by_year[year] += 1
            dates_filed.append(d["date_filed"])
        if d.get("assigned_judge_name"):
            judges[d["assigned_judge_name"]] += 1
        if d.get("referred_judge_name"):
            judges[d["referred_judge_name"] + " (referred)"] += 1
        for a in d.get("attorneys") or []:
            attorneys_all[a] += 1
        for fm in d.get("firms") or []:
            firms_all[fm] += 1
        for p in d.get("parties") or []:
            parties_all[p] += 1
        if d.get("is_sensitive"):
            sensitive_dockets.append(d)

    dates_filed_sorted = sorted([d for d in dates_filed if d])
    earliest = dates_filed_sorted[0] if dates_filed_sorted else "<unknown>"
    latest = dates_filed_sorted[-1] if dates_filed_sorted else "<unknown>"

    lines: list[str] = []
    lines.append("# Garrett Ogata — RECAP docket inventory\n")
    lines.append(f"_Generated {time.strftime('%Y-%m-%d %H:%M:%S %Z')} — read-only, no DB writes._\n")
    lines.append(f"**Total dockets pulled:** {total}")
    lines.append(f"**API reports `count`:** see page 1 raw — see probe_output/garrett_recap_page_1.json")
    lines.append(f"**Date-filed range:** {earliest} → {latest}\n")

    lines.append("## Breakdown by court")
    lines.append("| Court ID | Court name | Dockets |")
    lines.append("|---|---|---|")
    for key, n in by_court.most_common():
        court_id, _, name = key.partition(" — ")
        lines.append(f"| `{court_id}` | {name} | {n} |")
    lines.append("")

    lines.append("## Breakdown by year filed")
    lines.append("| Year | Dockets |")
    lines.append("|---|---|")
    for y, n in sorted(by_year.items()):
        lines.append(f"| {y} | {n} |")
    lines.append("")

    lines.append(f"## Unique judges appearing ({len(judges)})")
    for j, n in judges.most_common():
        lines.append(f"- {j} — {n} docket(s)")
    lines.append("")

    lines.append(f"## Unique attorneys-of-record appearing ({len(attorneys_all)})")
    if not attorneys_all:
        lines.append("_None surfaced in the search-result `attorney` field._ Attorney rosters on RECAP are usually populated via the `/dockets/{id}/` parties endpoint; follow-up probe required for full AUSA/opposing-counsel extraction.")
    else:
        for a, n in attorneys_all.most_common():
            lines.append(f"- {a} — {n} docket(s)")
    lines.append("")

    lines.append(f"## Unique firms ({len(firms_all)})")
    if not firms_all:
        lines.append("_None surfaced._")
    else:
        for fm, n in firms_all.most_common():
            lines.append(f"- {fm} — {n} docket(s)")
    lines.append("")

    lines.append(f"## Top parties appearing ({len(parties_all)} unique, showing top 30)")
    for p, n in parties_all.most_common(30):
        lines.append(f"- {p} — {n} docket(s)")
    lines.append("")

    lines.append(f"## Sensitive / disciplinary dockets flagged ({len(sensitive_dockets)})")
    if not sensitive_dockets:
        lines.append("_No RECAP docket in this set matched the discipline/bar-matter patterns._ ")
        lines.append("(The 2022 Nevada Supreme Court discipline matter lives in `/opinions/`, not RECAP — RECAP is federal PACER only.)")
    else:
        for d in sensitive_dockets:
            lines.append(f"- `{d['docket_id']}` {d['case_name']} — {d['court_id']} — filed {d.get('date_filed')}")
    lines.append("")

    lines.append("## Notes on data coverage")
    lines.append("- RECAP covers federal court filings (PACER). Nevada state criminal/DUI matters are **not** in this set.")
    lines.append("- The `attorney`, `firm`, and `party` fields in search results come from elastic indexing and may be sparse. Full party/attorney roster requires follow-up calls to `/dockets/{id}/` and `/dockets/{id}/parties/`.")
    lines.append("- Many matches are name collisions (other 'Ogata' defendants). Manual review is needed to separate Garrett-as-counsel from Garrett-as-subject or unrelated-Ogata cases.")
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

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DOCKET_DIR.mkdir(parents=True, exist_ok=True)

    raw_results = paginate_search(headers)
    print(f"\nTotal raw search results gathered: {len(raw_results)}")

    extracted: list[dict[str, Any]] = []
    written_files: list[str] = []
    sensitive_count = 0
    for r in raw_results:
        d = extract_docket(r)
        extracted.append(d)
        if d["is_sensitive"]:
            sensitive_count += 1
        did = d.get("docket_id")
        if did is None:
            continue
        out_path = DOCKET_DIR / f"{did}.json"
        out_path.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
        written_files.append(out_path.name)

    print(f"Per-docket JSON files written: {len(written_files)}")
    print(f"Dockets flagged is_sensitive: {sensitive_count}")

    summary_md = build_summary(extracted)
    SUMMARY_PATH.write_text(summary_md, encoding="utf-8")
    print(f"Summary written to: {SUMMARY_PATH}")

    # Programmatic summary for next task step
    consolidated = {
        "total": len(extracted),
        "sensitive_count": sensitive_count,
        "courts": sorted({d["court_id"] for d in extracted if d.get("court_id")}),
        "years": sorted({(d.get("date_filed") or "")[:4] for d in extracted if d.get("date_filed")}),
    }
    (OUTPUT_DIR / "garrett_recap_index.json").write_text(
        json.dumps(consolidated, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
