"""Generate the synthetic sample corpus (fake people, fake institutions).

Used by tests, CI and evals so nothing ever touches real personal documents. The corpus is
one person's archive with deliberate look-alikes (two transcripts that both list a Machine
Learning course, two scholarship letters, two degree certificates), so retrieval strategies
can actually be told apart. The eval golden set (evals/golden/synthetic.jsonl) is written
against this exact text: change both together.
    uv run python scripts/make_synthetic_docs.py
"""

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Flowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

rl_config.invariant = 1  # no timestamps or random IDs: regenerating gives identical bytes

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


def text_pdf(path: Path, title: str, heading: str, paragraphs: list[str]) -> None:
    """A one-page text-layer PDF: a banner, an institution heading, then paragraphs."""
    story: list[Flowable] = [
        Paragraph(BANNER, STYLES["Italic"]),
        Paragraph(heading, STYLES["Title"]),
    ]
    for text in paragraphs:
        story.append(Paragraph(text, STYLES["Normal"]))
        story.append(Spacer(1, 10))
    SimpleDocTemplate(str(path), pagesize=letter, title=title).build(story)


MS_COURSES = [
    ("Fall 2019", "DS 501", "Foundations of Data Science", "3", "A"),
    ("Fall 2019", "DS 510", "Statistical Learning", "3", "A-"),
    ("Fall 2019", "DS 520", "Data Engineering", "3", "A"),
    ("Spring 2020", "DS 530", "Machine Learning", "3", "B+"),
    ("Spring 2020", "DS 540", "Natural Language Processing", "3", "A"),
    ("Spring 2020", "DS 550", "Data Visualization", "3", "A"),
    ("Fall 2020", "DS 610", "Deep Learning", "3", "A"),
    ("Fall 2020", "DS 620", "Cloud Computing for Analytics", "3", "A-"),
    ("Spring 2021", "DS 690", "Master's Thesis", "6", "A"),
]


def ms_transcript(path: Path) -> None:
    """Two pages: the course table, then the thesis and summary on page 2."""
    doc = SimpleDocTemplate(str(path), pagesize=letter, title="Graduate Transcript")
    table = Table([("Term", "Course", "Title", "Credits", "Grade"), *MS_COURSES], repeatRows=1)
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey)]))
    doc.build(
        [
            Paragraph(BANNER, STYLES["Italic"]),
            Paragraph("Lakeshore Institute of Technology", STYLES["Title"]),
            Paragraph("Graduate Academic Transcript", STYLES["Heading2"]),
            Paragraph(
                "Student: Alex Rivera. Student ID: LIT-2019-30418. Program: Master of Science "
                "in Data Science. Date issued: June 1, 2021.",
                STYLES["Normal"],
            ),
            Spacer(1, 12),
            table,
            PageBreak(),
            Paragraph("Thesis", STYLES["Heading2"]),
            Paragraph(
                "Thesis title: Forecasting Urban Water Demand with Gradient Boosting. "
                "Thesis advisor: Dr. Priya Raman. Defended on April 22, 2021.",
                STYLES["Normal"],
            ),
            Spacer(1, 12),
            Paragraph(
                "Total graduate credits earned: 30. Cumulative graduate GPA: 3.72 on a 4.00 "
                "scale. Graduate assistantship: Data Science Lab, 2020-2021.",
                STYLES["Normal"],
            ),
        ]
    )


