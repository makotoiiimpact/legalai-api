"""
LegalAI — FastAPI Backend
=========================
Wraps ingest, analyze, memo, and HIL review into HTTP endpoints.
Deploy to Railway. Frontend at paralegal.iiimpact.ai calls this.

Routes:
  GET  /health
  POST /cases
  GET  /cases
  GET  /cases/{case_id}
  POST /cases/{case_id}/documents        ← file upload + ingest
  GET  /cases/{case_id}/documents
  POST /cases/{case_id}/analyze          ← run AI checklist
  GET  /cases/{case_id}/findings
  POST /cases/{case_id}/findings/{id}/review  ← HIL confirm/edit/reject
  POST /cases/{case_id}/memo             ← generate disposition memo
  POST /cases/{case_id}/memo/approve     ← attorney approval gate
  GET  /cases/{case_id}/audit
  GET  /confidence                       ← HIL graduation dashboard
"""

import os
import uuid
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(
    title="LegalAI API",
    description="Criminal defense case analysis — Law Offices of Garrett T. Ogata",
    version="0.1.0"
)

# CORS — allow legalai.iiimpact.ai + paralegal.iiimpact.ai and localhost dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://legalai.iiimpact.ai",
        "https://paralegal.iiimpact.ai",
        "http://localhost:3000",
        "http://localhost:3001",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Lazy Supabase client ──────────────────────────────────────────────────────

def get_db():
    from supabase import create_client
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"]
    )

# ─── Pydantic models ──────────────────────────────────────────────────────────

class CreateCaseRequest(BaseModel):
    case_number: str           # e.g. "GTO-2024-001"
    client_name: str
    case_type: str             # "DUI" | "Drug" | "Assault"
    charge: Optional[str] = None
    charge_severity: Optional[str] = None  # "misdemeanor" | "felony"
    incident_date: Optional[str] = None    # "2024-11-14"
    jurisdiction: str = "Clark County, NV"
    notes: Optional[str] = None

class ReviewFindingRequest(BaseModel):
    action: str                # "confirmed" | "edited" | "rejected"
    edited_answer: Optional[str] = None
    reviewer_name: str = "Paralegal"
    note: Optional[str] = None

class ApproveMemoRequest(BaseModel):
    attorney_name: str = "G. Ogata"
    notes: Optional[str] = None

# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "legalai-api",
        "version": "0.1.0",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

# ─── Cases ────────────────────────────────────────────────────────────────────

@app.post("/cases", status_code=201)
def create_case(body: CreateCaseRequest):
    db = get_db()
    case_id = str(uuid.uuid4())

    result = db.table("cases").insert({
        "id": case_id,
        "case_number": body.case_number,
        "client_name": body.client_name,
        "case_type": body.case_type,
        "charge": body.charge,
        "charge_severity": body.charge_severity,
        "incident_date": body.incident_date,
        "jurisdiction": body.jurisdiction,
        "notes": body.notes,
        "status": "intake",
    }).execute()

    db.table("audit_log").insert({
        "case_id": case_id,
        "action": "CASE_CREATED",
        "actor": "system",
        "actor_name": "API",
        "note": f"Case {body.case_number} created for {body.client_name}",
    }).execute()

    return result.data[0]


@app.get("/cases")
def list_cases(status: Optional[str] = None, case_type: Optional[str] = None):
    db = get_db()
    q = db.table("cases").select("*").order("created_at", desc=True)
    if status:
        q = q.eq("status", status)
    if case_type:
        q = q.eq("case_type", case_type)
    return q.execute().data


@app.get("/cases/{case_id}")
def get_case(case_id: str):
    db = get_db()
    result = db.table("cases").select("*").eq("id", case_id).single().execute()
    if not result.data:
        raise HTTPException(404, "Case not found")
    return result.data

# ─── Documents ────────────────────────────────────────────────────────────────

@app.post("/cases/{case_id}/documents", status_code=201)
async def upload_document(
    case_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    doc_type: str = Form(default="other"),
):
    """
    Upload a document and kick off background ingestion (extract → chunk → embed → store).
    Returns immediately with document ID. Poll GET /cases/{id}/documents to check indexed status.
    """
    db = get_db()

    # Verify case exists
    case = db.table("cases").select("id, case_number").eq("id", case_id).single().execute()
    if not case.data:
        raise HTTPException(404, "Case not found")

    # Save upload to temp file
    suffix = Path(file.filename).suffix
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    # Create document record (not indexed yet)
    doc_id = str(uuid.uuid4())
    db.table("documents").insert({
        "id": doc_id,
        "case_id": case_id,
        "name": file.filename,
        "doc_type": doc_type,
        "file_size_kb": round(len(content) / 1024),
        "indexed": False,
    }).execute()

    # Update case status
    db.table("cases").update({"status": "indexing"}).eq("id", case_id).execute()

    # Background task: ingest (embed + store chunks)
    background_tasks.add_task(_ingest_background, tmp_path, doc_id, case_id, doc_type, file.filename)

    return {
        "document_id": doc_id,
        "name": file.filename,
        "doc_type": doc_type,
        "indexed": False,
        "message": "Document received. Ingestion running in background — poll status to confirm."
    }


