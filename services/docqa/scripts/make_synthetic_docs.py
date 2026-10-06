"""Generate the synthetic sample corpus (fake people, fake institutions).

Used by tests, CI and evals so nothing ever touches real personal documents.
    uv run python scripts/make_synthetic_docs.py
"""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT = Path(__file__).resolve().parent.parent / "samples" / "synthetic"
BANNER = "SYNTHETIC SAMPLE - NOT A REAL RECORD"
STYLES = getSampleStyleSheet()

COURSES = [
    ("Fall 2017", "CS 101", "Introduction to Programming", "4", "A"),
    ("Fall 2017", "MATH 151", "Calculus I", "4", "A-"),
    ("Spring 2018", "CS 201", "Data Structures", "4", "A"),
    ("Spring 2018", "STAT 210", "Probability and Statistics", "3", "B+"),
    ("Fall 2018", "CS 310", "Algorithms", "4", "A"),
    ("Fall 2018", "CS 340", "Database Systems", "3", "A-"),
    ("Spring 2019", "CS 420", "Machine Learning", "4", "A"),
    ("Spring 2019", "CS 495", "Senior Capstone Project", "3", "A"),
]


def transcript(path: Path) -> None:
    doc = SimpleDocTemplate(str(path), pagesize=letter, title="Official Transcript")
    rows = [("Term", "Course", "Title", "Credits", "Grade"), *COURSES]
    table = Table(rows, repeatRows=1)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey)]))
    doc.build(
        [
            Paragraph(BANNER, STYLES["Italic"]),
            Paragraph("Northfield State University", STYLES["Title"]),
            Paragraph("Official Academic Transcript", STYLES["Heading2"]),
            Paragraph(
                "Student: Alex Rivera. Student ID: NSU-0042-7781. Program: Bachelor of Science "
                "in Computer Science. Date issued: June 14, 2019.",
                STYLES["Normal"],
            ),
            Spacer(1, 12),
            table,
            Spacer(1, 12),
            Paragraph(
                "Total credits earned: 29. Cumulative GPA: 3.86 on a 4.00 scale. "
                "Academic standing: Dean's List, Spring 2019.",
                STYLES["Normal"],
            ),
        ]
    )


def certificate(path: Path) -> None:
    doc = SimpleDocTemplate(str(path), pagesize=letter, title="Diploma")
    doc.build(
        [
            Paragraph(BANNER, STYLES["Italic"]),
            Paragraph("Northfield State University", STYLES["Title"]),
            Paragraph(
                "Upon the recommendation of the faculty, the Board of Trustees has conferred on "
                "Alex Rivera the degree of Bachelor of Science in Computer Science, with all the "
                "rights and privileges thereto, magna cum laude.",
                STYLES["Normal"],
            ),
            Paragraph("Conferred on May 25, 2019, at Northfield, Ohio.", STYLES["Normal"]),
        ]
    )


def text_image(lines: list[str], size: tuple[int, int] = (1275, 1650)) -> Image.Image:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=34)
    y = 120
    for line in lines:
        draw.text((110, y), line, fill="black", font=font)
        y += 60
    return image


AWARD_LINES = [
    BANNER,
    "",
    "Brightwater Foundation",
    "Scholarship Award Letter",
    "",
    "Date: August 2, 2017",
    "Dear Alex Rivera,",
    "We are pleased to award you the Brightwater STEM Scholarship",
    "in the amount of $4,000 per academic year for four years,",
    "renewable while you maintain a cumulative GPA of 3.0 or higher.",
    "",
    "Sincerely, Morgan Ellis, Director of Scholarships",
]


def scanned_letter(path: Path) -> None:
    """An image-only PDF: no text layer, so ingestion must use the vision model."""
    image = text_image(AWARD_LINES)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    from reportlab.pdfgen import canvas  # noqa: PLC0415

    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.drawImage(ImageReader(io.BytesIO(buf.getvalue())), 0, 0, *letter)
    pdf.save()


def membership_card(path: Path) -> None:
    lines = [
        BANNER,
        "",
        "Association for Computing Practitioners",
        "Student Member: Alex Rivera",
        "Member No. ACP-559102",
        "Valid through: December 31, 2019",
    ]
    text_image(lines, size=(1200, 520)).save(path, format="PNG")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    transcript(OUT / "transcript_northfield_state.pdf")
    certificate(OUT / "degree_certificate_northfield_state.pdf")
    scanned_letter(OUT / "scholarship_award_letter_scanned.pdf")
    membership_card(OUT / "acp_membership_card.png")
    for path in sorted(OUT.iterdir()):
        print(f"{path.stat().st_size:>8}  {path.name}")


if __name__ == "__main__":
    main()
