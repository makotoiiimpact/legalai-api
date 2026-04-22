"""Generate three additional test complaints for the Ogata demo.

  test_case_3.pdf — Drug possession, Judge Jones, DDA Walsh
  test_case_4.pdf — Domestic violence, Judge Wiese, DDA Schwartz
  test_case_5.pdf — Second DUI, Judge Jones, DDA Rodriguez

Used together with test_complaint.pdf + test_complaint_2.pdf these
give the Cases List 5 Tier-1 cases across 3 judges + 4 prosecutors.

Usage:
    python scripts/create_test_cases_batch.py [output_dir]

Default output dir is the current working directory.
"""

import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
except ImportError:
    sys.exit("reportlab not installed. Run: pip install reportlab")


@dataclass
class Charge:
    description: str
    statute: str


@dataclass
class TestCase:
    file_name: str
    case_number: str
    department: str  # Roman numeral
    filed_label: str  # "January 12, 2024"
    defendant: str
    prosecutor: str
    judge: str
    charges: list[Charge] = field(default_factory=list)


def draw_case(tc: TestCase, out_dir: Path) -> Path:
    out_path = out_dir / tc.file_name
    c = canvas.Canvas(str(out_path), pagesize=letter)

    c.setFont("Helvetica-Bold", 14)
    c.drawString(200, 750, "DISTRICT COURT")
    c.drawString(180, 732, "CLARK COUNTY, NEVADA")

    c.setFont("Helvetica", 11)
    c.drawString(72, 696, f"CASE NO.: {tc.case_number}")
    c.drawString(72, 680, f"DEPT. NO.: {tc.department}")
    c.drawString(72, 664, f"Filed: {tc.filed_label}")

    c.drawString(72, 628, "STATE OF NEVADA,")
    c.drawString(108, 612, "Plaintiff,")
    c.drawString(72, 596, "vs.")
    c.drawString(72, 580, f"{tc.defendant},")
    c.drawString(108, 564, "Defendant.")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(240, 520, "CRIMINAL COMPLAINT")

    c.setFont("Helvetica", 11)
    c.drawString(72, 488, f"The undersigned, {tc.prosecutor}, Deputy District Attorney,")
    c.drawString(72, 472, "Clark County, Nevada, hereby charges the above-named")
    c.drawString(72, 456, "defendant with the following offenses:")

    # Stack charges down the page. Each count takes ~48 pts.
    y = 420
    roman = ["I", "II", "III", "IV", "V"]
    for i, ch in enumerate(tc.charges):
        c.setFont("Helvetica-Bold", 11)
        c.drawString(72, y, f"COUNT {roman[i]}: {ch.description}")
        c.setFont("Helvetica", 11)
        c.drawString(72, y - 16, f"({ch.statute})")
        y -= 48

    y = min(y, 264)  # leave room for judge + attorney blocks
    c.drawString(72, y, f"The Honorable {tc.judge}, District Judge,")
    c.drawString(72, y - 16, f"Department {tc.department}, presiding.")

    c.drawString(72, y - 56, "Attorney for Defendant: GARRETT T. OGATA, ESQ.")
    c.drawString(72, y - 72, "Nevada Bar No. 7469")

    c.save()
    return out_path


CASES = [
    TestCase(
        file_name="test_case_3.pdf",
        case_number="A-24-556789-C",
        department="IX",
        filed_label="January 12, 2024",
        defendant="MARIA ELENA GUTIERREZ",
        prosecutor="JESSICA WALSH",
        judge="TIERRA JONES",
        charges=[
            Charge("Possession of Controlled Substance", "NRS 453.336"),
            Charge("Possession of Drug Paraphernalia", "NRS 453.566"),
        ],
    ),
    TestCase(
        file_name="test_case_4.pdf",
        case_number="A-23-334455-C",
        department="XXVI",
        filed_label="November 3, 2023",
        defendant="ROBERT ALLEN THOMPSON",
        prosecutor="DAVID SCHWARTZ",
        judge="JERRY WIESE",
        charges=[
            Charge("Battery Constituting Domestic Violence", "NRS 200.485"),
            Charge("Coercion", "NRS 207.190"),
        ],
    ),
    TestCase(
        file_name="test_case_5.pdf",
        case_number="A-24-778899-C",
        department="IX",
        filed_label="March 22, 2024",
        defendant="ANTHONY WAYNE BROOKS",
        prosecutor="MICHAEL RODRIGUEZ",
        judge="TIERRA JONES",
        charges=[
            Charge("Driving Under the Influence", "NRS 484C.110"),
            Charge("Speeding in Excess of Posted Limit", "NRS 484B.600"),
            Charge("Failure to Obey Traffic Control Device", "NRS 484B.300"),
        ],
    ),
]


if __name__ == "__main__":
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    for tc in CASES:
        p = draw_case(tc, out_dir)
        print(f"Wrote {p.resolve()}")