TEXT_DOCUMENTS: dict[str, tuple[str, str, list[str]]] = {
    "degree_certificate_lakeshore_tech_ms.pdf": (
        "Diploma",
        "Lakeshore Institute of Technology",
        [
            "The Trustees of Lakeshore Institute of Technology, on the recommendation of the "
            "Graduate Faculty, have conferred on Alex Rivera the degree of Master of Science in "
            "Data Science, with distinction.",
            "Conferred on May 14, 2021, at Milwaukee, Wisconsin.",
        ],
    ),
    "high_school_diploma_cedar_grove.pdf": (
        "High School Diploma",
        "Cedar Grove High School",
        [
            "This certifies that Alex Rivera has satisfactorily completed the course of study "
            "prescribed for graduation and is awarded this High School Diploma with honors.",
            "Graduation date: June 10, 2015. Cedar Grove, Ohio. Class rank: 12 of 284.",
        ],
    ),
    "offer_letter_harborview_analytics.pdf": (
        "Offer Letter",
        "Harborview Analytics",
        [
            "Date: June 2, 2021",
            "Dear Alex Rivera,",
            "We are delighted to offer you the position of Data Analyst in our Columbus, Ohio "
            "office, reporting to Jordan Blake, Analytics Manager.",
            "Your starting annual base salary will be $78,500, paid semi-monthly, with a signing "
            "bonus of $5,000. Your expected start date is July 12, 2021.",
            "You will be eligible for 20 days of paid time off per year and the company 401(k) "
            "plan with a 4 percent employer match.",
            "Sincerely, Taylor Morgan, Head of People",
        ],
    ),
    "employment_verification_harborview.pdf": (
        "Employment Verification",
        "Harborview Analytics",
        [
            "Date: March 3, 2024",
            "To whom it may concern,",
            "This letter confirms that Alex Rivera has been employed full-time by Harborview "
            "Analytics since July 12, 2021. Alex joined as a Data Analyst and has held the title "
            "Senior Data Analyst since January 1, 2023.",
            "Alex's current annual base salary is $96,000.",
            "Sincerely, Taylor Morgan, Head of People",
        ],
    ),
    "certificate_cloud_foundations.pdf": (
        "Certificate",
        "Skyline Cloud Academy",
        [
            "Certificate of Achievement",
            "Alex Rivera has passed the Cloud Practitioner Foundations examination.",
            "Credential ID: SCA-CPF-88213. Issued: September 9, 2022. Valid until: September 9, "
            "2025.",
        ],
    ),
    "certificate_advanced_sql.pdf": (
        "Certificate",
        "Ridgeway Online Learning",
        [
            "Certificate of Completion",
            "Alex Rivera has completed the course Advanced SQL for Analysts (24 hours), "
            "covering window functions, query optimization and data modeling.",
            "Completed: October 18, 2020. Instructor: Sam Whitfield.",
        ],
    ),
    "english_proficiency_score_report.pdf": (
        "Score Report",
        "International English Assessment",
        [
            "Test Report Form. Candidate: Alex Rivera. Test date: March 2, 2019. "
            "Test centre: Cleveland, Ohio.",
            "Overall band score: 8.0. Listening: 8.5. Reading: 8.0. Writing: 7.0. Speaking: 7.5.",
            "Results are valid for two years from the test date.",
        ],
    ),
    "recommendation_letter_prof_okafor.pdf": (
        "Recommendation Letter",
        "Northfield State University, Department of Computer Science",
        [
            "Date: December 3, 2018",
            "To the Graduate Admissions Committee,",
            "I am writing to recommend Alex Rivera for your Master of Science in Data Science "
            "program. Alex took my Algorithms course and led our senior capstone project, "
            "Predicting River Flooding with Low-Cost Sensor Data.",
            "Alex ranks among the top five percent of students I have taught in fifteen years.",
            "Sincerely, Professor Dana Okafor",
        ],
    ),
    "internship_completion_pinecrest.pdf": (
        "Internship Certificate",
        "Pinecrest Software",
        [
            "Certificate of Internship Completion",
            "Alex Rivera completed a summer internship as a Software Engineering Intern on the "
            "Payments team from June 4, 2018 to August 17, 2018.",
            "Supervisor: Chris Nakamura, Engineering Manager.",
        ],
    ),
    "scholarship_renewal_brightwater.pdf": (
        "Scholarship Renewal",
        "Brightwater Foundation",
        [
            "Date: July 20, 2018",
            "Dear Alex Rivera,",
            "Congratulations: your Brightwater STEM Scholarship has been renewed for the "
            "2018-2019 academic year at $4,000.",
            "In recognition of your academic record, you also receive a one-time conference "
            "travel grant of $500.",
            "Sincerely, Morgan Ellis, Director of Scholarships",
        ],
    ),
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    transcript(OUT / "transcript_northfield_state.pdf")
    certificate(OUT / "degree_certificate_northfield_state.pdf")
    scanned_letter(OUT / "scholarship_award_letter_scanned.pdf")
    membership_card(OUT / "acp_membership_card.png")
    ms_transcript(OUT / "transcript_lakeshore_tech_ms.pdf")
    for name, (title, heading, paragraphs) in TEXT_DOCUMENTS.items():
        text_pdf(OUT / name, title, heading, paragraphs)
    for path in sorted(OUT.iterdir()):
        print(f"{path.stat().st_size:>8}  {path.name}")


if __name__ == "__main__":
    main()
