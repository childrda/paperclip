"""Generate a small sample .mbox fixture for manual testing and demos.

Run:
    python scripts/generate_sample_mbox.py tests/fixtures/sample.mbox
"""

from __future__ import annotations

import argparse
import mailbox
import mimetypes
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path


def _plain_email() -> EmailMessage:
    msg = EmailMessage()
    msg["Message-ID"] = make_msgid(domain="district.example.org")
    msg["From"] = "Alice Admin <alice@district.example.org>"
    msg["To"] = "Bob Parent <bob@example.com>"
    msg["Subject"] = "Bus route change"
    msg["Date"] = formatdate(localtime=True)
    # Body deliberately contains a few PII shapes the Phase 3 detector
    # will catch — student ID (8-digit), an email, a phone number, a
    # narrative date — so the bundled walkthrough actually shows
    # proposed redactions in the UI overlay.
    msg.set_content(
        "Hello Bob,\n\n"
        "Route 42 will shift by 10 minutes starting Monday.\n"
        "If your student 82746153 needs an alternative drop-off, please reply\n"
        "to this email or call the transportation office at (571) 555-0199.\n"
        "We will revisit on April 15, 2024.\n\n"
        "-- Alice\n"
        "alice@district.example.org\n"
    )
    return msg


def _html_with_tracking() -> EmailMessage:
    msg = EmailMessage()
    msg["Message-ID"] = make_msgid(domain="district.example.org")
    msg["From"] = "Newsletter <news@district.example.org>"
    msg["To"] = "Parents <parents@example.com>"
    msg["Subject"] = "Weekly newsletter"
    msg["Date"] = formatdate(localtime=True)
    msg.set_content("Fallback plain text: weekly update.")
    msg.add_alternative(
        """
        <html><head><style>body{font-family:sans-serif}</style></head>
        <body>
          <h1>Weekly update</h1>
          <p>Click <a href="https://district.example.org/news">here</a>.</p>
          <img src="https://track.example.com/pixel.gif" width="1" height="1" />
          <script>alert('xss')</script>
          <iframe src="https://evil.example.com"></iframe>
        </body></html>
        """,
        subtype="html",
    )
    return msg


def _email_with_pdf_attachment() -> EmailMessage:
    msg = EmailMessage()
    msg["Message-ID"] = make_msgid(domain="district.example.org")
    msg["From"] = "Principal <principal@district.example.org>"
    msg["To"] = "Board <board@district.example.org>"
    msg["Subject"] = "Budget draft for 12345678"
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(
        "See attached draft budget.\n\n"
        "Note: parent contact for student 12345678 is "
        "Bob Parent <bob.parent@example.com>, phone (571) 555-0123.\n"
        "Lunch account LA456789 is overdue.\n"
    )
    fake_pdf = b"%PDF-1.4\n% Fake PDF for testing\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
    msg.add_attachment(
        fake_pdf, maintype="application", subtype="pdf", filename="budget_draft.pdf"
    )
    return msg


def _email_with_image() -> EmailMessage:
    msg = EmailMessage()
    msg["Message-ID"] = make_msgid(domain="district.example.org")
    msg["From"] = "Teacher <teacher@district.example.org>"
    msg["To"] = "Parent <parent@example.com>"
    msg["Subject"] = "Field trip photo"
    msg["Date"] = formatdate(localtime=True)
    msg.set_content("Photo attached.")
    png = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    msg.add_attachment(png, maintype="image", subtype="png", filename="trip.png")
    return msg


def _email_with_nested_eml() -> EmailMessage:
    outer = EmailMessage()
    outer["Message-ID"] = make_msgid(domain="district.example.org")
    outer["From"] = "Forwarder <fwd@district.example.org>"
    outer["To"] = "Records <records@district.example.org>"
    outer["Subject"] = "Fwd: parent complaint"
    outer["Date"] = formatdate(localtime=True)
    outer.set_content("Forwarding the original message.")

    inner = EmailMessage()
    inner["Message-ID"] = make_msgid(domain="example.com")
    inner["From"] = "Parent <parent@example.com>"
    inner["To"] = "Principal <principal@district.example.org>"
    inner["Subject"] = "Concern about recess"
    inner["Date"] = formatdate(localtime=True)
    inner.set_content("Original complaint text here.")

    outer.add_attachment(inner, filename="original.eml")
    return outer


def build(path: Path) -> int:
    mimetypes.add_type("message/rfc822", ".eml")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    box = mailbox.mbox(str(path))
    box.lock()
    try:
        for builder in (
            _plain_email,
            _html_with_tracking,
            _email_with_pdf_attachment,
            _email_with_image,
            _email_with_nested_eml,
        ):
            box.add(builder())
        box.flush()
    finally:
        box.unlock()
        box.close()
    return 5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "output",
        nargs="?",
        default="tests/fixtures/sample.mbox",
        help="Output path",
    )
    args = ap.parse_args()
    out = Path(args.output)
    count = build(out)
    print(f"Wrote {count} messages to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
