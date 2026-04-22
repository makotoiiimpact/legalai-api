"""
LegalAI v2 — Firm Case Intake API
=================================
New HTTP surface for the UX Design v1 frontend (legalai-ui).
Mounted at /api/v2 in main.py. Old routes at root are untouched.

All routes hit legalai-dev (cfiaxrvtafszmgraftbk) via SUPABASE_DEV_URL.
Responses use camelCase to match the TypeScript interfaces in
legalai-ui/lib/types.ts.

Field mapping concessions for v1 (flagged for future migration):
- cases.client_name   → JSON caseName
- cases.jurisdiction  → JSON court
- cases.incident_date → JSON filedDate
- JSON courtDept      → null (no column on cases yet)
- cases.charge (single text) → JSON charges (split on ';' + statute parsed
  from trailing "(NRS ...)" / "(USC ...)"; fallback to 1-element array)
- EntityCandidate.attributionConfidence is derived from review_status
  because extraction_candidates has no attribution_confidence column
  (that column lives on case_attorneys).

Stubs (per spec D4/D5):
- POST /cases/upload launches run_extraction() (services/extraction.py) as a
  BackgroundTask. Real Tier 1 Claude extraction: downloads the PDF from the
  case-documents bucket, extracts text with pdfplumber, sends to Claude for
  structured entity extraction, matches against existing judges/prosecutors/
  attorneys, writes extraction_candidates rows, updates the cases row.
- GET /cases/{id}/matchup returns hardcoded Banuelos-shaped fixture.
  Real matchup computation from aggregation tables is post-demo.

Auth: None. Firm_id is NULL everywhere. The service-role key bypasses
the storage.objects RLS policies we set up in 20260421_002, so the
`garrett/` folder convention is not enforced in v1 — it's just a
naming convention that will map to auth.uid() once auth ships.
"""

import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from pydantic import BaseModel

from services.extraction import run_extraction

# Explicit .env path per legalai-api CLAUDE.md (python-dotenv 3.14 bug with find_dotenv).
load_dotenv(Path(__file__).parent.parent / ".env")

router = APIRouter()


# ─── Dev Supabase client ──────────────────────────────────────────────────────

def get_dev_db():
    """Client for legalai-dev (cfiaxrvtafszmgraftbk). Separate from the
    prod get_db() used by old routes in main.py."""
    from supabase import create_client
    url = os.environ.get("SUPABASE_DEV_URL")
    key = os.environ.get("SUPABASE_DEV_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError(
            "Missing SUPABASE_DEV_URL or SUPABASE_DEV_SERVICE_KEY in environment. "
            "See legalai-api/.env.example for the template."
        )
    return create_client(url, key)


# ─── Pydantic bodies ──────────────────────────────────────────────────────────

class CreateCaseBody(BaseModel):
    caseNumber: str


class CorrectionBody(BaseModel):
    correctionType: Literal["wrong_person", "wrong_role", "not_entity"]
    correctedEntityId: Optional[str] = None
    newEntityName: Optional[str] = None
    correctedRole: Optional[str] = None


class ResolveBody(BaseModel):
    pickedEntityId: Optional[str] = None


class AddEntityBody(BaseModel):
    name: str
    role: str  # matches EntityRole union on frontend


# ─── Constants + mappers ─────────────────────────────────────────────────────

STATUS_PRIORITY = {
    "processing": 0,
    "needs_review": 1,
    "in_review": 2,
    "shell": 3,
    "confirmed": 4,
}

CANDIDATE_TYPE_TO_ROLE = {
    "attorney": "defense_attorney",
    "judge": "judge",
    "prosecutor": "prosecutor",
    "officer": "officer",
    "witness": "witness",
    "expert": "expert",
}

ATTR_CONFIDENCE_BY_REVIEW = {
    "pending": "inferred",
    "confirmed": "attorney_verified",
    "edited": "attorney_verified",
    "rejected": "unverified",
}