def _ingest_background(tmp_path: str, doc_id: str, case_id: str, doc_type: str, original_name: str):
    """Background task: runs full ingestion pipeline on uploaded file."""
    try:
        from ingestion.ingest import extract_text, chunk_text, embed_chunks, store_chunks, log_audit
        db = get_db()

        raw_text, page_count = extract_text(tmp_path)
        chunks = chunk_text(raw_text, doc_type, original_name)
        chunks = embed_chunks(chunks)

        # Update doc with extracted text + page count
        db.table("documents").update({
            "raw_text": raw_text,
            "page_count": page_count,
        }).eq("id", doc_id).execute()

        store_chunks(doc_id, case_id, chunks)
        log_audit(case_id, doc_id, len(chunks), original_name)

        # Update case status to analysis-ready if all docs indexed
        db.table("cases").update({"status": "analysis"}).eq("id", case_id).execute()

    except Exception as e:
        db = get_db()
        db.table("audit_log").insert({
            "case_id": case_id,
            "document_id": doc_id,
            "action": "INGEST_ERROR",
            "actor": "system",
            "note": str(e),
        }).execute()
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.get("/cases/{case_id}/documents")
def list_documents(case_id: str):
    db = get_db()
    return db.table("documents").select(
        "id, name, doc_type, file_size_kb, page_count, indexed, indexed_at, chunk_count, created_at"
    ).eq("case_id", case_id).order("created_at").execute().data

# ─── Analysis ─────────────────────────────────────────────────────────────────

@app.post("/cases/{case_id}/analyze", status_code=202)
def run_analysis(case_id: str, background_tasks: BackgroundTasks):
    """
    Kick off AI analysis checklist for a case.
    Runs in background — poll GET /cases/{id}/findings for results.
    """
    db = get_db()

    case = db.table("cases").select("id, case_type, status").eq("id", case_id).single().execute()
    if not case.data:
        raise HTTPException(404, "Case not found")

    # Check at least one document is indexed
    docs = db.table("documents").select("id").eq("case_id", case_id).eq("indexed", True).execute()
    if not docs.data:
        raise HTTPException(400, "No indexed documents found. Upload and wait for indexing first.")

    run_id = str(uuid.uuid4())
    db.table("cases").update({"status": "analysis"}).eq("id", case_id).execute()

    background_tasks.add_task(
        _analyze_background,
        case_id,
        case.data["case_type"] or "DUI",
        run_id
    )

    return {
        "run_id": run_id,
        "case_id": case_id,
        "case_type": case.data["case_type"],
        "message": "Analysis started. Poll GET /cases/{id}/findings for results."
    }


def _analyze_background(case_id: str, case_type: str, run_id: str):
    """Background task: runs full analysis checklist."""
    try:
        from agents.analyze import analyze_case
        analyze_case(case_id, case_type)

        db = get_db()
        db.table("cases").update({"status": "review"}).eq("id", case_id).execute()
    except Exception as e:
        db = get_db()
        db.table("audit_log").insert({
            "case_id": case_id,
            "action": "ANALYSIS_ERROR",
            "actor": "system",
            "note": str(e),
            "metadata": {"run_id": run_id}
        }).execute()


@app.get("/cases/{case_id}/findings")
def list_findings(case_id: str, run_id: Optional[str] = None):
    db = get_db()
    q = db.table("findings").select("*").eq("case_id", case_id)
    if run_id:
        q = q.eq("run_id", run_id)
    return q.order("created_at").execute().data

# ─── HIL Review ───────────────────────────────────────────────────────────────

