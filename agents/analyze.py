"""
LegalAI — Case Analysis Agent
==============================
Runs a structured checklist against the case RAG.
Produces findings with confidence scores and source citations.
Each finding goes into the HIL review queue.

Usage:
  python agents/analyze.py --case-id GTO-2024-001
  python agents/analyze.py --case-id GTO-2024-001 --case-type DUI
"""

import os
import sys
import json
import uuid
import argparse
from datetime import datetime, timezone

import anthropic
from openai import OpenAI
from supabase import create_client
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

load_dotenv()
console = Console()

# ─── Clients ──────────────────────────────────────────────────────────────────
# Lazy init — clients only used if API keys present (allows dry-run/test without keys)

def _get_claude():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

def _get_openai():
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])

def _get_supabase():
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"]
    )

EMBEDDING_MODEL = "text-embedding-3-small"
CLAUDE_MODEL    = "claude-sonnet-4-20250514"

# ─── Check Definitions ────────────────────────────────────────────────────────
# Each check has:
#   check_type: canonical key (used for confidence tracking)
#   label:      human-readable label shown in UI
#   question:   what we ask the RAG
#   case_types: which case types this check applies to

CHECKS = {
    "DUI": [
        {
            "check_type": "stop_justification",
            "label": "Traffic stop justification documented",
            "question": "Is the reason for the traffic stop clearly documented in the arrest report? What specific violation did the officer observe? Include the exact time the stop was initiated."
        },
        {
            "check_type": "miranda_documentation",
            "label": "Miranda rights documented and acknowledged",
            "question": "Was the Miranda warning administered? Is there a signed Miranda acknowledgment form or witness signature? Note whether acknowledgment was verbal only or written."
        },
        {
            "check_type": "breathalyzer_calibration",
            "label": "Breathalyzer calibration within required 90-day window",
            "question": "What is the Intoxilyzer/breathalyzer device ID and last certified calibration date? When was the test administered? Calculate the days elapsed and check whether it falls within Nevada's 90-day calibration requirement under NAC 484C.400."
        },
        {
            "check_type": "fst_protocol",
            "label": "Field sobriety tests administered per NHTSA protocol",
            "question": "Which standardized field sobriety tests (HGN, Walk-and-Turn, One-Leg-Stand) were administered? Were all three administered? If any were omitted, is there documentation of a physical limitation preventing them? List the clue counts for each test."
        },
        {
            "check_type": "observation_period",
            "label": "20-minute pre-test observation period documented",
            "question": "What time was the traffic stop initiated? What time was the breathalyzer test administered? Calculate the elapsed time. Nevada requires a continuous 20-minute observation period before breath testing (NAC 484C.400). Was this requirement met?"
        },
        {
            "check_type": "bac_result",
            "label": "BAC result and legal threshold",
            "question": "What was the BAC result? What is the defendant's driver classification (standard, commercial, underage)? Compare against Nevada legal thresholds: 0.08% standard, 0.04% commercial, 0.02% underage."
        },
        {
            "check_type": "officer_certification",
            "label": "Arresting officer DUI certification documented",
            "question": "Is the arresting officer's name documented? Is there any mention of their DUI/SFST certification or training? Note their badge number and unit."
        },
        {
            "check_type": "blood_draw_procedure",
            "label": "Blood draw procedure compliance (if applicable)",
            "question": "Was a blood draw performed instead of or in addition to breath testing? If so, was it performed by a qualified person (physician, nurse, phlebotomist per NRS 484C.160)? Is the chain of custody for blood evidence documented?"
        },
    ],
    "Drug": [
        {
            "check_type": "stop_justification",
            "label": "Stop/contact justification documented",
            "question": "What was the legal basis for the initial stop or contact? Was it a traffic stop, consensual encounter, or arrest warrant? Document the officer's stated probable cause."
        },
        {
            "check_type": "search_legality",
            "label": "Search legality — warrant or valid exception",
            "question": "Was a search warrant obtained? If not, what exception to the warrant requirement applies (consent, plain view, search incident to arrest, automobile exception, exigent circumstances)? Is the exception properly documented?"
        },
        {
            "check_type": "miranda_documentation",
            "label": "Miranda rights documented and acknowledged",
            "question": "Was the Miranda warning administered before any custodial interrogation? Is acknowledgment documented? Were any statements made by the defendant post-Miranda?"
        },
        {
            "check_type": "chain_of_custody",
            "label": "Drug evidence chain of custody intact",
            "question": "Is there a complete chain of custody log for the controlled substance evidence? Identify every person who handled the evidence from collection through testing. Note any gaps or missing signatures."
        },
        {
            "check_type": "lab_analysis",
            "label": "Lab analysis confirms substance identity",
            "question": "Was the substance submitted for laboratory analysis? What were the lab results? What specific controlled substance was identified and what schedule is it under NRS 453?"
        },
        {
            "check_type": "quantity_and_packaging",
            "label": "Quantity documented — possession vs. trafficking threshold",
            "question": "What quantity of controlled substance was found? What was the packaging (personal use vs. distribution indicators)? Compare to Nevada trafficking thresholds under NRS 453.3385."
        },
    ],
    "Assault": [
        {
            "check_type": "stop_justification",
            "label": "Probable cause for arrest documented",
            "question": "What is the stated probable cause for the arrest? Was there a warrant, or was it a warrantless arrest? If warrantless, what exception applies?"
        },
        {
            "check_type": "miranda_documentation",
            "label": "Miranda rights documented and acknowledged",
            "question": "Was the Miranda warning administered? Were any statements made by the defendant? Is written acknowledgment documented?"
        },
        {
            "check_type": "victim_statement_consistency",
            "label": "Victim statement consistency across documents",
            "question": "What does the victim's statement say? Are there any inconsistencies between the victim's statement, witness statements, and the arrest report narrative? Note any discrepancies in timeline, location, or description of events."
        },
        {
            "check_type": "physical_evidence",
            "label": "Physical evidence documented and preserved",
            "question": "What physical evidence was collected (photos, medical records, weapons)? Is there a chain of custody for each item? Was medical attention sought or documented?"
        },
    ],
    "Murder": [
        {
            "check_type": "probable_cause",
            "label": "Probable cause for arrest documented",
            "question": "Does the arrest report clearly establish probable cause for the arrest? What specific facts are cited?",
            "legal_basis": "4th Amendment"
        },
        {
            "check_type": "miranda_timing",
            "label": "Miranda rights timing and documentation",
            "question": "When was Miranda administered relative to arrest time? Was there a gap? Were any statements made by the defendant BEFORE Miranda was read? Was a written waiver obtained?",
            "legal_basis": "5th/6th Amendment — Miranda v. Arizona"
        },
        {
            "check_type": "search_warrant",
            "label": "Search warrant obtained or valid exception",
            "question": "Was a search warrant obtained before any searches? If exigent circumstances or other warrant exceptions were claimed, are the specific facts supporting the exception clearly documented?",
            "legal_basis": "4th Amendment"
        },
        {
            "check_type": "chain_of_custody",
            "label": "Chain of custody for physical evidence",
            "question": "Is the chain of custody for all physical evidence (weapons, clothing, DNA, etc.) complete and unbroken? Are there any gaps in handling or signatures?",
            "legal_basis": "NRS 51.075 — Nevada Rules of Evidence"
        },
        {
            "check_type": "forensic_review",
            "label": "Autopsy/forensic report reviewed for defensive anomalies",
            "question": "Does the ME or forensic report contain any findings that support the defendant's version of events — defensive wounds, self-defense indicators, alternative explanations for evidence?",
            "legal_basis": "Nevada Rules of Evidence"
        },
        {
            "check_type": "witness_consistency",
            "label": "Witness statement consistency across documents",
            "question": "Are witness statements internally consistent? Do timelines match across multiple witness accounts? Are there any prior relationships, biases, or inconsistencies that affect credibility?",
            "legal_basis": "Brady v. Maryland — material impeachment evidence"
        },
        {
            "check_type": "prior_bad_acts",
            "label": "Prior bad acts evidence disclosed",
            "question": "Has the prosecution disclosed any prior bad acts evidence they intend to introduce? Have proper NRS 48.045 notice requirements been met?",
            "legal_basis": "NRS 48.045 — Evidence of other crimes"
        },
        {
            "check_type": "speedy_arraignment",
            "label": "Right to speedy arraignment — timeline documented",
            "question": "How much time elapsed between arrest and formal booking/arraignment? Any unreasonable delay could support a 6th Amendment challenge.",
            "legal_basis": "6th Amendment / NRS 178.556"
        },
    ],
}

