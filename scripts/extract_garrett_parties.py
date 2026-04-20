"""Extract attorney/firm rosters from Garrett Ogata's RECAP search-page dumps.

Local-only. Reads scripts/probe_output/garrett_recap_page_*.json (which are
gitignored but present on the workstation from the earlier pull). Produces:

  scripts/probe_output/garrett_parties_full.md         — full roster
  scripts/probe_output/garrett_parties_nv_criminal.md  — filtered NV federal criminal view
  scripts/probe_output/garrett_parties_index.json      — machine-readable rollup

NO external network calls. NO Supabase writes.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

OUTPUT_DIR = Path(__file__).parent / "probe_output"
PAGE_GLOB = "garrett_recap_page_*.json"
FULL_MD = OUTPUT_DIR / "garrett_parties_full.md"
NV_MD = OUTPUT_DIR / "garrett_parties_nv_criminal.md"
INDEX_JSON = OUTPUT_DIR / "garrett_parties_index.json"

# --- Heuristics ---------------------------------------------------------------

AUSA_FIRM_HINTS = [
    "united states attorney",
    "u.s. attorney",
    "us attorney",
    "usattorney",
    "department of justice",
    "dept. of justice",
    "us department of justice",
    " doj",
]

DEFENDER_HINTS = [  # exclude from AUSA tagging — these are defense, not prosecution
    "federal public defender",
    "public defender",
    "federal defender",
]

GARRETT_NAME_PATTERNS = [
    re.compile(r"\bgarrett\s+t\.?\s+ogata\b", re.IGNORECASE),
    re.compile(r"\bgarrett\s+ogata\b", re.IGNORECASE),
]
GARRETT_FIRM_PATTERNS = [
    re.compile(r"law offices? of garrett", re.IGNORECASE),
]

# Bankruptcy indicators beyond court_id ending in 'b'
BANKRUPTCY_CASE_NAME_PATTERNS = [
    re.compile(r"^\s*in re[:\s]", re.IGNORECASE),
    re.compile(r"\bchapter\s*(7|11|13|15)\b", re.IGNORECASE),
    re.compile(r"\bv\.?\s+trustee\b", re.IGNORECASE),
]

# NV federal courts (trial + bankruptcy). nvb excluded from criminal view by definition.
NV_FEDERAL_COURTS = {"nvd", "nvb"}
NV_FEDERAL_CRIMINAL_COURTS = {"nvd"}

CRIMINAL_CASE_NAME_PATTERNS = [
    re.compile(r"^\s*united\s+states\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^\s*usa\s+v\.?\s+", re.IGNORECASE),
    re.compile(r"^\s*u\.?s\.?\s+v\.?\s+", re.IGNORECASE),
]


def is_bankruptcy_docket(r: dict[str, Any]) -> bool:
    court_id = (r.get("court_id") or "").lower()
    if court_id.endswith("b"):
        return True
    if r.get("chapter"):
        return True
    case_name = r.get("caseName") or ""
    if any(p.search(case_name) for p in BANKRUPTCY_CASE_NAME_PATTERNS):
        return True
    return False


def is_federal_criminal_docket(r: dict[str, Any]) -> bool:
    """nvd + case name 'United States v. <defendant>' + not a civil case."""
    court_id = (r.get("court_id") or "").lower()
    if court_id not in NV_FEDERAL_CRIMINAL_COURTS:
        return False
    if is_bankruptcy_docket(r):
        return False
    # Civil federal cases have a nature of suit; criminal cases don't.
    if r.get("suitNature"):
        return False
    # Civil cases tagged jurisdiction
    jt = (r.get("jurisdictionType") or "").lower()
    if "plaintiff" in jt or "defendant" in jt:  # e.g. 'U.S. Government Plaintiff' = civil
        return False
    case_name = r.get("caseName") or ""
    if not any(p.search(case_name) for p in CRIMINAL_CASE_NAME_PATTERNS):
        return False
    return True


def firm_is_ausa(firm: str) -> bool:
    lo = firm.lower()
    if any(d in lo for d in DEFENDER_HINTS):
        return False
    return any(h in lo for h in AUSA_FIRM_HINTS)


def name_is_garrett(name: str) -> bool:
    return any(p.search(name) for p in GARRETT_NAME_PATTERNS)


def firm_is_garrett(firm: str) -> bool:
    return any(p.search(firm) for p in GARRETT_FIRM_PATTERNS)


# --- Data loading -------------------------------------------------------------


def load_all_dockets() -> dict[int, dict[str, Any]]:
    """Dedup across paginated pages by docket_id. Last-seen wins (stable)."""
    dockets: dict[int, dict[str, Any]] = {}
    for path in sorted(OUTPUT_DIR.glob(PAGE_GLOB)):
        data = json.loads(path.read_text())
        for r in data.get("results") or []:
            did = r.get("docket_id")
            if did is None:
                continue
            dockets[int(did)] = r
    return dockets


def ensure_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x is not None]
    return [str(v)]


# --- Rollups ------------------------------------------------------------------


def build_rollups(dockets: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Count unique attorneys/firms by the set of DOCKETS they appear on."""
    attorney_dockets: dict[str, set[int]] = defaultdict(set)
    firm_dockets: dict[str, set[int]] = defaultdict(set)
    court_dockets: dict[str, set[int]] = defaultdict(set)
    court_name_map: dict[str, str] = {}
    dockets_with_attorneys = 0
    garrett_counsel_dockets: set[int] = set()
    ausa_firms: Counter[str] = Counter()

    for did, r in dockets.items():
        attys = ensure_list(r.get("attorney"))
        firms = ensure_list(r.get("firm"))
        if attys or firms:
            dockets_with_attorneys += 1 if attys else 0

        for a in attys:
            attorney_dockets[a].add(did)
            if name_is_garrett(a):
                garrett_counsel_dockets.add(did)
        for f in firms:
            firm_dockets[f].add(did)
            if firm_is_garrett(f):
                garrett_counsel_dockets.add(did)
            if firm_is_ausa(f):
                ausa_firms[f] += 1

        court_id = r.get("court_id") or "?"
        court_dockets[court_id].add(did)
        court_name_map[court_id] = r.get("court") or court_id

    return {
        "attorney_dockets": attorney_dockets,
        "firm_dockets": firm_dockets,
        "court_dockets": court_dockets,
        "court_name_map": court_name_map,
        "dockets_with_attorneys": dockets_with_attorneys,
        "garrett_counsel_dockets": sorted(garrett_counsel_dockets),
        "ausa_firms": ausa_firms,
    }


