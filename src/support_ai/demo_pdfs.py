"""Generate a deterministic fictional policy PDF; no network or API key needed."""

import argparse
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak

PAGES = (
    ("Returns and refunds", (
        ("Return window", "Customers may request a return within 30 calendar days of delivery. "
         "Eligibility is measured from the delivery date, not the order date. If the delivery date "
         "is unknown, support must obtain it before deciding eligibility."),
        ("Condition and exclusions", "Non-defective products must include original accessories and "
         "be in resalable condition. Digital downloads and gift cards are non-refundable. "
         "Faulty physical products may be returned within the same 30-day window even if opened."),
        ("Refund processing", "After a returned item passes inspection, the refund is issued to the "
         "original payment method within 5 to 10 business days. A previously refunded order is "
         "not eligible for a second refund. Support must verify order and return status."),
    )),
    ("Shipping and delivery", (
        ("Delivery estimates", "Standard shipping normally takes 3 to 7 business days after dispatch. "
         "Express shipping normally takes 1 to 2 business days. Estimates are not guarantees."),
        ("Delayed or missing shipments", "If tracking has not changed for 5 business days, support "
         "should open a carrier investigation. Confirm the shipping address and tracking number "
         "before promising a replacement. A shipped status alone does not establish delivery."),
        ("Damaged deliveries", "Report visible shipping damage within 7 calendar days of delivery. "
         "Keep packaging and provide photos so support can investigate with the carrier."),
    )),
    ("Technical support and warranty", (
        ("Support response targets", "Basic customers receive an initial response within 2 business "
         "days. Pro customers receive an initial response within 1 business day. Enterprise "
         "customers receive an initial response within 4 business hours. These are response "
         "targets, not guaranteed resolution times."),
        ("Hardware warranty", "Physical hardware carries a 12-month limited warranty from delivery "
         "for manufacturing defects. Accidental damage and unauthorized modifications are "
         "excluded. Warranty claims require proof of purchase and diagnostic review."),
        ("Escalation", "Escalate unresolved recurring technical problems after two documented "
         "troubleshooting attempts. Do not infer that a refund or warranty replacement is "
         "approved from the existence of a support ticket."),
    )),
)


def generate_demo_pdf(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    styles["Title"].textColor = colors.HexColor("#17324D")
    styles["BodyText"].leading = 16
    story = []
    for page_index, (title, sections) in enumerate(PAGES):
        if page_index:
            story.append(PageBreak())
        story.extend([Paragraph("NORTHSTAR TECH - FICTIONAL DEMO", styles["Heading3"]),
                      Paragraph(title, styles["Title"]),
                      Paragraph("Assessment fixture | Version 1.0 | Effective January 1, 2025", styles["BodyText"]),
                      Spacer(1, 24)])
        for heading, body in sections:
            story.extend([Paragraph(heading, styles["Heading2"]),
                          Paragraph(body, styles["BodyText"]), Spacer(1, 16)])

    def footer(canvas, doc):
        canvas.setFont("Helvetica", 9)
        canvas.setFillColor(colors.HexColor("#526170"))
        canvas.drawString(48, 35, "Synthetic company policies for demonstration only.")
        canvas.drawRightString(A4[0] - 48, 35, f"Page {doc.page}")

    SimpleDocTemplate(str(path), pagesize=A4, rightMargin=48, leftMargin=48,
                      topMargin=48, bottomMargin=60, invariant=1,
                      title="Northstar Tech - Fictional Support Policies",
                      author="Customer Support Demo").build(story, onFirstPage=footer, onLaterPages=footer)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/pdf/demo_policies.pdf"))
    args = parser.parse_args()
    generate_demo_pdf(args.output)
    print(f"Created fictional demo policies: {args.output}")


if __name__ == "__main__":
    main()
