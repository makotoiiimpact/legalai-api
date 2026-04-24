"""
LegalAI — Tier 1 extraction pipeline
=====================================
Downloads a PDF from the case-documents bucket, extracts text with
pdfplumber, sends it to Claude for structured entity extraction, matches
extracted entities against existing judges/prosecutors/attorneys, then
writes extraction_candidates rows and updates the cases row (legacy
column mapping — see intake.py file header).

run_extraction takes 2 args (case_id, capture_event_id) to match the
FastAPI BackgroundTask call site in routes/intake.py; storage_path is
looked up internally from capture_events.source_metadata.storage_path.

Per ADR-002 this is Tier 1 (AI-extracted, pending human review). OCR for
scanned PDFs is a separate future pipeline — today we raise ExtractionError
with a recognizable message when text extraction yields < MIN_TEXT_CHARS.
"""

import asyncio
import io
import json
import os
import uuid
from pathlib import Path
from typing import Optional

import anthropic
import pdfplumber
from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv(Path(__file__).parent.parent / ".env")


# ─── Constants ────────────────────────────────────────────────────────────────

CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TEXT_CHARS_FOR_CLAUDE = 100_000
MAX_TEXT_CHARS_FOR_STORAGE = 50_000
MIN_TEXT_CHARS = 50
STORAGE_BUCKET = "case-documents"


class ExtractionError(Exception):
    """Raised when extraction pipeline fails at a known stage (bad PDF,
    scanned image, unparseable Claude response, etc.)."""


EXTRACTION_SYSTEM_PROMPT = """You are a legal document analyst for a criminal defense law firm. You extract structured entity information from case filings (complaints, indictments, informations, motions).

Extract the following entities from the document text. For each entity found, provide:
- The extracted value
- A confidence score (0.0-1.0) based on how clearly the value appears
- The source context (the sentence or phrase where you found it)

Respond with ONLY a JSON object. No markdown, no explanation.

JSON schema:
{
  "case_number": {"value": "string or null", "confidence": 0.0-1.0, "source_context": "string"},
  "court": {"value": "string or null", "confidence": 0.0-1.0, "source_context": "string", "department": "string or null"},
  "case_type": {"value": "string or null", "confidence": 0.0-1.0, "source_context": "string"},
  "judge": {"value": "string or null", "confidence": 0.0-1.0, "source_context": "string", "title": "string or null"},
  "prosecutor": {"value": "string or null", "confidence": 0.0-1.0, "source_context": "string", "title": "string or null"},
  "defense_attorney": {"value": "string or null", "confidence": 0.0-1.0, "source_context": "string", "title": "string or null"},
  "defendant": {"value": "string or null", "confidence": 0.0-1.0, "source_context": "string"},
  "charges": [
    {"description": "string", "statute": "string or null", "confidence": 0.0-1.0, "source_context": "string"}
  ],
  "filed_date": {"value": "YYYY-MM-DD or null", "confidence": 0.0-1.0, "source_context": "string"}
}

Rules:
- If an entity is not found, set value to null and confidence to 0.0.
- Nevada state case numbers often look like A-21-841234-1 or C-21-123456-1.
- "DDA" = Deputy District Attorney. "AUSA" = Assistant U.S. Attorney.
- Judge names often follow "DEPT.", "DEPARTMENT", "The Honorable", or "JUDGE".
- Defense attorney may appear as "Attorney for Defendant", "Counsel for Defense", or "Represented by".
- Charges appear as "COUNT I:", "COUNT II:" or as a list of NRS/USC statutes. Extract ALL charges, not just the first.
- court.department: number appears after "DEPT.", "DEPT. NO.", "DEPARTMENT", or "DIV." and is a Roman numeral (XIV) or Arabic number (14, 7). Extract the identifier as-is.
- case_type: infer from charges + court context. Common values:
    "Criminal — DUI", "Criminal — Drug Possession", "Criminal — Domestic Violence",
    "Criminal — Assault", "Criminal — Theft/Fraud", "Civil", "Family".
    For any filing with NRS 484C charges, return "Criminal — DUI".
- Confidence scoring:
  - 0.9-1.0: name appears in a header/caption with clear role label
  - 0.7-0.89: name appears with contextual role indicators
  - 0.5-0.69: name appears but role is inferred from position/context
  - 0.3-0.49: weak signal — mentioned in body text, role ambiguous
  - 0.0-0.29: very uncertain or not found
"""


