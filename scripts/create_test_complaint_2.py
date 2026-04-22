"""Second test criminal complaint — State v. Davis.

Shares Judge Kephart + attorney Ogata with test_complaint.pdf so the demo
shows Garrett appearing before Kephart on two cases. Different defendant,
different prosecutor, more counts.

Usage:
    python scripts/create_test_complaint_2.py [output_path]
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
    c.drawString(72, 696, "CASE NO.: A-23-887654-C")
    c.drawString(72, 680, "DEPT. NO.: XIV")
    c.drawString(72, 664, "Filed: September 8, 2023")

    c.drawString(72, 628, "STATE OF NEVADA,")
    c.drawString(108, 612, "Plaintiff,")
    c.drawString(72, 596, "vs.")
    c.drawString(72, 580, "JAMES MICHAEL DAVIS,")
    c.drawString(108, 564, "Defendant.")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(240, 520, "CRIMINAL COMPLAINT")

    c.setFont("Helvetica", 11)
    c.drawString(72, 488, "The undersigned, MICHAEL RODRIGUEZ, Deputy District Attorney,")
    c.drawString(72, 472, "Clark County, Nevada, hereby charges the above-named")
    c.drawString(72, 456, "defendant with the following offenses:")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(72, 420, "COUNT I: Driving Under the Influence, Second Offense")
    c.setFont("Helvetica", 11)
    c.drawString(72, 404, "(NRS 484C.400)")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(72, 372, "COUNT II: Reckless Driving")
    c.setFont("Helvetica", 11)
    c.drawString(72, 356, "(NRS 484B.653)")

    c.setFont("Helvetica-Bold", 11)
    c.drawString(72, 324, "COUNT III: Open Container in Vehicle")
    c.setFont("Helvetica", 11)
    c.drawString(72, 308, "(NRS 484B.150)")

    c.drawString(72, 264, "The Honorable WILLIAM KEPHART, District Judge,")
    c.drawString(72, 248, "Department XIV, presiding.")

    c.drawString(72, 208, "Attorney for Defendant: GARRETT T. OGATA, ESQ.")
    c.drawString(72, 192, "Nevada Bar No. 7469")

    c.save()


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("test_complaint_2.pdf")
    build(out)
    print(f"Wrote {out.resolve()}")
