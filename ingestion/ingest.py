"""
LegalAI — Document Ingestion Pipeline
======================================
Extracts text from PDFs/DOCX/TXT, chunks intelligently,
embeds with OpenAI, stores in Supabase pgvector.

Usage:
  python ingestion/ingest.py --file "police_report.pdf" --case-id GTO-2024-001 --doc-type arrest_report
  python ingestion/ingest.py --file "nrs_484c.pdf" --static
  python ingestion/ingest.py --dir ./sample_docs --case-id GTO-2024-001
"""

import os
import sys
import re
import json
import uuid
import argparse
from pathlib import Path
from datetime import datetime, timezone

import pdfplumber
from docx import Document as DocxDocument
from dotenv import load_dotenv

load_dotenv()

# ─── Lazy clients (no crash if keys absent — allows test/dry-run) ─────────────

def _get_openai():
    from openai import OpenAI
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])

def _get_supabase():
    from supabase import create_client
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

# ─── Config ───────────────────────────────────────────────────────────────────

EMBEDDING_MODEL   = "text-embedding-3-small"
CHUNK_SIZE_WORDS  = 400   # ~500 GPT tokens (word ≈ 1.25 tokens)
CHUNK_OVERLAP     = 50    # word overlap between chunks
EMBED_BATCH_SIZE  = 100

DOC_TYPES = [
    "arrest_report", "breathalyzer", "fst_transcript", "dash_cam",
    "witness_statement", "charging_doc", "client_intake",
    "chain_of_custody", "search_warrant", "lab_report",
    "nrs_statute", "court_rules", "winning_motion", "other"
]

# ─── Tokenizer (no network needed) ────────────────────────────────────────────

def tokenize(text: str) -> list[str]:
    return text.split()

def detokenize(words: list[str]) -> str:
    return " ".join(words)

def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)

# ─── Text Extraction ──────────────────────────────────────────────────────────

def extract_text_from_pdf(file_path: str) -> tuple[str, int]:
    text_parts = []
    with pdfplumber.open(file_path) as pdf:
        page_count = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            page_text = page.extract_text()
            if page_text:
                text_parts.append(f"[PAGE {i+1}]\n{page_text.strip()}")
    return "\n\n".join(text_parts), page_count

def extract_text_from_docx(file_path: str) -> tuple[str, int]:
    doc = DocxDocument(file_path)
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs), 1

def extract_text_from_txt(file_path: str) -> tuple[str, int]:
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return text, 1

def extract_text(file_path: str) -> tuple[str, int]:
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        return extract_text_from_pdf(file_path)
    elif ext in (".docx", ".doc"):
        return extract_text_from_docx(file_path)
    elif ext in (".txt", ".md"):
        return extract_text_from_txt(file_path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")

# ─── Chunking ─────────────────────────────────────────────────────────────────

def chunk_text(text: str, doc_type: str, doc_name: str) -> list[dict]:
    """Sentence-boundary-aware chunking with word overlap."""
    chunks = []
    chunk_index = 0
    current_page = 1
    sentences = re.split(r'(?<=[.!?])\s+', text)
    current_words: list[str] = []
    current_sentences: list[str] = []

    for sentence in sentences:
        page_match = re.match(r'\[PAGE (\d+)\]', sentence)
        if page_match:
            current_page = int(page_match.group(1))
            continue

        sentence_words = tokenize(sentence)

        if len(current_words) + len(sentence_words) > CHUNK_SIZE_WORDS and current_words:
            content = detokenize(current_words).strip()
            if content:
                chunks.append({
                    "content": content,
                    "chunk_index": chunk_index,
                    "token_count": approx_tokens(content),
                    "metadata": {
                        "page": current_page,
                        "doc_type": doc_type,
                        "doc_name": doc_name,
                        "sentence_count": len(current_sentences)
                    }
                })
                chunk_index += 1
            current_words = current_words[-CHUNK_OVERLAP:] + sentence_words
            current_sentences = [sentence]
        else:
            current_words.extend(sentence_words)
            current_sentences.append(sentence)

    if current_words:
        content = detokenize(current_words).strip()
        if content:
            chunks.append({
                "content": content,
                "chunk_index": chunk_index,
                "token_count": approx_tokens(content),
                "metadata": {
                    "page": current_page,
                    "doc_type": doc_type,
                    "doc_name": doc_name,
                    "sentence_count": len(current_sentences)
                }
            })

    return chunks

# ─── Embeddings ───────────────────────────────────────────────────────────────

def embed_chunks(chunks: list[dict]) -> list[dict]:
    """Embed all chunks in batches using OpenAI."""
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
    from rich.console import Console
    console = Console()

    openai_client = _get_openai()
    texts = [c["content"] for c in chunks]

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), BarColumn(), console=console) as progress:
        task = progress.add_task(f"Embedding {len(texts)} chunks...", total=len(texts))
        for i in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[i:i + EMBED_BATCH_SIZE]
            response = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
            for j, emb in enumerate(response.data):
                chunks[i + j]["embedding"] = emb.embedding
            progress.advance(task, len(batch))

    return chunks