# ─── PDF text extraction ──────────────────────────────────────────────────────

def _extract_text_sync(pdf_bytes: bytes) -> str:
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            pages = [p.extract_text() or "" for p in pdf.pages]
    except Exception as e:
        raise ExtractionError(f"PDF parse error: {e}")
    return "\n\n".join(t for t in pages if t.strip()).strip()


async def extract_text_from_pdf(supabase: Client, storage_path: str) -> str:
    """Download PDF from the case-documents bucket and return concatenated text.
    Raises ExtractionError if the download fails or the PDF yields too little
    text (likely a scanned image — future OCR work)."""
    try:
        pdf_bytes = supabase.storage.from_(STORAGE_BUCKET).download(storage_path)
    except Exception as e:
        raise ExtractionError(f"Failed to download {storage_path}: {e}")
    if not pdf_bytes:
        raise ExtractionError(f"Empty file at {storage_path}")

    text = await asyncio.to_thread(_extract_text_sync, pdf_bytes)
    if len(text) < MIN_TEXT_CHARS:
        raise ExtractionError(
            "Document appears to be a scanned image or has no extractable text "
            f"(got {len(text)} chars). OCR pipeline is future work."
        )
    return text


# ─── Claude extraction ────────────────────────────────────────────────────────

def _claude_client():
    # Match agents/analyze.py convention — sync client, lazy init.
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _call_claude_sync(document_text: str) -> dict:
    client = _claude_client()
    truncated = document_text[:MAX_TEXT_CHARS_FOR_CLAUDE]
    try:
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=EXTRACTION_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": f"Extract entities from this case filing:\n\n{truncated}",
            }],
        )
    except anthropic.APIError as e:
        raise ExtractionError(f"Claude API error: {e}")

    response_text = message.content[0].text.strip()
    # Strip markdown fences if Claude wrapped the JSON despite the instruction.
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        response_text = "\n".join(lines).strip()

    try:
        return json.loads(response_text)
    except json.JSONDecodeError as e:
        raise ExtractionError(
            f"Claude returned unparseable JSON: {e}; first 200 chars: {response_text[:200]}"
        )


async def extract_entities_with_claude(document_text: str) -> dict:
    """Blocking anthropic SDK call wrapped in a thread so the BackgroundTask's
    event loop stays responsive if the demo fires multiple uploads."""
    return await asyncio.to_thread(_call_claude_sync, document_text)


# ─── Entity matching ──────────────────────────────────────────────────────────

ENTITY_TABLE_MAP = {
    "judge": "judges",
    "prosecutor": "prosecutors",
    "defense_attorney": "attorneys",
}


def _strip_title_suffix(name: str) -> str:
    # "GARRETT T. OGATA, ESQ." -> "GARRETT T. OGATA"
    return name.strip().split(",")[0].strip()


def match_entity_against_existing(
    supabase: Client,
    field_key: str,
    entity_name: str,
) -> Optional[dict]:
    """Best-effort match against judges/prosecutors/attorneys by full_name
    (exact, case-insensitive) then last_name as a fallback.

    Returns None when the entity type isn't one we match (e.g. defendant) or
    when nothing matches. prior_case_count is a stub 0 for now; real counts
    come from aggregation tables in a later spec (D5)."""
    table = ENTITY_TABLE_MAP.get(field_key)
    if not table or not entity_name:
        return None

    cleaned = _strip_title_suffix(entity_name)
    if not cleaned:
        return None

    select_cols = "id, full_name, last_name"
    if table == "attorneys":
        select_cols += ", is_firm_member"

    try:
        rows = (
            supabase.table(table)
            .select(select_cols)
            .ilike("full_name", cleaned)
            .is_("deleted_at", "null")
            .limit(5)
            .execute()
            .data or []
        )
    except Exception as e:
        print(f"[match_entity] exact-match query on {table} failed: {e}")
        return None

    if not rows:
        last_name = cleaned.split()[-1] if cleaned else ""
        if len(last_name) < 2:
            return None
        try:
            rows = (
                supabase.table(table)
                .select(select_cols)
                .ilike("last_name", last_name)
                .is_("deleted_at", "null")
                .limit(5)
                .execute()
                .data or []
            )
        except Exception as e:
            print(f"[match_entity] last-name query on {table} failed: {e}")
            return None

    if not rows:
        return None

    primary = rows[0]
    alternatives = [
        {"entity_id": r["id"], "name": r.get("full_name"), "prior_cases": 0}
        for r in rows[1:]
    ]
    return {
        "matched_entity_id": primary["id"],
        "matched_entity_type": table,
        "matched_entity_name": primary.get("full_name"),
        "is_firm_member": bool(primary.get("is_firm_member")) if table == "attorneys" else False,
        "prior_case_count": 0,
        "alternatives": alternatives,
    }


