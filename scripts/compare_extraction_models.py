"""
LegalAI Learning Lab — Model Comparison Harness (Script 1 of 5)
================================================================

Runs a single legal document through multiple extraction models and produces
a comparison report (latency, JSON validity, per-field agreement with
Claude-as-ground-truth).

R&D tooling — not wired into production. Self-contained so it works BEFORE
Script 5 refactors `services/extraction.py` into a model-agnostic provider
layer. Do NOT import from `services/` yet.

Usage
-----
    python scripts/compare_extraction_models.py path/to/doc.pdf
    python scripts/compare_extraction_models.py --claude-only   # skip Ollama

Env
---
    ANTHROPIC_API_KEY   required
    OLLAMA_HOST         optional, default http://localhost:11434
                        (pod may route differently)

Output
------
    scripts/output/model_comparison_results.md

Notion spec: https://www.notion.so/34b3764230fa81aa966dd338d99920df
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pdfplumber
import requests
from dotenv import load_dotenv

import anthropic


# ─── Paths ────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE = REPO_ROOT / "test_complaint.pdf"
OUTPUT_DIR = REPO_ROOT / "scripts" / "output"
REPORT_PATH = OUTPUT_DIR / "model_comparison_results.md"

# override=True so values in the repo .env beat a shell that exports empty
# defaults (`ANTHROPIC_API_KEY=""` in zshrc will otherwise shadow the key).
load_dotenv(REPO_ROOT / ".env", override=True)


# ─── Model registry ───────────────────────────────────────────────────────────
#
# Ordering matters — Claude runs first and its output is ground truth for
# scoring every Ollama model. Keep Claude at position [0].
#
# NOTE on Ollama tags: we intentionally do NOT hardcode `qwen3.5:9b` or
# `qwen3.5:27b`. Those tags are suspect — Qwen 3.5 is not on Ollama's public
# registry as of 2026-04-23; what people pull is usually qwen2.5 (the known
# good series). Before adding a tag here, SSH into the pod and confirm with
# `ollama list` what is actually pulled.

MODELS: list[dict[str, str]] = [
    {
        "id": "claude-sonnet-4-5",
        "provider": "anthropic",
        "name": "claude-sonnet-4-5-20250929",
    },
    {
        "id": "qwen2.5:14b",
        "provider": "ollama",
        "name": "qwen2.5:14b",
    },
    {
        "id": "llama3.1:8b",
        "provider": "ollama",
        "name": "llama3.1:8b",
    },
    # TODO(makoto): add a 4th Ollama model here after running `ollama list`
    # on the pod. Example shape:
    #   {"id": "<tag>", "provider": "ollama", "name": "<tag>"},
    # Only add tags that the pod has actually pulled — otherwise the run
    # wastes ~120s on a timeout.
]


# ─── Extraction target ────────────────────────────────────────────────────────

EXTRACTION_FIELDS: tuple[str, ...] = (
    "case_number",
    "judge",
    "prosecutor",
    "defendant",
    "filing_date",
    "charges",
    "court_name",
    "document_type",
)

EXTRACTION_PROMPT = """You are a legal document analyst. Extract structured entity information from the provided court filing (complaint, indictment, information, motion, or similar).

Respond with ONLY a single JSON object. No markdown fences, no commentary, no prose before or after.

JSON schema — every key MUST be present; use null when the field is not found:
{
  "case_number": "string or null",
  "judge": "string or null",
  "prosecutor": "string or null",
  "defendant": "string or null",
  "filing_date": "YYYY-MM-DD or null",
  "charges": ["string", ...],
  "court_name": "string or null",
  "document_type": "string or null"
}

Rules:
- Nevada state case numbers often look like A-21-841234-1 or C-21-123456-1.
- Federal case numbers look like 2:21-cr-00123-ABC.
- "DDA" = Deputy District Attorney; "AUSA" = Assistant U.S. Attorney.
- Judge names typically follow "DEPT.", "DEPARTMENT", "The Honorable", or "JUDGE".
- "charges" is ALWAYS an array. Include one string per count, each describing the charge (include statute citation if present).
- "document_type" is one of: complaint, information, indictment, motion, order, judgment, other.
- "filing_date" must be ISO-8601 (YYYY-MM-DD) or null.

