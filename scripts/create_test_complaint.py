"""Generate a test criminal complaint PDF for exercising run_extraction.

Usage:
    python scripts/create_test_complaint.py [output_path]

Requires reportlab. If missing, install with:
    pip install reportlab

The PDF content is a minimally plausible Nevada DUI complaint with the
entities Tier 1 extraction should pick up:
- Case number, court, dept, filed date
- Judge, prosecutor, defense attorney, defendant
- Two charges with NRS statutes
"""

import sys
from pathlib import Path

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
except ImportError:
    sys.exit("reportlab not installed. Run: pip install reportlab")


def build(output_path: Path):
    c = canvas.Canvas(str(output_path), pagesize=letter)

    c.setFont("Helvetica-Bold", 14)
    c.drawString(200, 750, "DISTRICT COURT")
    c.drawString(180, 732, "CLARK COUNTY, NEVADA")

    c.setFont("Helvetica", 11)
    c.drawString(72, 696, "CASE NO.: A-24-901234-C")
    c.drawString(72, 680, "DEPT. NO.: XIV")
    c.drawString(72, 664, "Filed: March 15, 2024")

    c.drawString(72, 628, "STATE OF NEVADA,")
    c.drawString(108, 612, "Plaintiff,")
    c.drawString(72, 596, "vs.")
    c.drawString(72, 580, "CARLOS MARTINEZ,")
    c.drawString(108, 564, "Defendant.")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(240, 520, "CRIMINAL COMPLAINT")

    c.setFont("Helvetica", 11)
    c.drawString(72, 488, "The undersigned, SARAH CHEN, Deputy District Attorney,")
    c.drawString(72, 472, "Clark County, Nevada, hereby charges the above-named")
    c.drawString(72, 456, "defendant with the following offenses:")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(72, 420, "COUNT I: Driving Under the Influence, First Offense")
    c.setFont("Helvetica", 11)
    c.drawString(72, 404, "(NRS 484C.110)")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(72, 372, "COUNT II: Failure to Maintain Travel Lane")
    c.setFont("Helvetica", 11)
    c.drawString(72, 356, "(NRS 484B.223)")

    c.drawString(72, 312, "The Honorable WILLIAM KEPHART, District Judge,")
    c.drawString(72, 296, "Department XIV, presiding.")

    c.drawString(72, 256, "Attorney for Defendant: GARRETT T. OGATA, ESQ.")
    c.drawString(72, 240, "Nevada Bar No. 7469")

    c.save()


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("test_complaint.pdf")
    build(out)
    print(f"Wrote {out.resolve()}")