ACCEPTED_EXTS = {".pdf", ".docx", ".jpg", ".jpeg", ".png", ".heic"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic"}
MAX_FILE_BYTES = 25 * 1024 * 1024


def derive_confidence_label(score) -> str:
    if score is None:
        return "high"
    s = float(score)
    if s >= 0.8:
        return "high"
    if s >= 0.6:
        return "medium"
    return "low"


def _role_to_candidate_type(role: str) -> str:
    return {
        "judge": "judge",
        "prosecutor": "prosecutor",
        "defense_attorney": "attorney",
        "co_counsel": "attorney",
        "officer": "officer",
        "witness": "witness",
        "expert": "expert",
        "defendant": "other",
    }.get(role, "other")


# ─── Case + entity shape builders ────────────────────────────────────────────

def build_case_summary(case_row, entity_count, confirmed_count, has_matchup, ambiguous_count):
    return {
        "id": case_row["id"],
        "caseNumber": case_row.get("case_number") or "",
        "caseName": case_row.get("client_name"),
        "court": case_row.get("jurisdiction"),
        "courtDept": None,  # no column yet — see file header
        "filedDate": case_row.get("incident_date"),
        "caseType": case_row.get("case_type"),
        "reviewStatus": case_row.get("review_status") or "shell",
        "dataTier": case_row.get("data_tier") or "tier_1_ai_extracted",
        "entityCount": entity_count,
        "confirmedCount": confirmed_count,
        "hasMatchupData": has_matchup,
        "ambiguousCount": ambiguous_count,
        "updatedAt": case_row["updated_at"],
    }


def build_entity_candidate(cand_row):
    payload = cand_row.get("proposed_payload") or {}
    role = (
        payload.get("role")
        or CANDIDATE_TYPE_TO_ROLE.get(cand_row.get("candidate_type"))
        or "witness"
    )
    alts_raw = cand_row.get("alternative_matches") or []
    alternatives = [
        {
            "entityId": a.get("entity_id") or a.get("entityId"),
            "name": a.get("name"),
            "priorCases": a.get("prior_cases") or a.get("priorCases") or 0,
            "jurisdiction": a.get("jurisdiction"),
        }
        for a in alts_raw
        if isinstance(a, dict)
    ]
    is_firm_member = bool(payload.get("is_firm_member"))
    match_status = (
        "auto_confirmed" if is_firm_member
        else "matched" if cand_row.get("matched_entity_id")
        else "ambiguous" if len(alternatives) > 0
        else "new"
    )
    review_status = cand_row.get("review_status") or "pending"
    return {
        "id": cand_row["id"],
        "role": role,
        "extractedName": payload.get("name") or "",
        "confidence": derive_confidence_label(cand_row.get("confidence_score")),
        "matchStatus": match_status,
        "matchedEntityId": cand_row.get("matched_entity_id"),
        "matchedEntityName": payload.get("matched_entity_name"),
        "matchedPriorCases": payload.get("matched_prior_cases"),
        "alternatives": alternatives or None,
        "reviewStatus": review_status,
        "attributionConfidence": ATTR_CONFIDENCE_BY_REVIEW.get(review_status, "inferred"),
        "isFirmMember": is_firm_member,
    }


def build_extracted_fields(case_row):
    def field(key, label, value):
        return {
            "key": key,
            "label": label,
            "value": value or None,
            "status": "extracted" if value else "pending",
        }
    return [
        field("case_number", "Case Number", case_row.get("case_number")),
        field("court", "Court", case_row.get("jurisdiction")),
        field("filed_date", "Filed", case_row.get("incident_date")),
        field("case_type", "Case Type", case_row.get("case_type")),
    ]


# Matches "(NRS 484C.110)" / "(USC 18.3)" etc. anchored at end of a segment.
_STATUTE_RE = re.compile(r"\(((?:NRS|USC)[^)]+)\)\s*$", re.IGNORECASE)
# Matches leading "COUNT 1:" / "COUNT 12: " etc. produced by run_extraction.
_COUNT_PREFIX_RE = re.compile(r"^\s*COUNT\s+\d+\s*:\s*", re.IGNORECASE)


def build_charges(case_row):
    """cases.charge is a single TEXT column. run_extraction writes multiple
    counts as "COUNT 1: desc (NRS X); COUNT 2: desc (NRS Y)". Split on ';'
    and parse each segment into a Charge object so Screen 4 (review) renders
    one bullet per count with a separate statute chip.

    On any parsing failure, falls back to a single-bullet representation so
    we never drop the charge text entirely."""
    charge_text = case_row.get("charge")
    if not charge_text:
        return []

    case_id = case_row["id"]
    fallback = [{
        "id": f"charge-{case_id}-1",
        "text": charge_text,
        "statute": None,
    }]

    try:
        segments = [s.strip() for s in charge_text.split(";") if s.strip()]
        if not segments:
            return fallback

        charges = []
        for i, seg in enumerate(segments, start=1):
            body = _COUNT_PREFIX_RE.sub("", seg).strip()
            statute_match = _STATUTE_RE.search(body)
            if statute_match:
                statute = statute_match.group(1).strip()
                text = body[: statute_match.start()].strip()
            else:
                statute = None
                text = body
            if not text:
                return fallback
            charges.append({
                "id": f"charge-{case_id}-{i}",
                "text": text,
                "statute": statute,
            })
        return charges
    except Exception:
        return fallback


def build_documents(capture_rows):
    docs = []
    for r in capture_rows:
        if r.get("source") not in ("document_upload", "image_upload"):
            continue
        meta = r.get("source_metadata") or {}
        name = meta.get("file_name") or "document.pdf"
        lower = name.lower()
        if lower.endswith(".pdf"):
            file_type = "pdf"
        elif lower.endswith(".docx") or lower.endswith(".doc"):
            file_type = "docx"
        else:
            file_type = "image"
        docs.append({
            "id": r["id"],
            "name": name,
            "fileType": file_type,
            "pageCount": meta.get("page_count"),
            "uploadedAt": r["created_at"],
            "sizeBytes": meta.get("size_bytes") or 0,
        })
    return docs


def _active(candidate_rows):
    """Drop rejected candidates — the review-flow 'not_entity' branch."""
    return [c for c in candidate_rows if c.get("review_status") != "rejected"]


def compute_entity_counts(candidate_rows, case_row=None):
    active = _active(candidate_rows)
    entity_count = len(active)
    confirmed_count = sum(
        1 for c in active
        if c.get("review_status") in ("confirmed", "edited")
    )
    ambiguous_count = sum(
        1 for c in active
        if (c.get("alternative_matches") or [])
        and c.get("review_status") == "pending"
    )
    # hasMatchupData aligns with the /matchup endpoint's gate: available when
    # the case is confirmed, OR when any entity has a concrete match.
    has_matchup = (
        (case_row and case_row.get("review_status") == "confirmed")
        or any(c.get("matched_entity_id") for c in active)
    )
    return entity_count, confirmed_count, ambiguous_count, has_matchup


def build_case_detail(case_row, candidate_rows, capture_rows):
    active = _active(candidate_rows)
    entities = [build_entity_candidate(c) for c in active]
    entity_count, confirmed_count, ambiguous_count, has_matchup = compute_entity_counts(
        candidate_rows, case_row
    )
    return {
        **build_case_summary(case_row, entity_count, confirmed_count, has_matchup, ambiguous_count),
        "charges": build_charges(case_row),
        "entities": entities,
        "documents": build_documents(capture_rows),
        "extractedFields": build_extracted_fields(case_row),
    }


# ─── Reads ────────────────────────────────────────────────────────────────────

@router.get("/cases")
def list_cases():
    db = get_dev_db()
    cases = db.table("cases").select("*").execute().data or []
    caps = db.table("capture_events").select("id, case_id").execute().data or []
    cap_to_case = {c["id"]: c["case_id"] for c in caps}
    case_has_capture = set(cap_to_case.values())
    cands = db.table("extraction_candidates").select("*").execute().data or []
    by_case: dict[str, list[dict]] = {}
    for c in cands:
        cid = cap_to_case.get(c.get("capture_event_id"))
        if cid:
            by_case.setdefault(cid, []).append(c)

    summaries = []
    for case_row in cases:
        # Hide Tier 0 seed cases with no uploaded document. Those are test-
        # harness rows preserved for their case_attorneys attribution data;
        # they have no intake artifact and clutter Garrett's list. The seed
        # rows stay in the DB; a future /admin view can surface them if
        # needed.
        if (
            case_row.get("data_tier") == "tier_0_public"
            and case_row["id"] not in case_has_capture
        ):
            continue
        cs = by_case.get(case_row["id"], [])
        ec, cc, ambig, hm = compute_entity_counts(cs, case_row)
        summaries.append(build_case_summary(case_row, ec, cc, hm, ambig))

    def sort_key(s):
        try:
            ts = -datetime.fromisoformat(s["updatedAt"].replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError, TypeError):
            ts = 0
        return (STATUS_PRIORITY.get(s["reviewStatus"], 99), ts)
    summaries.sort(key=sort_key)
    return summaries


@router.get("/cases/{case_id}")
def get_case(case_id: str):
    db = get_dev_db()
    rows = db.table("cases").select("*").eq("id", case_id).execute().data or []
    if not rows:
        raise HTTPException(404, "Case not found")
    case_row = rows[0]
    caps = db.table("capture_events").select("*").eq("case_id", case_id).execute().data or []
    cap_ids = [c["id"] for c in caps]
    cands = []
    if cap_ids:
        cands = db.table("extraction_candidates").select("*").in_("capture_event_id", cap_ids).execute().data or []
    return build_case_detail(case_row, cands, caps)


@router.get("/cases/{case_id}/documents/{document_id}/url")
def get_document_url(case_id: str, document_id: str):
    """Return a 1-hour signed URL for a case document so the Screen 5 View
    button can window.open() the PDF. document_id is the capture_event_id
    that build_documents exposed as Document.id to the frontend. The
    case-documents bucket is private with RLS; the service role used by
    this backend creates signed URLs without invoking RLS."""
    db = get_dev_db()
    rows = (
        db.table("capture_events")
          .select("id, source_metadata")
          .eq("id", document_id)
          .eq("case_id", case_id)
          .execute().data or []
    )
    if not rows:
        raise HTTPException(404, "Document not found")
    storage_path = (rows[0].get("source_metadata") or {}).get("storage_path")
    if not storage_path:
        raise HTTPException(404, "Document has no storage path on record")
    try:
        result = db.storage.from_("case-documents").create_signed_url(storage_path, 3600)
    except Exception as e:
        raise HTTPException(500, f"Storage create_signed_url failed: {e}")
    # supabase-py returns the URL under "signedURL" (most versions) or
    # "signedUrl"; accept either. Some versions wrap in {'data': {...}}.
    inner = result.get("data") if isinstance(result, dict) else None
    payload = inner if isinstance(inner, dict) else (result if isinstance(result, dict) else {})
    url = payload.get("signedURL") or payload.get("signedUrl") or payload.get("signed_url")
    if not url:
        raise HTTPException(500, f"Storage did not return a signed URL; got {result!r}")
    return {"url": url}


@router.get("/cases/{case_id}/extraction")
def get_extraction(case_id: str):
    db = get_dev_db()
    rows = db.table("cases").select("*").eq("id", case_id).execute().data or []
    if not rows:
        raise HTTPException(404, "Case not found")
    case_row = rows[0]
    caps = (db.table("capture_events").select("*")
              .eq("case_id", case_id)
              .order("created_at", desc=True)
              .execute().data or [])
    doc_cap = caps[0] if caps else None
    cap_ids = [c["id"] for c in caps]
    cands = []
    if cap_ids:
        cands = db.table("extraction_candidates").select("*").in_("capture_event_id", cap_ids).execute().data or []

    status = case_row.get("review_status") or "shell"
    state = "extracting" if status == "processing" else "complete"
    entities = [build_entity_candidate(c) for c in _active(cands)]
    doc_meta = (doc_cap.get("source_metadata") if doc_cap else None) or {}
    return {
        "caseId": case_id,
        "state": state,
        "fields": build_extracted_fields(case_row),
        "entities": entities,
        "documentName": doc_meta.get("file_name") or "",
        "documentPageCount": doc_meta.get("page_count"),
        "totalEntitiesFound": len(entities),
        "startedAt": case_row["updated_at"],
    }


def _fetch_confirmed_entity_names(db, case_id: str) -> tuple[Optional[str], Optional[str]]:
    """Pull the confirmed judge + prosecutor names from extraction_candidates
    so the matchup card echoes the names Garrett just reviewed, not names from
    someone else's case. Returns (judge_name, prosecutor_name), either None if
    the role wasn't extracted or wasn't confirmed."""
    caps = db.table("capture_events").select("id").eq("case_id", case_id).execute().data or []
    cap_ids = [c["id"] for c in caps]
    if not cap_ids:
        return (None, None)
    cands = (
        db.table("extraction_candidates")
          .select("candidate_type, proposed_payload, review_status")
          .in_("capture_event_id", cap_ids)
          .in_("review_status", ["confirmed", "edited"])
          .execute().data or []
    )
    judge = prosecutor = None
    for c in cands:
        payload = c.get("proposed_payload") or {}
        name = (payload.get("name") or "").strip()
        if not name:
            continue
        if c["candidate_type"] == "judge" and not judge:
            judge = name
        elif c["candidate_type"] == "prosecutor" and not prosecutor:
            prosecutor = name
    return (judge, prosecutor)


def _count_cases_with_entity(db, candidate_type: str, entity_name: str) -> int:
    """Count distinct confirmed cases that have a confirmed/edited extraction
    candidate of the given type whose name case-insensitive-matches.

    Drives priorCasesWithYou on the matchup card — "You've appeared before
    Kephart 2 times" for two confirmed cases with Kephart as the judge.
    Includes the current case, which is the intended "cases you've seen
    this entity on" definition.

    Three cheap queries rather than a server-side join because supabase-py
    doesn't expose PostgREST embedded resources for our shape cleanly, and
    the list of candidates is small for the demo. Revisit with a proper
    aggregation view when this matters for >10s of cases.
    """
    if not entity_name:
        return 0
    wanted = entity_name.strip().lower()
    try:
        cands = (
            db.table("extraction_candidates")
              .select("capture_event_id, proposed_payload")
              .eq("candidate_type", candidate_type)
              .in_("review_status", ["confirmed", "edited"])
              .execute().data or []
        )
    except Exception as e:
        print(f"[_count_cases_with_entity] candidates query failed: {e}")
        return 0

    cap_ids = sorted({
        c["capture_event_id"]
        for c in cands
        if ((c.get("proposed_payload") or {}).get("name") or "").strip().lower() == wanted
        and c.get("capture_event_id")
    })
    if not cap_ids:
        return 0

    try:
        caps = (
            db.table("capture_events")
              .select("id, case_id")
              .in_("id", cap_ids)
              .execute().data or []
        )
    except Exception as e:
        print(f"[_count_cases_with_entity] capture_events query failed: {e}")
        return 0

    case_ids = sorted({c["case_id"] for c in caps if c.get("case_id")})
    if not case_ids:
        return 0

    try:
        cases = (
            db.table("cases")
              .select("id")
              .in_("id", case_ids)
              .eq("review_status", "confirmed")
              .execute().data or []
        )
    except Exception as e:
        print(f"[_count_cases_with_entity] cases query failed: {e}")
        return 0

    return len(cases)


def _title_case_name(s: str) -> str:
    """Mirror the frontend's titleCaseName so strings baked into the fixture
    (patternNarrative, placeholderCopy) render consistently with the names
    the UI displays via its own titleCaseName pass.

    Strings that already contain any lowercase letter are returned unchanged;
    all-caps words get title-cased word-by-word, with single-letter initials
    like "T." preserved.
    """
    if not s or any(c.islower() for c in s):
        return s
    out = []
    for part in s.split():
        if len(part) <= 2 and part.endswith("."):
            out.append(part)
        else:
            out.append(part[:1].upper() + part[1:].lower())
    return " ".join(out)


def _last_name(full_name: str) -> str:
    """Return the title-cased last whitespace-separated token of a name after
    stripping any trailing comma-qualified suffix ("JOHN JONES, DDA" → "Jones")."""
    if not full_name:
        return ""
    stripped = full_name.split(",")[0].strip()
    parts = stripped.split()
    tail = parts[-1] if parts else stripped
    return _title_case_name(tail)


@router.get("/cases/{case_id}/matchup")
def get_matchup(case_id: str):
    db = get_dev_db()
    rows = db.table("cases").select("review_status").eq("id", case_id).execute().data or []
    if not rows:
        raise HTTPException(404, "Case not found")
    if rows[0].get("review_status") != "confirmed":
        return None
    # v1 stub: motion stats, own-record, and growthHint stay as fixture data
    # (aggregation-driven matchup is post-demo, spec D5). Names, per-entity
    # counts, and copy that references those names are now pulled from the
    # case's confirmed entities + the set of other confirmed cases that share
    # them, so Garrett sees coherent "before Kephart 2 times" numbers rather
    # than a hardcoded "4".
    judge_name, prosecutor_name = _fetch_confirmed_entity_names(db, case_id)
    judge_display = judge_name or "William Kephart"
    prosecutor_display = prosecutor_name or "John Jones, DDA"
    judge_last = _last_name(judge_display)
    prosecutor_last = _last_name(prosecutor_display)
    judge_prior = _count_cases_with_entity(db, "judge", judge_name) if judge_name else 0
    prosecutor_prior = (
        _count_cases_with_entity(db, "prosecutor", prosecutor_name) if prosecutor_name else 0
    )
    return {
        "caseId": case_id,
        "judge": {
            "judgeName": judge_display,
            "priorCasesWithYou": judge_prior,
            "tier": "building",
            "motionStats": [
                {"label": "Suppression motions", "granted": 3, "total": 4},
                {"label": "Continuance requests", "granted": 2, "total": 2},
            ],
            "avgDispositionDays": 94,
            "patternNarrative": (
                f"{judge_last} tends to grant suppression motions when bodycam "
                "evidence is contested. He has denied 1 motion where the "
                "stop was based on a 911 caller report."
            ),
            "growthHint": {
                "totalAvailableCases": 2200,
                "casesToNextTier": 10,
                "nextTier": "strong",
            },
        },
        "prosecutor": {
            "prosecutorName": prosecutor_display,
            "priorCasesWithYou": prosecutor_prior,
            "tier": "sparse",
            "placeholderCopy": (
                f"When you've faced {prosecutor_last} on 3+ cases, you'll see plea offer "
                "patterns and timing, trial vs. plea tendencies, and charge "
                "bargaining behavior."
            ),
        },
        "ownRecord": {
            "scope": "Dept. 14",
            "caseCount": 4,
            "focusCaseType": "DUI-focused",
            "outcomes": [
                {"label": "Dismissed", "count": 1},
                {"label": "Reduced charges", "count": 2},
                {"label": "Guilty as charged", "count": 1},
            ],
            "winRatePct": 75,
        },
    }


# ─── Case creation ───────────────────────────────────────────────────────────

@router.post("/cases")
def create_case_from_number(body: CreateCaseBody):
    db = get_dev_db()
    existing = (db.table("cases").select("id")
                  .eq("case_number", body.caseNumber)
                  .execute().data or [])
    if existing:
        return {"caseId": existing[0]["id"], "duplicateOf": existing[0]["id"]}

    case_id = str(uuid.uuid4())
    # cases.client_name is NOT NULL on the existing schema. v1 workaround:
    # store the case number as the placeholder name until extraction fills it.
    # Proper fix: nullable-client_name migration (deferred).
    db.table("cases").insert({
        "id": case_id,
        "case_number": body.caseNumber,
        "client_name": body.caseNumber,
        "case_type": "Unknown",
        "jurisdiction": "Unknown",
        "status": "intake",              # old-surface column, kept for compat
        "review_status": "shell",
        "data_tier": "tier_2_manual",
    }).execute()
    return {"caseId": case_id}


@router.post("/cases/upload")
async def upload_case(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    name_lower = (file.filename or "").lower()
    ext = Path(name_lower).suffix
    if ext not in ACCEPTED_EXTS:
        raise HTTPException(400, f"Unsupported file type: {ext}")

    contents = await file.read()
    if len(contents) > MAX_FILE_BYTES:
        raise HTTPException(400, "File exceeds 25 MB limit")

    is_image = ext in IMAGE_EXTS
    db = get_dev_db()
    case_id = str(uuid.uuid4())

    # 1. Create case row. client_name is NOT NULL on cases so we stash a
    # placeholder that run_extraction overwrites when it finishes.
    placeholder_number = f"pending-{case_id[:8]}"
    db.table("cases").insert({
        "id": case_id,
        "case_number": placeholder_number,
        "client_name": "(extraction pending)",
        "case_type": "Unknown",
        "jurisdiction": "Unknown",
        "status": "intake",
        "review_status": "shell" if is_image else "processing",
        "data_tier": "tier_2_manual" if is_image else "tier_1_ai_extracted",
    }).execute()

    # 2. Upload to Supabase Storage. Path convention: garrett/{case_id}/{filename}
    #    (Service key bypasses RLS; folder convention will map to auth.uid() once auth lands.)
    storage_path = f"garrett/{case_id}/{file.filename}"
    try:
        db.storage.from_("case-documents").upload(
            path=storage_path,
            file=contents,
            file_options={"content-type": file.content_type or "application/octet-stream"},
        )
    except Exception as e:
        # Don't fail the whole request — case + capture_event are still useful
        # for surfacing errors in the UI. Log and continue.
        print(f"[upload] storage upload failed for {storage_path}: {e}")

    # 3. Record capture_event.
    capture_id = str(uuid.uuid4())
    db.table("capture_events").insert({
        "id": capture_id,
        "case_id": case_id,
        "source": "image_upload" if is_image else "document_upload",
        "source_metadata": {
            "file_name": file.filename,
            "size_bytes": len(contents),
            "storage_path": storage_path,
            "page_count": 3 if not is_image else None,
        },
        "status": "received",
        "created_by": None,
    }).execute()

    # 4. Launch Tier 1 extraction (skipped for images — image OCR is future work).
    if not is_image:
        background_tasks.add_task(run_extraction, case_id, capture_id)

    # 5. Return initial CaseDetail.
    case_row = (db.table("cases").select("*").eq("id", case_id).execute().data or [None])[0]
    caps = db.table("capture_events").select("*").eq("case_id", case_id).execute().data or []
    return build_case_detail(case_row, [], caps)


# ─── Entity review actions ───────────────────────────────────────────────────

@router.patch("/cases/{case_id}/entities/confirm-all")
def confirm_all(case_id: str):
    db = get_dev_db()
    cap_ids = [c["id"] for c in (db.table("capture_events").select("id")
                                    .eq("case_id", case_id).execute().data or [])]
    if cap_ids:
        pending = (db.table("extraction_candidates")
                     .select("id, confidence_score")
                     .in_("capture_event_id", cap_ids)
                     .eq("review_status", "pending")
                     .execute().data or [])
        for p in pending:
            if float(p.get("confidence_score") or 0) >= 0.8:
                db.table("extraction_candidates").update({
                    "review_status": "confirmed",
                }).eq("id", p["id"]).execute()
    _maybe_finalize_case(db, case_id)
    return get_case(case_id)


@router.patch("/cases/{case_id}/entities/{entity_id}/confirm")
def confirm_entity(case_id: str, entity_id: str):
    db = get_dev_db()
    db.table("extraction_candidates").update({
        "review_status": "confirmed",
    }).eq("id", entity_id).execute()
    _maybe_finalize_case(db, case_id)
    return get_case(case_id)


@router.patch("/cases/{case_id}/entities/{entity_id}/correct")
def correct_entity(case_id: str, entity_id: str, body: CorrectionBody):
    db = get_dev_db()
    existing = (db.table("extraction_candidates").select("*")
                  .eq("id", entity_id).execute().data or [None])[0]
    if not existing:
        raise HTTPException(404, "Entity not found")

    if body.correctionType == "not_entity":
        db.table("extraction_candidates").update({
            "review_status": "rejected",
            "correction_type": "not_entity",
        }).eq("id", entity_id).execute()

    elif body.correctionType == "wrong_person":
        payload = existing.get("proposed_payload") or {}
        if body.newEntityName:
            payload["name"] = body.newEntityName
        db.table("extraction_candidates").update({
            "proposed_payload": payload,
            "matched_entity_id": body.correctedEntityId,
            "review_status": "edited",
            "correction_type": "wrong_person",
            "corrected_entity_id": body.correctedEntityId,
        }).eq("id", entity_id).execute()

    elif body.correctionType == "wrong_role":
        payload = existing.get("proposed_payload") or {}
        if body.correctedRole:
            payload["role"] = body.correctedRole
        db.table("extraction_candidates").update({
            "proposed_payload": payload,
            "review_status": "edited",
            "correction_type": "wrong_role",
            "corrected_role": body.correctedRole,
        }).eq("id", entity_id).execute()

    _maybe_finalize_case(db, case_id)
    return get_case(case_id)


@router.patch("/cases/{case_id}/entities/{entity_id}/resolve")
def resolve_ambiguous(case_id: str, entity_id: str, body: ResolveBody):
    db = get_dev_db()
    if body.pickedEntityId is None:
        db.table("extraction_candidates").update({
            "matched_entity_id": None,
            "alternative_matches": [],
            "review_status": "pending",
        }).eq("id", entity_id).execute()
    else:
        db.table("extraction_candidates").update({
            "matched_entity_id": body.pickedEntityId,
            "alternative_matches": [],
            "review_status": "confirmed",
        }).eq("id", entity_id).execute()
    _maybe_finalize_case(db, case_id)
    return get_case(case_id)


@router.post("/cases/{case_id}/entities")
def add_entity(case_id: str, body: AddEntityBody):
    db = get_dev_db()
    caps = (db.table("capture_events").select("id")
              .eq("case_id", case_id)
              .order("created_at", desc=True)
              .limit(1)
              .execute().data or [])
    if caps:
        capture_id = caps[0]["id"]
    else:
        # Manual-only case (no document yet) — synthesize a capture_event.
        capture_id = str(uuid.uuid4())
        db.table("capture_events").insert({
            "id": capture_id,
            "case_id": case_id,
            "source": "manual",
            "source_metadata": {},
            "status": "confirmed",
        }).execute()

    db.table("extraction_candidates").insert({
        "id": str(uuid.uuid4()),
        "capture_event_id": capture_id,
        "firm_id": None,
        "candidate_type": _role_to_candidate_type(body.role),
        "proposed_payload": {"name": body.name, "role": body.role},
        "confidence_score": 1.0,
        "status": "confirmed",
        "review_status": "confirmed",
    }).execute()
    _maybe_finalize_case(db, case_id)
    return get_case(case_id)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _maybe_finalize_case(db, case_id: str):
    """Cascade case.review_status based on child candidate states:
       - no pending & at least one active candidate → 'confirmed'
       - any confirmed/edited but some still pending → 'in_review'
       - otherwise (all pending still)               → leave as 'needs_review'"""
    cap_ids = [c["id"] for c in (db.table("capture_events").select("id")
                                    .eq("case_id", case_id).execute().data or [])]
    if not cap_ids:
        return
    cands = (db.table("extraction_candidates").select("review_status")
               .in_("capture_event_id", cap_ids).execute().data or [])
    active = [c for c in cands if c.get("review_status") != "rejected"]
    if not active:
        return
    any_pending = any(c.get("review_status") == "pending" for c in active)
    any_confirmed = any(c.get("review_status") in ("confirmed", "edited") for c in active)
    if not any_pending:
        db.table("cases").update({"review_status": "confirmed"}).eq("id", case_id).execute()
    elif any_confirmed:
        db.table("cases").update({"review_status": "in_review"}).eq("id", case_id).execute()