Document text:
---
{DOCUMENT_TEXT}
---
"""


# ─── Result types ─────────────────────────────────────────────────────────────

@dataclass
class ModelResult:
    model_id: str
    provider: str
    model_name: str
    raw_response: str = ""
    parsed: dict[str, Any] | None = None
    latency_s: float | None = None
    error: str | None = None
    token_count: int | None = None  # completion tokens if provider reports


@dataclass
class FieldAgreement:
    field_name: str
    ground_truth: Any
    candidate: Any
    match: bool


@dataclass
class ScoredResult:
    result: ModelResult
    agreements: list[FieldAgreement] = field(default_factory=list)
    agreement_pct: float = 0.0


# ─── Document loading ─────────────────────────────────────────────────────────

def load_document_text(path: Path) -> str:
    """Extract plain text from a .pdf or .txt file.

    Kept inline (does not import from services/) so this script works before
    the Script 5 provider refactor lands.
    """
    if not path.exists():
        raise FileNotFoundError(f"Document not found: {path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        with pdfplumber.open(path) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
        return "\n".join(pages).strip()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8").strip()
    raise ValueError(f"Unsupported file type: {suffix} (expected .pdf or .txt)")


# ─── JSON parsing ─────────────────────────────────────────────────────────────

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_lenient(text: str) -> dict[str, Any] | None:
    """Try hard to extract a JSON object from an LLM response.

    Returns None if no JSON object can be recovered.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


# ─── Provider calls ───────────────────────────────────────────────────────────

CLAUDE_TIMEOUT_S = 120
OLLAMA_TIMEOUT_S = 120


def _build_prompt(document_text: str) -> str:
    return EXTRACTION_PROMPT.replace("{DOCUMENT_TEXT}", document_text)


def _call_claude(document_text: str, model_name: str) -> ModelResult:
    result = ModelResult(
        model_id="claude-sonnet-4-5",
        provider="anthropic",
        model_name=model_name,
    )
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        result.error = "ANTHROPIC_API_KEY not set"
        return result

    client = anthropic.Anthropic(api_key=api_key, timeout=CLAUDE_TIMEOUT_S)
    prompt = _build_prompt(document_text)

    start = time.perf_counter()
    try:
        msg = client.messages.create(
            model=model_name,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        result.latency_s = time.perf_counter() - start
        text_parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        result.raw_response = "".join(text_parts)
        result.token_count = getattr(msg.usage, "output_tokens", None)
        result.parsed = parse_json_lenient(result.raw_response)
        if result.parsed is None:
            result.error = "unparseable JSON"
    except Exception as exc:  # broad by design — R&D harness, never crash the run
        result.latency_s = time.perf_counter() - start
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _call_ollama(document_text: str, model_name: str, host: str) -> ModelResult:
    result = ModelResult(
        model_id=model_name,
        provider="ollama",
        model_name=model_name,
    )
    prompt = _build_prompt(document_text)
    url = host.rstrip("/") + "/api/generate"
    payload = {
        "model": model_name,
        "prompt": prompt,
        "format": "json",  # Ollama's structured-output mode
        "stream": False,
        # Qwen 3.5 ships with thinking mode ON by default — produces a
        # <think>...</think> monologue before the JSON, which blows latency
        # and sometimes breaks structured output. Disable it globally; models
        # that don't support the flag silently ignore it.
        "think": False,
        # Temperature 0.1 — deterministic enough for extraction (Claude
        # ground-truth runs at ~0), non-zero to avoid pathological local
        # minima on Ollama models like llama3.1:8b.
        "options": {"temperature": 0.1},
    }

    start = time.perf_counter()
    try:
        resp = requests.post(url, json=payload, timeout=OLLAMA_TIMEOUT_S)
        result.latency_s = time.perf_counter() - start
        if resp.status_code != 200:
            result.raw_response = resp.text[:2000]
            result.error = f"HTTP {resp.status_code}"
            return result
        body = resp.json()
        response_text = body.get("response", "")
        # Belt-and-suspenders: some Ollama model adapters emit think tags even when think=false.
        response_text = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL)
        result.raw_response = response_text
        result.token_count = body.get("eval_count")
        result.parsed = parse_json_lenient(response_text)
        if result.parsed is None:
            result.error = "unparseable JSON"
    except requests.ConnectionError as exc:
        result.latency_s = time.perf_counter() - start
        result.error = f"ConnectionError: {exc}"
    except requests.Timeout:
        result.latency_s = time.perf_counter() - start
        result.error = f"Timeout after {OLLAMA_TIMEOUT_S}s"
    except Exception as exc:
        result.latency_s = time.perf_counter() - start
        result.error = f"{type(exc).__name__}: {exc}"
    return result