def sort_by_docket_count(d: dict[str, set[int]], top_n: int | None = None) -> list[tuple[str, int, list[int]]]:
    rows = [(key, len(doc_ids), sorted(doc_ids)) for key, doc_ids in d.items()]
    rows.sort(key=lambda x: (-x[1], x[0]))
    return rows[:top_n] if top_n else rows


# --- NV criminal subset -------------------------------------------------------


def build_nv_criminal_view(dockets: dict[int, dict[str, Any]]) -> dict[str, Any]:
    kept: list[dict[str, Any]] = []
    ausa_names: dict[str, set[int]] = defaultdict(set)
    ausa_firms_names: dict[tuple[str, str], set[int]] = defaultdict(set)
    other_counsel: dict[str, set[int]] = defaultdict(set)

    for did, r in dockets.items():
        if not is_federal_criminal_docket(r):
            continue
        attys = ensure_list(r.get("attorney"))
        firms = ensure_list(r.get("firm"))
        has_ausa_firm = any(firm_is_ausa(f) for f in firms)

        kept.append({
            "docket_id": did,
            "court_id": r.get("court_id"),
            "case_name": r.get("caseName"),
            "date_filed": r.get("dateFiled"),
            "date_terminated": r.get("dateTerminated"),
            "assigned_to": r.get("assignedTo"),
            "referred_to": r.get("referredTo"),
            "docket_number": r.get("docketNumber"),
            "attorneys": attys,
            "firms": firms,
            "parties": ensure_list(r.get("party")),
        })

        # Heuristic attorney→AUSA: if the docket has any AUSA-tagged firm, tag
        # attorneys whose name looks DOJ-ish OR just mark the whole-docket AUSA
        # firms since we can't pair them. We'll do both — a name list and a firm list.
        for a in attys:
            if name_is_garrett(a):
                continue
            # If the docket has ANY AUSA firm, we can't say which attorney belongs
            # to it with certainty. Collect ambiguous candidates with a tag.
            other_counsel[a].add(did)

        if has_ausa_firm:
            for f in firms:
                if firm_is_ausa(f):
                    ausa_firms_names[(f, "")].add(did)

    return {
        "dockets": kept,
        "ausa_firms": ausa_firms_names,
        "other_counsel": other_counsel,
    }


# --- Markdown renderers -------------------------------------------------------


