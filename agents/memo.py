"""
LegalAI — Disposition Memo Generator
======================================
Takes all HIL-reviewed findings for a case and drafts
a structured disposition memo for attorney review.

This is the FINAL step before Garrett sees it.
Nothing is filed or acted on without his sign-off.

Usage:
  python agents/memo.py --case-id GTO-2024-001
"""

import os
import json
import argparse
from datetime import datetime, timezone

import anthropic
from supabase import create_client
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
console = Console()

claude   = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_KEY"]
)

CLAUDE_MODEL = "claude-sonnet-4-20250514"

MEMO_SYSTEM_PROMPT = """You are drafting a legal disposition memo for attorney Garrett T. Ogata, a criminal defense attorney in Las Vegas, Nevada with 20+ years of experience.

You will be given:
1. Case details
2. A set of reviewed findings (confirmed and edited by a paralegal)
3. Any priority flags

Your job is to produce a clear, structured disposition memo that Garrett can quickly review and approve.

The memo must include:
1. CASE SUMMARY — one paragraph, just the facts
2. EVIDENCE ASSESSMENT — what we have, what's missing, strength of state's case
3. PROCEDURAL ISSUES — any violations of defendant's rights, suppression opportunities
4. LEGAL RESEARCH SUMMARY — relevant Nevada statutes and precedents
5. DEFENSE STRATEGY OPTIONS — ranked from strongest to weakest
   - Option A: [strategy] — [rationale] — [risk level]
   - Option B: [strategy] — [rationale] — [risk level]
6. RECOMMENDED PATH — your single best recommendation with reasoning
7. IMMEDIATE ACTION ITEMS — what needs to happen in the next 48 hours

Be direct and specific. Garrett has seen thousands of cases — don't waste his time with filler. If a finding was edited by the paralegal, note that. If a finding was rejected, exclude it.

Format as clean markdown. No fluff."""


def get_case_details(case_id: str) -> dict:
    result = supabase.table("cases").select("*").eq("id", case_id).single().execute()
    return result.data


def get_reviewed_findings(case_id: str) -> list[dict]:
    """Get all findings that have been through HIL review."""
    result = supabase.table("findings").select("*").eq("case_id", case_id).neq("hil_status", None).execute()
    return result.data or []