# ─── RAG Query ────────────────────────────────────────────────────────────────

def rag_query(question: str, case_id: str, top_k: int = 8) -> list[dict]:
    """
    Embed question, retrieve top-k relevant chunks from Supabase.
    Returns list of chunk dicts with content + metadata.
    """
    openai_c = _get_openai()
    supabase = _get_supabase()
    response = openai_c.embeddings.create(
        model=EMBEDDING_MODEL,
        input=question
    )
    query_embedding = response.data[0].embedding

    result = supabase.rpc("match_chunks", {
        "query_embedding": query_embedding,
        "target_case_id": case_id,
        "match_count": top_k,
        "similarity_threshold": 0.25
    }).execute()

    return result.data if result.data else []

# ─── Single Check ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a meticulous criminal defense paralegal AI assistant working for the Law Offices of Garrett T. Ogata in Las Vegas, Nevada.

Your job is to analyze case documents and answer specific legal checklist questions.

Rules you must follow:
1. ONLY use information from the provided document excerpts. Never invent facts.
2. Always cite the specific document and location (page, section) where you found each fact.
3. If information is NOT present in the documents, say exactly that — "Not found in available documents."
4. Flag any procedural irregularities, missing documentation, or potential defense arguments.
5. Be precise about times, dates, names, and numerical values.
6. At the end, provide a confidence score (0-100) based on how clearly the documents answer the question.
7. If you identify a potential suppression argument or defense issue, mark it with ⚠️ PRIORITY.