# ─── Scoring ──────────────────────────────────────────────────────────────────

def _normalize_string(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip().lower()
    return " ".join(s.split()) or None


def _normalize_charges(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    out: set[str] = set()
    for item in value:
        if isinstance(item, str):
            norm = _normalize_string(item)
            if norm:
                out.add(norm)
        elif isinstance(item, dict):
            # Tolerate old-schema shape {"description": "...", "statute": "..."}
            parts = [str(item.get(k, "")) for k in ("description", "statute")]
            norm = _normalize_string(" ".join(parts))
            if norm:
                out.add(norm)
    return out


def _fields_agree(field_name: str, truth: Any, cand: Any) -> bool:
    if field_name == "charges":
        return _normalize_charges(truth) == _normalize_charges(cand)
    return _normalize_string(truth) == _normalize_string(cand)


def score_against_truth(
    ground_truth: dict[str, Any] | None,
    candidate: ModelResult,
) -> ScoredResult:
    scored = ScoredResult(result=candidate)
    if ground_truth is None or candidate.parsed is None:
        # Can't score — emit placeholders at 0%.
        for fname in EXTRACTION_FIELDS:
            scored.agreements.append(
                FieldAgreement(
                    field_name=fname,
                    ground_truth=None if ground_truth is None else ground_truth.get(fname),
                    candidate=None if candidate.parsed is None else candidate.parsed.get(fname),
                    match=False,
                )
            )
        scored.agreement_pct = 0.0
        return scored

    hits = 0
    for fname in EXTRACTION_FIELDS:
        truth_val = ground_truth.get(fname)
        cand_val = candidate.parsed.get(fname)
        match = _fields_agree(fname, truth_val, cand_val)
        scored.agreements.append(
            FieldAgreement(
                field_name=fname,
                ground_truth=truth_val,
                candidate=cand_val,
                match=match,
            )
        )
        if match:
            hits += 1
    scored.agreement_pct = 100.0 * hits / len(EXTRACTION_FIELDS)
    return scored


# ─── Report ───────────────────────────────────────────────────────────────────

def _fmt(v: Any) -> str:
    if v is None:
        return "_(null)_"
    if isinstance(v, list):
        return "; ".join(str(x) for x in v) if v else "_(empty)_"
    s = str(v)
    return s.replace("|", "\\|").replace("\n", " ")


def _fmt_latency(s: float | None) -> str:
    return "—" if s is None else f"{s:.2f}s"


def _fmt_pct(p: float) -> str:
    return f"{p:.0f}%"


def build_report(
    doc_path: Path,
    doc_chars: int,
    timestamp: datetime,
    ground_truth_result: ModelResult,
    scored_candidates: list[ScoredResult],
) -> str:
    lines: list[str] = []
    lines.append(f"# Model Comparison — {doc_path.name}")
    lines.append("")
    lines.append(f"- **Run:** {timestamp.isoformat()}")
    lines.append(f"- **Document:** `{doc_path}`")
    lines.append(f"- **Text length:** {doc_chars:,} chars")
    lines.append(f"- **Ground truth model:** `{ground_truth_result.model_name}`")
    lines.append("")

    # Summary table
    lines.append("## Summary")
    lines.append("")
    lines.append("| Model | Latency | JSON valid? | Field agreement | Notes |")
    lines.append("| --- | ---: | :---: | ---: | --- |")

    gt_valid = "✅" if ground_truth_result.parsed is not None else "❌"
    lines.append(
        f"| `{ground_truth_result.model_name}` (ground truth) | "
        f"{_fmt_latency(ground_truth_result.latency_s)} | {gt_valid} | "
        f"— | {_fmt(ground_truth_result.error) if ground_truth_result.error else 'reference'} |"
    )
    for sc in scored_candidates:
        r = sc.result
        valid = "✅" if r.parsed is not None else "❌"
        note = r.error if r.error else ""
        lines.append(
            f"| `{r.model_name}` | {_fmt_latency(r.latency_s)} | {valid} | "
            f"{_fmt_pct(sc.agreement_pct)} | {_fmt(note)} |"
        )
    lines.append("")

    # Per-field diff
    lines.append("## Per-field diff vs ground truth")
    lines.append("")
    header = ["Field", "Ground truth"] + [f"`{sc.result.model_name}`" for sc in scored_candidates]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join("---" for _ in header) + " |")
    gt_parsed = ground_truth_result.parsed or {}
    for fname in EXTRACTION_FIELDS:
        row = [f"`{fname}`", _fmt(gt_parsed.get(fname))]
        for sc in scored_candidates:
            fa = next((a for a in sc.agreements if a.field_name == fname), None)
            if fa is None or sc.result.parsed is None:
                row.append("—")
            else:
                mark = "✅" if fa.match else "❌"
                row.append(f"{mark} {_fmt(fa.candidate)}")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    # Raw responses (collapsed)
    lines.append("## Raw responses")
    lines.append("")
    all_results: list[ModelResult] = [ground_truth_result] + [sc.result for sc in scored_candidates]
    for r in all_results:
        lines.append(f"<details><summary><code>{r.model_name}</code> ({r.provider})</summary>")
        lines.append("")
        lines.append("```")
        lines.append(r.raw_response or "(empty)")
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines) + "\n"