# ─── Supabase client ──────────────────────────────────────────────────────────

def _get_dev_db() -> Client:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError(
            "Missing SUPABASE_URL / SUPABASE_SERVICE_KEY — see .env.example"
        )
    return create_client(url, key)


# ─── Candidate + case writers ────────────────────────────────────────────────

# Per v1 schema the candidate_type column is text (not enum), but intake.py's
# CANDIDATE_TYPE_TO_ROLE mapping only knows these keys. Defendant has no
# dedicated candidate_type value, so we use "other" like the stub did.
FIELD_TO_CANDIDATE_TYPE = {
    "judge": "judge",
    "prosecutor": "prosecutor",
    "defense_attorney": "attorney",
    "defendant": "other",
}


def _insert_entity_candidates(db: Client, capture_event_id: str, extracted: dict) -> int:
    count = 0
    for field_key, candidate_type in FIELD_TO_CANDIDATE_TYPE.items():
        entity = extracted.get(field_key) or {}
        name = entity.get("value")
        if not name:
            continue

        match = match_entity_against_existing(db, field_key, name)

        payload = {
            "name": name,
            "role": field_key,  # matches the EntityRole union on the frontend
            "source_context": entity.get("source_context"),
        }
        if entity.get("title"):
            payload["title"] = entity["title"]

        row = {
            "id": str(uuid.uuid4()),
            "capture_event_id": capture_event_id,
            "firm_id": None,
            "candidate_type": candidate_type,
            "proposed_payload": payload,
            "confidence_score": float(entity.get("confidence", 0) or 0),
            "status": "pending",
            "review_status": "pending",
        }

        if match:
            row["matched_entity_id"] = match["matched_entity_id"]
            row["matched_entity_type"] = match["matched_entity_type"]
            row["alternative_matches"] = match.get("alternatives") or []
            payload["matched_entity_name"] = match.get("matched_entity_name")
            payload["matched_prior_cases"] = match.get("prior_case_count", 0)
            if match.get("is_firm_member"):
                # Firm-member match auto-confirms per build_entity_candidate's
                # match_status = "auto_confirmed" branch in intake.py.
                payload["is_firm_member"] = True
                row["review_status"] = "confirmed"

        db.table("extraction_candidates").insert(row).execute()
        count += 1
    return count


def _update_case_from_extracted(db: Client, case_id: str, extracted: dict):
    """Map extracted values onto the v1 legacy columns. When the schema
    alignment migration lands (see CLAUDE.md 'Known stubs') this mapping
    should move to proper columns (case_name, court, filed_date, etc.)."""
    updates = {}

    cn = (extracted.get("case_number") or {}).get("value")
    if cn:
        updates["case_number"] = cn

    court_obj = extracted.get("court") or {}
    court = court_obj.get("value")
    if court:
        # v1 cases has no court_dept column (schema-alignment migration
        # pending). Fold "Dept. XIV" into jurisdiction text so the Review /
        # Confirmed Case views can render it without a frontend change.
        dept = (court_obj.get("department") or "").strip()
        updates["jurisdiction"] = f"{court}, Dept. {dept}" if dept else court

    filed = (extracted.get("filed_date") or {}).get("value")
    if filed:
        updates["incident_date"] = filed

    ctype = (extracted.get("case_type") or {}).get("value")
    if ctype:
        updates["case_type"] = ctype

    defendant = (extracted.get("defendant") or {}).get("value")
    if defendant:
        # client_name is NOT NULL on cases; frontend renders it as caseName.
        updates["client_name"] = defendant

    charges = extracted.get("charges") or []
    if charges:
        # v1 cases.charge is a single text column. Concatenate all counts so
        # we don't silently drop count II/III. Proper charges[] normalization
        # is part of the schema-alignment migration.
        parts = []
        for i, c in enumerate(charges, start=1):
            desc = (c.get("description") or "").strip()
            if not desc:
                continue
            statute = (c.get("statute") or "").strip()
            label = f"COUNT {i}: " if len(charges) > 1 else ""
            piece = f"{label}{desc}"
            if statute:
                piece += f" ({statute})"
            parts.append(piece)
        if parts:
            updates["charge"] = "; ".join(parts)

    if not updates:
        return
    try:
        db.table("cases").update(updates).eq("id", case_id).execute()
    except Exception as e:
        print(f"[run_extraction] cases update failed for {case_id}: {e}")