@app.post("/cases/{case_id}/findings/{finding_id}/review")
def review_finding(case_id: str, finding_id: str, body: ReviewFindingRequest):
    """
    Paralegal HIL action: confirm, edit, or reject a finding.
    Every action is written to audit_log immediately.
    """
    if body.action not in ("confirmed", "edited", "rejected"):
        raise HTTPException(400, "action must be 'confirmed', 'edited', or 'rejected'")

    if body.action == "edited" and not body.edited_answer:
        raise HTTPException(400, "edited_answer required when action is 'edited'")

    db = get_db()

    # Verify finding belongs to this case
    finding = db.table("findings").select("id, label").eq("id", finding_id).eq("case_id", case_id).single().execute()
    if not finding.data:
        raise HTTPException(404, "Finding not found")

    now = datetime.now(timezone.utc).isoformat()
    update = {
        "hil_status": body.action,
        "reviewed_by": body.reviewer_name,
        "reviewed_at": now,
    }
    if body.action == "edited":
        update["edited_answer"] = body.edited_answer

    db.table("findings").update(update).eq("id", finding_id).execute()

    db.table("audit_log").insert({
        "case_id": case_id,
        "finding_id": finding_id,
        "action": f"FINDING_{body.action.upper()}",
        "actor": "paralegal",
        "actor_name": body.reviewer_name,
        "note": body.note or "",
        "metadata": {"finding_label": finding.data["label"]}
    }).execute()

    # Check if all findings reviewed — auto-advance case status
    all_findings = db.table("findings").select("hil_status").eq("case_id", case_id).execute().data
    all_reviewed = all(f["hil_status"] is not None for f in all_findings) if all_findings else False
    if all_reviewed:
        db.table("cases").update({"status": "memo_draft"}).eq("id", case_id).execute()

    return {
        "finding_id": finding_id,
        "action": body.action,
        "all_reviewed": all_reviewed,
        "message": "Review saved and logged to audit trail."
    }

# ─── Disposition Memo ─────────────────────────────────────────────────────────

@app.post("/cases/{case_id}/memo", status_code=202)
def generate_memo(case_id: str, background_tasks: BackgroundTasks):
    """
    Generate disposition memo from all reviewed findings.
    Requires all findings to be reviewed first.
    """
    db = get_db()

    # Enforce: all findings must be reviewed
    findings = db.table("findings").select("hil_status").eq("case_id", case_id).execute().data
    if not findings:
        raise HTTPException(400, "No findings found. Run analysis first.")

    unreviewed = [f for f in findings if f["hil_status"] is None]
    if unreviewed:
        raise HTTPException(
            400,
            f"{len(unreviewed)} findings still need HIL review before memo can be generated."
        )

    db.table("cases").update({"status": "memo_draft"}).eq("id", case_id).execute()
    background_tasks.add_task(_memo_background, case_id)

    return {
        "case_id": case_id,
        "message": "Memo generation started. Check GET /cases/{id} for status update."
    }


def _memo_background(case_id: str):
    try:
        from agents.memo import generate_memo
        generate_memo(case_id)
    except Exception as e:
        db = get_db()
        db.table("audit_log").insert({
            "case_id": case_id,
            "action": "MEMO_ERROR",
            "actor": "system",
            "note": str(e),
        }).execute()


@app.get("/cases/{case_id}/memo")
def get_memo(case_id: str):
    """Get the latest disposition memo for a case."""
    db = get_db()
    result = db.table("disposition_memos").select("*").eq("case_id", case_id).order(
        "created_at", desc=True
    ).limit(1).execute()

    if not result.data:
        raise HTTPException(404, "No memo found. Generate one first.")
    return result.data[0]


@app.post("/cases/{case_id}/memo/approve")
def approve_memo(case_id: str, body: ApproveMemoRequest):
    """
    ATTORNEY GATE — hard requirement, non-negotiable.
    Only this endpoint sets attorney_approved = True.
    """
    db = get_db()

    memo = db.table("disposition_memos").select("id").eq("case_id", case_id).eq(
        "attorney_approved", False
    ).order("created_at", desc=True).limit(1).execute()

    if not memo.data:
        raise HTTPException(404, "No pending memo found for approval.")

    memo_id = memo.data[0]["id"]
    now = datetime.now(timezone.utc).isoformat()

    db.table("disposition_memos").update({
        "attorney_approved": True,
        "approved_at": now,
        "approved_by": body.attorney_name,
        "attorney_notes": body.notes or "",
    }).eq("id", memo_id).execute()

    db.table("cases").update({"status": "complete"}).eq("id", case_id).execute()

    db.table("audit_log").insert({
        "case_id": case_id,
        "action": "MEMO_APPROVED",
        "actor": "attorney",
        "actor_name": body.attorney_name,
        "note": body.notes or "Approved",
        "metadata": {"memo_id": memo_id}
    }).execute()

    return {
        "case_id": case_id,
        "memo_id": memo_id,
        "approved_by": body.attorney_name,
        "approved_at": now,
        "message": "Memo approved. Case marked complete."
    }

# ─── Audit Log ────────────────────────────────────────────────────────────────

@app.get("/cases/{case_id}/audit")
def get_audit_log(case_id: str):
    db = get_db()
    return db.table("audit_log").select("*").eq("case_id", case_id).order(
        "created_at", desc=True
    ).execute().data

# ─── Confidence Dashboard ─────────────────────────────────────────────────────

@app.get("/confidence")
def get_confidence():
    """
    Returns per-check-type accuracy stats from the confidence_by_check_type view.
    Powers the HIL graduation dashboard.
    """
    db = get_db()
    try:
        result = db.table("confidence_by_check_type").select("*").execute()
        return result.data
    except Exception:
        # View may not exist yet if schema hasn't been run
        return []
