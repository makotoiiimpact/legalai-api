"""
LegalAI Learning Lab — Batch Extraction (Script 2 of 5)
========================================================

Runs a single chosen extraction model across a directory of legal documents.
Supports resume (skip docs whose output JSON already exists), bounded
concurrency via asyncio, a warmup ping to preload model weights, and a
per-batch summary CSV.

R&D tooling — not wired into production. Imports shared constants and pure
helpers from scripts/compare_extraction_models.py but duplicates the
provider call functions as async variants, since Script 1's are sync.
TODO(script-5): consolidate the sync and async call paths when the
model-agnostic provider refactor lands in services/extraction.py.

Usage
-----
    python scripts/batch_extract.py \\
        --input-dir <path> \\
        --output-dir <path> \\
        --model <tag> \\
        [--concurrency N] [--limit N] [--ollama-host URL]

Env
---
    ANTHROPIC_API_KEY   required when --model is claude-*

Notion spec: https://www.notion.so/34b3764230fa81aa966dd338d99920df
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import httpx
from dotenv import load_dotenv


# ─── Sibling-script import ────────────────────────────────────────────────────

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS_DIR.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from compare_extraction_models import (  # type: ignore  # noqa: E402
    EXTRACTION_FIELDS,
    EXTRACTION_PROMPT,
    load_document_text,
    parse_json_lenient,
)

load_dotenv(REPO_ROOT / ".env", override=True)


# ─── Config ───────────────────────────────────────────────────────────────────

# Model → provider. Validated at argparse time so an unknown tag aborts
# before we touch the filesystem or the network.
SUPPORTED_MODELS: dict[str, str] = {
    "qwen3.5:9b": "ollama",
    "llama3.1:8b": "ollama",
    "mistral-nemo:12b": "ollama",
    "qwen3:14b": "ollama",
    "claude-sonnet-4-5-20250929": "anthropic",
}

PER_DOC_TIMEOUT_S = 180       # legal docs can be long; 32B responses are slow
WARMUP_TIMEOUT_S = 60         # qwen3:14b cold load is ~11.6s
WARMUP_PROMPT = "Reply with: OK"
WARMUP_MAX_TOKENS = 10

# tqdm is nice but not required
try:
    from tqdm import tqdm  # type: ignore
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False


# ─── Small helpers ────────────────────────────────────────────────────────────

def _iso_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _default_output_dir() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return REPO_ROOT / "scripts" / "output" / f"batch_{ts}"


def _discover_docs(input_dir: Path, limit: int | None) -> list[Path]:
    if not input_dir.exists() or not input_dir.is_dir():
        raise FileNotFoundError(f"Input dir not found or not a directory: {input_dir}")
    docs: list[Path] = []
    for ext in ("*.pdf", "*.txt"):
        docs.extend(input_dir.rglob(ext))
    docs.sort(key=lambda p: str(p.resolve()))
    if limit is not None:
        docs = docs[:limit]
    return docs


def _count_populated(extraction: dict[str, Any] | None) -> int:
    if not extraction:
        return 0
    count = 0
    for fname in EXTRACTION_FIELDS:
        val = extraction.get(fname)
        if fname == "charges":
            if isinstance(val, list) and val:
                count += 1
        else:
            if val not in (None, "", []):
                count += 1
    return count


def _fmt_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


# ─── Async provider calls ────────────────────────────────────────────────────
# Duplicated from Script 1's sync versions; TODO(script-5) consolidate.

async def _call_claude_async(
    client: anthropic.AsyncAnthropic,
    document_text: str,
    model_name: str,
) -> dict[str, Any]:
    prompt = EXTRACTION_PROMPT.replace("{DOCUMENT_TEXT}", document_text)
    start = time.perf_counter()
    try:
        msg = await client.messages.create(
            model=model_name,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        latency = time.perf_counter() - start
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        raw = "".join(parts)
        parsed = parse_json_lenient(raw)
        return {
            "parsed": parsed,
            "raw_response": raw,
            "latency_s": latency,
            "error": None if parsed is not None else "unparseable JSON",
            "token_count": getattr(msg.usage, "output_tokens", None),
        }
    except Exception as exc:
        return {
            "parsed": None,
            "raw_response": "",
            "latency_s": time.perf_counter() - start,
            "error": f"{type(exc).__name__}: {exc}",
            "token_count": None,
        }


async def _call_ollama_async(
    http: httpx.AsyncClient,
    ollama_host: str,
    document_text: str,
    model_name: str,
) -> dict[str, Any]:
    prompt = EXTRACTION_PROMPT.replace("{DOCUMENT_TEXT}", document_text)
    url = ollama_host.rstrip("/") + "/api/generate"
    payload = {
        "model": model_name,
        "prompt": prompt,
        "format": "json",
        "stream": False,
        # Mirror Script 1 flags: thinking off, low temperature.
        "think": False,
        "options": {"temperature": 0.1},
    }
    start = time.perf_counter()
    try:
        resp = await http.post(url, json=payload, timeout=PER_DOC_TIMEOUT_S)
        latency = time.perf_counter() - start
        if resp.status_code != 200:
            return {
                "parsed": None,
                "raw_response": resp.text[:2000],
                "latency_s": latency,
                "error": f"HTTP {resp.status_code}",
                "token_count": None,
            }
        body = resp.json()
        raw = body.get("response", "")
        # Mirror Script 1 a3381e4: strip leaked think tags before parsing.
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        parsed = parse_json_lenient(raw)
        return {
            "parsed": parsed,
            "raw_response": raw,
            "latency_s": latency,
            "error": None if parsed is not None else "unparseable JSON",
            "token_count": body.get("eval_count"),
        }
    except httpx.ConnectError as exc:
        # Bubble up — main() treats this as fatal at warmup and per-doc time.
        raise
    except Exception as exc:
        return {
            "parsed": None,
            "raw_response": "",
            "latency_s": time.perf_counter() - start,
            "error": f"{type(exc).__name__}: {exc}",
            "token_count": None,
        }


# ─── Warmup ──────────────────────────────────────────────────────────────────

async def _warmup(
    *,
    provider: str,
    model_name: str,
    claude_client: anthropic.AsyncAnthropic | None,
    http_client: httpx.AsyncClient | None,
    ollama_host: str,
) -> float:
    """Send a single tiny request so the model is loaded into VRAM.

    Returns latency in seconds. Raises httpx.ConnectError for Ollama when the
    pod isn't reachable — main() treats that as fatal (exit 1).
    """
    print(f"→ Warming up {model_name}...", flush=True)
    start = time.perf_counter()
    if provider == "anthropic":
        assert claude_client is not None
        await claude_client.messages.create(
            model=model_name,
            max_tokens=WARMUP_MAX_TOKENS,
            messages=[{"role": "user", "content": WARMUP_PROMPT}],
        )
    else:
        assert http_client is not None
        url = ollama_host.rstrip("/") + "/api/generate"
        payload = {
            "model": model_name,
            "prompt": WARMUP_PROMPT,
            "stream": False,
            "think": False,
            "options": {"num_predict": WARMUP_MAX_TOKENS, "temperature": 0.1},
        }
        resp = await http_client.post(url, json=payload, timeout=WARMUP_TIMEOUT_S)
        if resp.status_code != 200:
            raise RuntimeError(
                f"warmup HTTP {resp.status_code} from {model_name}: {resp.text[:500]}"
            )
    latency = time.perf_counter() - start
    print(f"  ✅ warmup {latency:.2f}s", flush=True)
    return latency


# ─── Per-document processing ─────────────────────────────────────────────────

async def _process_doc(
    *,
    doc_path: Path,
    output_dir: Path,
    model_name: str,
    provider: str,
    claude_client: anthropic.AsyncAnthropic | None,
    http_client: httpx.AsyncClient | None,
    ollama_host: str,
) -> dict[str, Any]:
    """Returns a summary dict used to build _summary.csv.

    Side effect: writes <output-dir>/<doc-stem>.json on first run.
    On resume (output exists), loads the existing file to populate the
    summary row — no re-extraction.
    """
    out_path = output_dir / f"{doc_path.stem}.json"
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
        return {
            "status": "skipped",
            "document": doc_path.name,
            "model": existing.get("model", model_name),
            "latency_seconds": existing.get("latency_seconds"),
            "extraction_valid": existing.get("extraction") is not None,
            "field_count_populated": _count_populated(existing.get("extraction")),
            "error_count": len(existing.get("errors") or []),
        }

    errors: list[str] = []
    extraction: dict[str, Any] | None = None
    latency_s: float | None = None
    raw_response = ""

    try:
        document_text = load_document_text(doc_path)
    except Exception as exc:
        errors.append(f"text extraction: {type(exc).__name__}: {exc}")
        document_text = ""

    if document_text:
        if provider == "anthropic":
            assert claude_client is not None
            call_result = await _call_claude_async(claude_client, document_text, model_name)
        else:
            assert http_client is not None
            call_result = await _call_ollama_async(
                http_client, ollama_host, document_text, model_name
            )
        extraction = call_result["parsed"]
        latency_s = call_result["latency_s"]
        raw_response = call_result["raw_response"]
        if call_result["error"]:
            errors.append(call_result["error"])
            if extraction is None and raw_response:
                errors.append(f"raw_response: {raw_response[:1500]}")

    record = {
        "document": doc_path.name,
        "model": model_name,
        "timestamp": _iso_utc(),
        "latency_seconds": round(latency_s, 3) if latency_s is not None else None,
        "extraction": extraction,
        "errors": errors,
    }
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "status": "ok" if extraction is not None else "failed",
        "document": doc_path.name,
        "model": model_name,
        "latency_seconds": record["latency_seconds"],
        "extraction_valid": extraction is not None,
        "field_count_populated": _count_populated(extraction),
        "error_count": len(errors),
    }


# ─── Summary CSV ──────────────────────────────────────────────────────────────

SUMMARY_COLUMNS = (
    "document",
    "model",
    "latency_seconds",
    "extraction_valid",
    "field_count_populated",
    "error_count",
)


def _write_summary_csv(summary_path: Path, rows: list[dict[str, Any]]) -> None:
    with summary_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in SUMMARY_COLUMNS})


# ─── Orchestration ────────────────────────────────────────────────────────────

async def run_batch(args: argparse.Namespace) -> int:
    provider = SUPPORTED_MODELS[args.model]
    input_dir = args.input_dir.resolve()
    output_dir = args.output_dir.resolve()

    docs = _discover_docs(input_dir, args.limit)
    if not docs:
        print(f"No .pdf or .txt files found under {input_dir}", file=sys.stderr)
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"→ Found {len(docs)} document(s) in {input_dir}", flush=True)
    print(f"→ Output: {output_dir}", flush=True)
    print(f"→ Model:  {args.model}  (provider: {provider})", flush=True)
    print(f"→ Concurrency: {args.concurrency}", flush=True)
    if args.concurrency > 2 and provider == "ollama":
        print(
            "  ⚠️  Warning: Ollama on single GPU degrades past concurrency 2.",
            flush=True,
        )

    claude_client: anthropic.AsyncAnthropic | None = None
    http_client: httpx.AsyncClient | None = None

    if provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("error: ANTHROPIC_API_KEY not set", file=sys.stderr)
            return 1
        claude_client = anthropic.AsyncAnthropic(api_key=api_key, timeout=PER_DOC_TIMEOUT_S)
    http_client = httpx.AsyncClient(timeout=PER_DOC_TIMEOUT_S)

    try:
        try:
            await _warmup(
                provider=provider,
                model_name=args.model,
                claude_client=claude_client,
                http_client=http_client,
                ollama_host=args.ollama_host,
            )
        except httpx.ConnectError as exc:
            print(
                f"error: Ollama connection refused at {args.ollama_host} ({exc}). "
                "Is the pod up?",
                file=sys.stderr,
            )
            return 1
        except Exception as exc:
            print(f"error: warmup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        sem = asyncio.Semaphore(max(1, args.concurrency))
        completed = 0
        total = len(docs)
        pbar = tqdm(total=total, unit="doc") if HAS_TQDM else None

        async def _one(doc: Path) -> dict[str, Any]:
            nonlocal completed
            async with sem:
                try:
                    row = await _process_doc(
                        doc_path=doc,
                        output_dir=output_dir,
                        model_name=args.model,
                        provider=provider,
                        claude_client=claude_client,
                        http_client=http_client,
                        ollama_host=args.ollama_host,
                    )
                except httpx.ConnectError as exc:
                    # Fatal — bubble up to stop the batch.
                    raise
                except Exception as exc:
                    row = {
                        "status": "failed",
                        "document": doc.name,
                        "model": args.model,
                        "latency_seconds": None,
                        "extraction_valid": False,
                        "field_count_populated": 0,
                        "error_count": 1,
                    }
                completed += 1
                status_label = row.get("status", "?")
                if pbar is not None:
                    pbar.update(1)
                    pbar.set_postfix_str(f"{doc.name} [{status_label}]")
                else:
                    print(f"[{completed}/{total}] {doc.name} — {status_label}", flush=True)
                return row

        t0 = time.perf_counter()
        try:
            rows = await asyncio.gather(*[_one(d) for d in docs])
        except httpx.ConnectError as exc:
            if pbar is not None:
                pbar.close()
            print(
                f"error: Ollama connection refused mid-batch ({exc}). Aborting.",
                file=sys.stderr,
            )
            return 1
        total_elapsed = time.perf_counter() - t0
        if pbar is not None:
            pbar.close()

        summary_path = output_dir / "_summary.csv"
        _write_summary_csv(summary_path, rows)

        succeeded = sum(1 for r in rows if r.get("extraction_valid"))
        failed = total - succeeded
        latencies = [r["latency_seconds"] for r in rows if r.get("latency_seconds") is not None]
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0

        print("")
        print(f"Batch complete: {succeeded}/{total} succeeded, {failed} failed", flush=True)
        print(f"Model: {args.model}", flush=True)
        print(f"Total time: {_fmt_duration(total_elapsed)}", flush=True)
        print(f"Avg latency: {avg_latency:.2f}s", flush=True)
        print(f"Output: {output_dir}", flush=True)
        print(f"Summary: {summary_path}", flush=True)
        return 0
    finally:
        if claude_client is not None:
            await claude_client.close()
        if http_client is not None:
            await http_client.aclose()


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a single extraction model across a directory of documents.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        type=Path,
        help="Directory to scan recursively for .pdf and .txt files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Where to write per-doc JSON + _summary.csv (default: scripts/output/batch_<timestamp>/).",
    )
    parser.add_argument(
        "--model",
        required=True,
        choices=sorted(SUPPORTED_MODELS.keys()),
        help="Extraction model tag.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Bounded async concurrency. Default 1 (serial).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N discovered docs (after alphabetical sort).",
    )
    parser.add_argument(
        "--ollama-host",
        default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        help="Ollama base URL.",
    )
    args = parser.parse_args(argv)
    if args.output_dir is None:
        args.output_dir = _default_output_dir()
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(run_batch(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