def _mark_capture_error(db: Client, case_id: str, capture_event_id: str, msg: str):
    """Persist the failure on both the capture_event (processing_error + status)
    AND the parent case (review_status='error'). Without the cases update the
    UI polls /cases/{id}/extraction forever — that endpoint derives state
    from cases.review_status, not capture_events.status.

    Requires migration 20260423_002_case_review_status_error.sql to have
    added 'error' to the case_review_status enum on the target project."""
    try:
        db.table("capture_events").update({
            "status": "error",
            "processing_error": msg[:500],
        }).eq("id", capture_event_id).execute()
    except Exception as e:
        print(f"[run_extraction] failed to mark capture_event error on {capture_event_id}: {e}")
    try:
        db.table("cases").update({
            "review_status": "error",
        }).eq("id", case_id).execute()
    except Exception as e:
        print(f"[run_extraction] failed to mark case review_status=error on {case_id}: {e}")


# ─── Orchestration entry point ───────────────────────────────────────────────

async def run_extraction(case_id: str, capture_event_id: str) -> dict | None:
    """Background task. 2-arg signature preserved so the call site in
    routes/intake.py only swaps the function name.

    Flow:
      1. Read capture_events.source_metadata.storage_path.
      2. capture_events.status: received → extracting
      3. cases.review_status: processing (idempotent; upload sets this already)
      4. Download PDF, extract text, cache first 50K chars on raw_payload.
      5. Claude entity extraction.
      6. Insert extraction_candidates (judge/prosecutor/attorney/defendant).
      7. Update cases legacy columns from extracted fields + charges.
      8. capture_events.status → awaiting_review; cases.review_status → needs_review.

    capture_events.status values are constrained by capture_events_status_check
    to: received, extracting, awaiting_review, confirmed, rejected, error.
    cases.review_status uses its own separate case_review_status enum, which
    is why the two status fields use different vocabularies.

    On ExtractionError or unexpected error: capture_events.status → 'error'
    with processing_error populated, AND cases.review_status → 'error'. The
    case-level flag is what GET /cases/{id}/extraction reads to return
    state:"error" to the frontend. Requires migration 20260423_002 for the
    enum value."""
    db = _get_dev_db()

    try:
        cap_rows = (
            db.table("capture_events")
            .select("source_metadata, status")
            .eq("id", capture_event_id)
            .execute()
            .data or []
        )
    except Exception as e:
        print(f"[run_extraction] failed to read capture_event {capture_event_id}: {e}")
        return

    if not cap_rows:
        print(f"[run_extraction] capture_event {capture_event_id} not found")
        return

    storage_path = (cap_rows[0].get("source_metadata") or {}).get("storage_path")
    if not storage_path:
        _mark_capture_error(db, case_id, capture_event_id, "No storage_path in source_metadata")
        return

    try:
        db.table("capture_events").update({"status": "extracting"}).eq("id", capture_event_id).execute()
        db.table("cases").update({"review_status": "processing"}).eq("id", case_id).execute()

        document_text = await extract_text_from_pdf(db, storage_path)

        db.table("capture_events").update({
            "raw_payload": document_text[:MAX_TEXT_CHARS_FOR_STORAGE],
        }).eq("id", capture_event_id).execute()

        extracted = await extract_entities_with_claude(document_text)

        candidates_created = _insert_entity_candidates(db, capture_event_id, extracted)
        _update_case_from_extracted(db, case_id, extracted)

        db.table("capture_events").update({"status": "awaiting_review"}).eq("id", capture_event_id).execute()
        db.table("cases").update({"review_status": "needs_review"}).eq("id", case_id).execute()

        return {"status": "completed", "candidates_created": candidates_created}

    except ExtractionError as e:
        _mark_capture_error(db, case_id, capture_event_id, str(e))
        return {"status": "error", "error": str(e), "candidates_created": 0}
    except Exception as e:
        _mark_capture_error(db, case_id, capture_event_id, f"Unexpected: {type(e).__name__}: {e}")
        return {"status": "error", "error": str(e), "candidates_created": 0}