def render_full_md(
    dockets: dict[int, dict[str, Any]],
    rollups: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# Garrett Ogata — RECAP party roster (full)\n")
    lines.append(f"_Generated {time.strftime('%Y-%m-%d %H:%M:%S %Z')} — extracted from local search-page dumps. No API calls._\n")
    lines.append(f"**Total unique dockets:** {len(dockets)}")
    lines.append(f"**Dockets with ≥1 attorney:** {rollups['dockets_with_attorneys']}")
    lines.append(f"**Unique attorneys:** {len(rollups['attorney_dockets'])}")
    lines.append(f"**Unique firms:** {len(rollups['firm_dockets'])}")
    lines.append(f"**Dockets where Garrett Ogata is counsel (by name or firm):** {len(rollups['garrett_counsel_dockets'])}")
    lines.append("")

    lines.append("## Courts ranked by docket count")
    lines.append("| Court ID | Court | Dockets |")
    lines.append("|---|---|---|")
    court_rows = sort_by_docket_count(rollups["court_dockets"])
    for court_id, n, _ in court_rows:
        name = rollups["court_name_map"].get(court_id, court_id)
        lines.append(f"| `{court_id}` | {name} | {n} |")
    lines.append("")

    lines.append("## Top 50 attorneys by distinct-docket count")
    lines.append("_Ranking is by number of DISTINCT dockets the attorney appears on, not total mention count._")
    lines.append("")
    lines.append("| # | Attorney | Dockets | Sample docket IDs |")
    lines.append("|---|---|---|---|")
    for i, (name, n, doc_ids) in enumerate(sort_by_docket_count(rollups["attorney_dockets"], 50), 1):
        sample = ", ".join(str(d) for d in doc_ids[:5])
        if len(doc_ids) > 5:
            sample += f", … (+{len(doc_ids) - 5})"
        flag = " 🟢" if name_is_garrett(name) else ""
        lines.append(f"| {i} | {name}{flag} | {n} | {sample} |")
    lines.append("")

    lines.append("## Top 50 firms by distinct-docket count")
    lines.append("| # | Firm | Dockets | AUSA/DOJ? | Garrett's firm? |")
    lines.append("|---|---|---|---|---|")
    for i, (firm, n, _doc_ids) in enumerate(sort_by_docket_count(rollups["firm_dockets"], 50), 1):
        ausa = "✓" if firm_is_ausa(firm) else ""
        gar = "✓" if firm_is_garrett(firm) else ""
        lines.append(f"| {i} | {firm} | {n} | {ausa} | {gar} |")
    lines.append("")

    lines.append("## Garrett Ogata — his own appearances as counsel")
    lines.append(f"**{len(rollups['garrett_counsel_dockets'])} dockets** where his name or firm appears in the attorney/firm roster:")
    lines.append("")
    for did in rollups["garrett_counsel_dockets"]:
        r = dockets.get(did) or {}
        name = (r.get("caseName") or "")[:80]
        court = r.get("court_id") or "?"
        date = r.get("dateFiled") or "?"
        lines.append(f"- `{did}` ({court}, filed {date}) — {name}")
    lines.append("")

    lines.append("## Notes on data quality")
    lines.append("- `attorney` and `firm` arrays in search results are parallel but NOT index-aligned (counts differ per docket). So this rollup counts attorney presence and firm presence separately; pairing an attorney with a specific firm from this source is unreliable.")
    lines.append("- `party` list contains names only — no Plaintiff/Defendant role tagging in the search-result payload.")
    lines.append("- AUSAs are tagged via firm name pattern (`U.S. Attorney`, `Department of Justice`), not via attorney name.")
    return "\n".join(lines)


def render_nv_criminal_md(nv: dict[str, Any]) -> str:
    dockets = nv["dockets"]
    ausa_firms = nv["ausa_firms"]
    other_counsel = nv["other_counsel"]

    lines: list[str] = []
    lines.append("# Garrett Ogata — NV federal criminal docket view\n")
    lines.append(f"_Generated {time.strftime('%Y-%m-%d %H:%M:%S %Z')} — filtered subset of the full roster._\n")
    lines.append("**Filter:** `court_id='nvd'` + case name matches `United States v. …` + no suit-nature tag + no bankruptcy indicators.\n")
    lines.append(f"**Dockets retained:** {len(dockets)}")
    lines.append(f"**Distinct AUSA/DOJ firm records on these dockets:** {len(ausa_firms)}")
    lines.append(f"**Distinct non-Garrett attorneys on these dockets:** {len(other_counsel)}")
    lines.append("")

    lines.append("## Retained dockets")
    for d in sorted(dockets, key=lambda x: (x.get("date_filed") or "")):
        lines.append(f"### `{d['docket_id']}` — {d['case_name']}")
        lines.append(f"**Court:** `{d['court_id']}`   **Docket #:** {d.get('docket_number') or '?'}   **Filed:** {d.get('date_filed') or '?'}   **Terminated:** {d.get('date_terminated') or '?'}")
        lines.append(f"**Judge assigned:** {d.get('assigned_to') or '?'}   **Magistrate referred:** {d.get('referred_to') or '?'}")
        lines.append("")
        if d["parties"]:
            lines.append(f"**Parties ({len(d['parties'])}):** " + ", ".join(d["parties"][:10]) + (" …" if len(d["parties"]) > 10 else ""))
        if d["firms"]:
            lines.append(f"**Firms ({len(d['firms'])}):** " + ", ".join(d["firms"][:10]) + (" …" if len(d["firms"]) > 10 else ""))
        if d["attorneys"]:
            lines.append(f"**Attorneys ({len(d['attorneys'])}):** " + ", ".join(d["attorneys"][:10]) + (" …" if len(d["attorneys"]) > 10 else ""))
        lines.append("")

    lines.append("## AUSA / DOJ firm records across the NV criminal set")
    if not ausa_firms:
        lines.append("_None._")
    else:
        for (firm, _), doc_ids in sorted(ausa_firms.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            lines.append(f"- **{firm}** — on {len(doc_ids)} docket(s): {sorted(doc_ids)}")
    lines.append("")

    lines.append("## Attorneys on NV criminal dockets (excluding Garrett)")
    lines.append("_AUSA-vs-defense can't be determined per-attorney from search results (the two arrays aren't index-aligned). Tag by firm or by cross-referencing docket entries in a followup._")
    lines.append("")
    lines.append("| Attorney | Dockets | Sample docket IDs |")
    lines.append("|---|---|---|")
    for name, doc_ids in sorted(other_counsel.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:100]:
        sample = ", ".join(str(d) for d in sorted(doc_ids)[:5])
        if len(doc_ids) > 5:
            sample += f", … (+{len(doc_ids) - 5})"
        lines.append(f"| {name} | {len(doc_ids)} | {sample} |")
    lines.append("")

    return "\n".join(lines)


# --- Index JSON ---------------------------------------------------------------


def build_index(
    dockets: dict[int, dict[str, Any]],
    rollups: dict[str, Any],
    nv: dict[str, Any],
) -> dict[str, Any]:
    top_attys = [
        {
            "name": name,
            "docket_count": n,
            "dockets": doc_ids,
            "is_garrett": name_is_garrett(name),
        }
        for name, n, doc_ids in sort_by_docket_count(rollups["attorney_dockets"], 50)
    ]
    by_court = {
        court_id: len(doc_ids)
        for court_id, doc_ids in rollups["court_dockets"].items()
    }
    ausa_rows = []
    for (firm, _), doc_ids in nv["ausa_firms"].items():
        ausa_rows.append({"firm": firm, "docket_count": len(doc_ids), "dockets": sorted(doc_ids)})
    ausa_rows.sort(key=lambda x: (-x["docket_count"], x["firm"]))

    nv_federal_dockets = [
        did for did, r in dockets.items()
        if (r.get("court_id") or "").lower() in NV_FEDERAL_COURTS
    ]

    return {
        "total_dockets": len(dockets),
        "dockets_with_attorney_data": rollups["dockets_with_attorneys"],
        "total_unique_attorneys": len(rollups["attorney_dockets"]),
        "total_unique_firms": len(rollups["firm_dockets"]),
        "nv_federal_docket_count": len(nv_federal_dockets),
        "nv_federal_criminal_docket_count": len(nv["dockets"]),
        "by_court": dict(sorted(by_court.items(), key=lambda kv: (-kv[1], kv[0]))),
        "top_attorneys_by_docket_count": top_attys,
        "ausas_identified": ausa_rows,
        "garrett_as_counsel_dockets": rollups["garrett_counsel_dockets"],
    }


def main() -> int:
    dockets = load_all_dockets()
    if not dockets:
        print("ERROR: no dockets loaded — missing garrett_recap_page_*.json files.")
        return 1

    print(f"Loaded {len(dockets)} unique dockets from local pages.")

    rollups = build_rollups(dockets)
    nv = build_nv_criminal_view(dockets)

    FULL_MD.write_text(render_full_md(dockets, rollups), encoding="utf-8")
    NV_MD.write_text(render_nv_criminal_md(nv), encoding="utf-8")
    INDEX_JSON.write_text(json.dumps(build_index(dockets, rollups, nv), indent=2, default=str), encoding="utf-8")

    print(f"Wrote {FULL_MD}")
    print(f"Wrote {NV_MD}")
    print(f"Wrote {INDEX_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
