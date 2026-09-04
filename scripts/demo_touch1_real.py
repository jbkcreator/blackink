"""One-off DEMO: what a real Touch 1 looks like once the two missing inputs
exist — the client's approved copy (asset C2) and Dev-2's Owner Visibility
Score PDF (asset C3). NOT part of the sequencer; it calls SmtpEmailSender
directly to render a credible cold email so a human can see the finished shape.

Both the copy and the PDF here are MOCKS standing in for C2/C3 — the real ones
come from the client and Dev 2. The prospect is a fictional property-management
firm in Kakkanad (Kochi) so the recipient can picture themselves as the owner.

Run:  PYTHONPATH=. python scripts/demo_touch1_real.py
Sends via the Mandrill creds in .env.test to lesly.vj@heu.ai.
"""

import io
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv(".env.test", override=True)  # Mandrill SMTP creds → SmtpEmailSender

# ── Mock prospect (stands in for a real ingested contact) ─────────────────────
FIRM = "Backwater Property Management"
OWNER = "Lesly"
AREA = "Kakkanad"
CITY = "Kochi"
DOORS = 140
SCORE = 61
RANK = 14
SCORED_IN_CITY = 38
COVERAGE_PCT = 90

# three weakest categories, each with a named local peer that beats them
WEAKNESSES = [
    ("Review response rate", "40%", "Skyline Homes Kochi", "88%"),
    ("Separate owner-addressed page", "absent", "Nest Managers", "present"),
    ("Published after-hours contact route", "absent", "Urban Roost PM", "present"),
]

TO = "lesly.vj@heu.ai"
FROM = "noreply@forcedactionleads.com"     # Mandrill-verified sending domain
SENDING_DOMAIN = "forcedactionleads.com"
CLIENT_REPLY_TO = "hello@getblackink.com"  # in prod this is the client's own inbox


def build_ovs_pdf() -> bytes:
    """Mock of Dev-2's Owner Visibility Score one-pager (asset C3)."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    w, h = letter

    # Header
    c.setFillColorRGB(0.08, 0.08, 0.10)
    c.rect(0, h - 1.1 * inch, w, 1.1 * inch, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(0.75 * inch, h - 0.7 * inch, "Owner Visibility Score")
    c.setFont("Helvetica", 11)
    c.drawString(0.75 * inch, h - 0.95 * inch, f"{FIRM}  ·  {AREA}, {CITY}")
    c.setFont("Helvetica", 9)
    c.drawRightString(w - 0.75 * inch, h - 0.95 * inch, "Blackink · getblackink.com")

    # Score block
    y = h - 2.2 * inch
    c.setFillColorRGB(0.08, 0.08, 0.10)
    c.setFont("Helvetica-Bold", 54)
    c.drawString(0.75 * inch, y, f"{SCORE}")
    c.setFont("Helvetica", 14)
    c.drawString(1.9 * inch, y + 0.28 * inch, "/ 100")
    c.setFont("Helvetica", 10)
    c.drawString(1.9 * inch, y + 0.02 * inch, f"{COVERAGE_PCT}% data coverage")

    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(w - 0.75 * inch, y + 0.28 * inch, f"County rank  #{RANK}")
    c.setFont("Helvetica", 10)
    c.drawRightString(w - 0.75 * inch, y + 0.02 * inch, f"of {SCORED_IN_CITY} managers scored in {CITY}")

    # Divider
    y -= 0.55 * inch
    c.setStrokeColorRGB(0.85, 0.85, 0.85)
    c.line(0.75 * inch, y, w - 0.75 * inch, y)

    # Weakest categories
    y -= 0.45 * inch
    c.setFillColorRGB(0.08, 0.08, 0.10)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(0.75 * inch, y, "Your three weakest areas — and who's beating you on each")
    c.setFont("Helvetica", 10.5)
    for label, you, peer, peer_val in WEAKNESSES:
        y -= 0.34 * inch
        c.setFillColorRGB(0.75, 0.15, 0.15)
        c.drawString(0.9 * inch, y, "•")
        c.setFillColorRGB(0.08, 0.08, 0.10)
        c.drawString(1.1 * inch, y, f"{label}:  you {you}   —   {peer} {peer_val}")

    # Revenue model box
    y -= 0.7 * inch
    c.setFillColorRGB(0.96, 0.96, 0.98)
    c.rect(0.75 * inch, y - 1.15 * inch, w - 1.5 * inch, 1.15 * inch, fill=1, stroke=0)
    c.setFillColorRGB(0.08, 0.08, 0.10)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(0.9 * inch, y - 0.28 * inch, "Estimated annual revenue left on the table")
    c.setFont("Helvetica-Bold", 20)
    c.drawString(0.9 * inch, y - 0.66 * inch, "Rs. 11,80,000")
    c.setFont("Helvetica", 8.5)
    c.setFillColorRGB(0.35, 0.35, 0.35)
    c.drawString(
        0.9 * inch, y - 0.95 * inch,
        f"Estimate. Inputs: {DOORS} doors · Rs.2,000/door/mo mgmt fee · 30-mo avg owner tenure · "
        "public review-response-lag as speed proxy. Model assumptions shown; not a quote.",
    )

    c.setFont("Helvetica", 8)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawString(0.75 * inch, 0.6 * inch, "All signals are public (website, Google/Justdial reviews, listings). "
                                          "No pretext inquiries. Blackink · getblackink.com")
    c.showPage()
    c.save()
    return buf.getvalue()


def compose_copy() -> tuple[str, str]:
    """Mock of the client's approved Touch 1 copy (asset C2)."""
    subject = f"{FIRM} — your Owner Visibility rank in {CITY}"
    body = (
        f"Hi {OWNER},\n\n"
        f"I put together a quick Owner Visibility Score for {FIRM} — a 0-100 read on how "
        f"easily a property owner in {AREA} can find you, judge you, and reach you online. "
        f"Everything in it is public: your website, Google/Justdial reviews, listing pages. "
        f"No forms, no calls.\n\n"
        f"{FIRM} scored {SCORE}/100 and ranks #{RANK} of the {SCORED_IN_CITY} managers we "
        f"scored in {CITY}. The one-pager attached shows your three weakest areas and names a "
        f"local firm outscoring you on each.\n\n"
        f"Reply YES and I'll show you exactly where you rank in {AREA}, and the two fixes that "
        f"move the number most.\n\n"
        f"— Aravind\n"
        f"Blackink · getblackink.com"
    )
    return subject, body


def main() -> int:
    from config.settings import get_settings
    from src.services.email_sender import SmtpEmailSender

    s = get_settings()
    if not (s.smtp_host and s.smtp_password):
        print("ERROR: SMTP not configured — check .env.test (SMTP_USERNAME/SMTP_PASSWORD).")
        return 3

    pdf = build_ovs_pdf()
    subject, body = compose_copy()

    sender = SmtpEmailSender(
        host=s.smtp_host,
        port=s.smtp_port,
        password=s.smtp_password.get_secret_value(),
        username=s.smtp_username,
        use_tls=s.smtp_use_tls,
        reply_to=CLIENT_REPLY_TO,   # in prod: the client firm's own inbox
    )
    result = sender.send(
        from_address=FROM,
        to_address=TO,
        subject=subject,
        body=body,
        sending_domain=SENDING_DOMAIN,
        attachments=[("Owner_Visibility_Score.pdf", pdf, "application/pdf")],
    )
    print(f"demo Touch 1 sent to {TO} — subject={subject!r} message_id={result.message_id} "
          f"pdf_bytes={len(pdf)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