def generate_memo(case_id: str) -> dict:
    """Generate disposition memo from reviewed findings."""

    # 1. Get case + findings
    case = get_case_details(case_id)
    findings = get_reviewed_findings(case_id)

    if not findings:
        console.print("[red]No reviewed findings found. Complete HIL review before generating memo.[/red]")
        return {}

    confirmed = [f for f in findings if f["hil_status"] in ("confirmed", "edited")]
    rejected  = [f for f in findings if f["hil_status"] == "rejected"]
    priority  = [f for f in confirmed if f.get("priority_flag")]

    console.print(f"\n[bold]Generating memo for {case_id}[/bold]")
    console.print(f"  Confirmed findings: {len(confirmed)}")
    console.print(f"  Rejected (excluded): {len(rejected)}")
    console.print(f"  Priority flags: {len(priority)}")

    # 2. Build findings summary for Claude
    findings_text = ""
    for f in confirmed:
        status_note = " [PARALEGAL EDITED]" if f["hil_status"] == "edited" else ""
        priority_note = " ⚠️ PRIORITY FLAG" if f.get("priority_flag") else ""
        answer = f.get("edited_answer") or f.get("ai_answer", "")
        conf_pct = round((f.get("confidence") or 0) * 100)
        findings_text += f"""
### {f['label']}{priority_note}{status_note}
Confidence: {conf_pct}%
{answer}
"""

    # 3. Build prompt
    user_prompt = f"""
CASE DETAILS:
- Case ID: {case['case_number']}
- Client: {case['client_name']}
- Charge: {case['charge']}
- Severity: {case.get('charge_severity', 'Unknown')}
- Incident Date: {case.get('incident_date', 'Unknown')}
- Jurisdiction: {case.get('jurisdiction', 'Clark County, NV')}

PARALEGAL-REVIEWED FINDINGS ({len(confirmed)} confirmed, {len(rejected)} rejected):
{findings_text}

PRIORITY FLAGS SUMMARY:
{chr(10).join(f"- {f['label']}: {(f.get('edited_answer') or f.get('ai_answer',''))[:200]}..." for f in priority) if priority else "None identified."}

Draft the disposition memo now.
"""

    # 4. Call Claude
    console.print("  [dim]→ Drafting memo with Claude...[/dim]")
    message = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=MEMO_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}]
    )
    memo_content = message.content[0].text

    # 5. Infer recommended path from memo content
    recommended_path = "plea_negotiate"  # default
    memo_lower = memo_content.lower()
    if "suppression motion" in memo_lower or "motion to suppress" in memo_lower:
        recommended_path = "suppression_motion"
    elif "trial" in memo_lower and "recommend" in memo_lower:
        recommended_path = "trial"
    elif "dismiss" in memo_lower:
        recommended_path = "dismiss"

    # 6. Store in Supabase — NOT approved yet
    memo_id = supabase.table("disposition_memos").insert({
        "case_id": case_id,
        "draft_content": memo_content,
        "recommended_path": recommended_path,
        "priority_findings": json.dumps([
            {"label": f["label"], "summary": (f.get("edited_answer") or f.get("ai_answer",""))[:300]}
            for f in priority
        ]),
        "attorney_approved": False,  # HARD GATE — must be set by attorney
    }).execute()

    # 7. Log
    supabase.table("audit_log").insert({
        "case_id": case_id,
        "action": "MEMO_GENERATED",
        "actor": "system",
        "actor_name": "Memo Agent",
        "note": f"Disposition memo drafted — routed to attorney for review",
        "metadata": {
            "recommended_path": recommended_path,
            "confirmed_findings": len(confirmed),
            "priority_flags": len(priority)
        }
    }).execute()

    # Update case status
    supabase.table("cases").update({"status": "attorney_review"}).eq("id", case_id).execute()

    console.print(f"  [bold green]✓ Memo drafted[/bold green] — recommended path: {recommended_path}")
    console.print(f"  [yellow]⚠️  Awaiting attorney approval — case status: attorney_review[/yellow]")

    return {
        "case_id": case_id,
        "recommended_path": recommended_path,
        "memo_preview": memo_content[:500] + "..."
    }


def attorney_approve(case_id: str, attorney_name: str = "G. Ogata", notes: str = ""):
    """
    Hard gate — attorney explicitly approves the memo.
    Only this function should ever set attorney_approved = True.
    """
    # Get latest draft memo
    result = supabase.table("disposition_memos").select("id").eq("case_id", case_id).eq("attorney_approved", False).order("created_at", desc=True).limit(1).execute()

    if not result.data:
        console.print("[red]No pending memo found for this case.[/red]")
        return

    memo_id = result.data[0]["id"]

    # Approve
    supabase.table("disposition_memos").update({
        "attorney_approved": True,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "approved_by": attorney_name,
        "attorney_notes": notes,
    }).eq("id", memo_id).execute()

    # Update case status
    supabase.table("cases").update({"status": "complete"}).eq("id", case_id).execute()

    # Audit log
    supabase.table("audit_log").insert({
        "case_id": case_id,
        "action": "MEMO_APPROVED",
        "actor": "attorney",
        "actor_name": attorney_name,
        "note": notes or "Approved",
        "metadata": {"memo_id": memo_id}
    }).execute()

    console.print(f"[bold green]✓ Memo approved by {attorney_name}[/bold green]")
    console.print(f"  Case {case_id} marked complete.")


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LegalAI Disposition Memo Generator")
    subparsers = parser.add_subparsers(dest="command")

    gen = subparsers.add_parser("generate", help="Generate disposition memo")
    gen.add_argument("--case-id", required=True)

    approve = subparsers.add_parser("approve", help="Attorney approval gate")
    approve.add_argument("--case-id",  required=True)
    approve.add_argument("--attorney", default="G. Ogata")
    approve.add_argument("--notes",    default="")

    args = parser.parse_args()

    if args.command == "generate":
        result = generate_memo(args.case_id)
        if result:
            console.print(f"\n[dim]Preview:[/dim]\n{result.get('memo_preview', '')}")
    elif args.command == "approve":
        attorney_approve(args.case_id, args.attorney, args.notes)
    else:
        parser.print_help()