# ─── Orchestration ────────────────────────────────────────────────────────────

def run_comparison(doc_path: Path, claude_only: bool, ollama_host: str) -> str:
    document_text = load_document_text(doc_path)
    if not document_text:
        raise RuntimeError(f"Empty text extracted from {doc_path}")

    claude_cfg = MODELS[0]
    ollama_cfgs = [m for m in MODELS[1:] if m["provider"] == "ollama"]

    print(f"→ Extracting text from {doc_path.name} ({len(document_text):,} chars)", flush=True)

    print(f"→ Running ground-truth model: {claude_cfg['name']}", flush=True)
    claude_result = _call_claude(document_text, claude_cfg["name"])
    if claude_result.error:
        print(f"  ⚠️  Claude error: {claude_result.error}", flush=True)
    else:
        print(f"  ✅ {_fmt_latency(claude_result.latency_s)}", flush=True)

    scored: list[ScoredResult] = []
    if claude_only:
        print("→ --claude-only set, skipping Ollama models", flush=True)
    else:
        for cfg in ollama_cfgs:
            print(f"→ Running {cfg['name']} (ollama @ {ollama_host})", flush=True)
            r = _call_ollama(document_text, cfg["name"], ollama_host)
            if r.error:
                print(f"  ⚠️  {cfg['name']} error: {r.error}", flush=True)
            else:
                print(f"  ✅ {_fmt_latency(r.latency_s)}", flush=True)
            scored.append(score_against_truth(claude_result.parsed, r))

    timestamp = datetime.now(timezone.utc)
    report = build_report(
        doc_path=doc_path,
        doc_chars=len(document_text),
        timestamp=timestamp,
        ground_truth_result=claude_result,
        scored_candidates=scored,
    )
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report, encoding="utf-8")
    print(f"→ Wrote report: {REPORT_PATH}", flush=True)
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare legal-entity extraction across Claude + Ollama models.",
    )
    parser.add_argument(
        "document",
        nargs="?",
        type=Path,
        default=DEFAULT_FIXTURE,
        help=f"Path to a .pdf or .txt document (default: {DEFAULT_FIXTURE.name}).",
    )
    parser.add_argument(
        "--claude-only",
        action="store_true",
        help="Skip all Ollama calls; useful for local smoke testing without a pod.",
    )
    parser.add_argument(
        "--ollama-host",
        default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        help="Ollama base URL (default: $OLLAMA_HOST or http://localhost:11434).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        run_comparison(args.document, args.claude_only, args.ollama_host)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