# ─── Supabase Storage ─────────────────────────────────────────────────────────

def upsert_document(case_id, file_path, doc_type, raw_text, page_count) -> str:
    supabase = _get_supabase()
    doc_id = str(uuid.uuid4())
    supabase.table("documents").insert({
        "id": doc_id,
        "case_id": case_id,
        "name": Path(file_path).name,
        "doc_type": doc_type,
        "file_size_kb": round(Path(file_path).stat().st_size / 1024),
        "page_count": page_count,
        "raw_text": raw_text,
        "indexed": False,
    }).execute()
    return doc_id

def store_chunks(document_id: str, case_id, chunks: list[dict]) -> int:
    supabase = _get_supabase()
    rows = [{
        "id": str(uuid.uuid4()),
        "document_id": document_id,
        "case_id": case_id,
        "content": c["content"],
        "embedding": c["embedding"],
        "chunk_index": c["chunk_index"],
        "token_count": c["token_count"],
        "metadata": c["metadata"],
    } for c in chunks]

    for i in range(0, len(rows), 50):
        supabase.table("chunks").insert(rows[i:i+50]).execute()

    supabase.table("documents").update({
        "indexed": True,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "chunk_count": len(chunks)
    }).eq("id", document_id).execute()
    return len(rows)

def log_audit(case_id, document_id, chunk_count, file_name):
    supabase = _get_supabase()
    supabase.table("audit_log").insert({
        "case_id": case_id,
        "document_id": document_id,
        "action": "DOCUMENT_INDEXED",
        "actor": "system",
        "actor_name": "Ingestion Pipeline",
        "note": f"Indexed {file_name} — {chunk_count} chunks stored",
        "metadata": {"chunk_count": chunk_count, "file_name": file_name}
    }).execute()

# ─── Main Pipeline ────────────────────────────────────────────────────────────

def ingest(file_path: str, doc_type: str, case_id=None, verbose=True) -> dict:
    """Full ingestion pipeline for a single document."""
    from rich.console import Console
    console = Console()

    file_path = str(Path(file_path).resolve())
    bucket = f"case {case_id}" if case_id else "STATIC KNOWLEDGE BASE"
    if verbose:
        console.print(f"\n[bold]Ingesting:[/bold] {Path(file_path).name}  ({doc_type} → {bucket})")

    raw_text, page_count = extract_text(file_path)
    if verbose: console.print(f"  [green]✓[/green] {len(raw_text):,} chars, {page_count} pages")

    chunks = chunk_text(raw_text, doc_type, Path(file_path).name)
    if verbose: console.print(f"  [green]✓[/green] {len(chunks)} chunks")

    chunks = embed_chunks(chunks)
    if verbose: console.print(f"  [green]✓[/green] Embedded")

    doc_id = upsert_document(case_id, file_path, doc_type, raw_text, page_count)
    chunk_count = store_chunks(doc_id, case_id, chunks)
    log_audit(case_id, doc_id, chunk_count, Path(file_path).name)

    if verbose: console.print(f"  [bold green]✓ Done[/bold green] — {chunk_count} chunks stored (doc {doc_id[:8]}...)")

    return {"document_id": doc_id, "file": Path(file_path).name, "case_id": case_id,
            "doc_type": doc_type, "pages": page_count, "chunks": chunk_count, "chars": len(raw_text)}

def ingest_directory(directory: str, doc_type: str, case_id=None, verbose=True) -> list[dict]:
    from rich.console import Console
    console = Console()
    supported = {".pdf", ".docx", ".doc", ".txt", ".md"}
    files = [f for f in Path(directory).iterdir() if f.suffix.lower() in supported]
    if not files:
        console.print(f"[yellow]No supported files in {directory}[/yellow]")
        return []
    results = []
    for f in files:
        try:
            results.append(ingest(str(f), doc_type, case_id, verbose))
        except Exception as e:
            console.print(f"  [red]✗ {f.name}: {e}[/red]")
    return results

# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LegalAI Ingestion Pipeline")
    parser.add_argument("--file")
    parser.add_argument("--dir")
    parser.add_argument("--case-id")
    parser.add_argument("--doc-type", default="other")
    parser.add_argument("--static", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    case_id = None if args.static else args.case_id
    verbose = not args.quiet

    if args.file:
        result = ingest(args.file, args.doc_type, case_id, verbose)
        print(json.dumps(result, indent=2))
    elif args.dir:
        results = ingest_directory(args.dir, args.doc_type, case_id, verbose)
        print(f"Ingested {len(results)} documents")
    else:
        parser.print_help()