Format your response as JSON with these exact keys:
{
  "answer": "Your detailed analysis here",
  "source_excerpts": [
    {"doc_name": "...", "excerpt": "...", "relevance": "why this excerpt matters"}
  ],
  "priority_flag": true/false,
  "priority_reason": "...",
  "confidence": 0-100,
  "not_found": true/false
}"""


def run_check(check: dict, case_id: str) -> dict:
    """Run a single check against the RAG and return a finding dict."""
    claude = _get_claude()
    # 1. Retrieve relevant chunks
    chunks = rag_query(check["question"], case_id)

    if not chunks:
        return {
            "case_id": case_id,
            "check_type": check["check_type"],
            "label": check["label"],
            "ai_answer": "No relevant documents found for this check. Documents may not be indexed yet.",
            "source_chunk_ids": [],
            "source_excerpts": [],
            "confidence": 0.0,
            "priority_flag": False,
            "hil_status": None,
        }

    # 2. Build context from retrieved chunks
    context_parts = []
    chunk_ids = []
    for i, chunk in enumerate(chunks):
        meta = chunk.get("metadata", {})
        context_parts.append(
            f"[EXCERPT {i+1}] Source: {meta.get('doc_name', 'Unknown')} "
            f"(Page {meta.get('page', '?')}, Type: {meta.get('doc_type', '?')})\n"
            f"{chunk['content']}"
        )
        chunk_ids.append(chunk["id"])

    context = "\n\n---\n\n".join(context_parts)

    # 3. Ask Claude
    message = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"QUESTION: {check['question']}\n\nDOCUMENT EXCERPTS:\n{context}"
        }]
    )

    raw = message.content[0].text.strip()

    # 4. Parse JSON response
    try:
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
            raw = raw.rsplit("```", 1)[0]
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = {
            "answer": raw,
            "source_excerpts": [],
            "priority_flag": False,
            "priority_reason": "",
            "confidence": 50,
            "not_found": False
        }

    return {
        "case_id": case_id,
        "check_type": check["check_type"],
        "label": check["label"],
        "ai_answer": parsed.get("answer", ""),
        "source_chunk_ids": chunk_ids,
        "source_excerpts": json.dumps(parsed.get("source_excerpts", [])),
        "confidence": parsed.get("confidence", 50) / 100.0,  # store as 0-1
        "priority_flag": parsed.get("priority_flag", False),
        "hil_status": None,  # HIL: human must review
    }

# ─── Full Case Analysis ───────────────────────────────────────────────────────

def analyze_case(case_id: str, case_type: str = "DUI") -> list[dict]:
    """
    Run all checks for a case type. Returns list of finding dicts.
    Stores findings in Supabase and logs to audit_log.
    """
    supabase = _get_supabase()
    checks = CHECKS.get(case_type, CHECKS["DUI"])
    run_id = str(uuid.uuid4())

    console.print(f"\n[bold]Analyzing case {case_id}[/bold] ({case_type}) — {len(checks)} checks")

    findings = []
    for i, check in enumerate(checks):
        console.print(f"  [{i+1}/{len(checks)}] {check['label']}...", end=" ")

        finding = run_check(check, case_id)
        finding["run_id"] = run_id

        # Store in Supabase
        insert_data = {k: v for k, v in finding.items() if k != "source_chunk_ids"}
        insert_data["id"] = str(uuid.uuid4())
        # Convert source_chunk_ids to proper format if needed
        supabase.table("findings").insert(insert_data).execute()

        # Console output
        pct = round(finding["confidence"] * 100)
        flag = " ⚠️" if finding["priority_flag"] else ""
        color = "green" if pct >= 80 else "yellow" if pct >= 60 else "red"
        console.print(f"[{color}]{pct}% conf[/{color}]{flag}")

        findings.append(finding)

    # Log analysis run to audit
    supabase.table("audit_log").insert({
        "case_id": case_id,
        "action": "ANALYSIS_RUN",
        "actor": "system",
        "actor_name": "Analysis Agent",
        "note": f"Ran {len(findings)} checks for {case_type} case",
        "metadata": {
            "run_id": run_id,
            "case_type": case_type,
            "check_count": len(findings),
            "priority_flags": sum(1 for f in findings if f["priority_flag"])
        }
    }).execute()

    priority_count = sum(1 for f in findings if f["priority_flag"])
    console.print(f"\n[bold green]✓ Analysis complete[/bold green] — {len(findings)} findings, {priority_count} priority flags")
    console.print(f"  Run ID: {run_id}")

    return findings

# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LegalAI Case Analysis Agent")
    parser.add_argument("--case-id",   required=True, help="Case ID (e.g. GTO-2024-001)")
    parser.add_argument("--case-type", default="DUI",
                        help="Case type: DUI | Drug | Assault | Murder (default: DUI)")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Print checks without running analysis")
    args = parser.parse_args()

    if args.dry_run:
        checks = CHECKS.get(args.case_type, CHECKS["DUI"])
        console.print(f"\n[bold]Checks for {args.case_type} ({len(checks)} total):[/bold]")
        for i, c in enumerate(checks):
            console.print(f"  {i+1}. [{c['check_type']}] {c['label']}")
    else:
        findings = analyze_case(args.case_id, args.case_type)
