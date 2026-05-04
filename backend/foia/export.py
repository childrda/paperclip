"""Phase 8 — PDF export with burned-in redactions, Bates numbering, and CSV log.

Design notes
------------
* "Burned in" means the redacted text never reaches the produced PDF.
  We split each line into visible / redacted segments and only draw
  the visible halves; black rectangles sit over the gaps with the
  exemption code stamped inside in white.
* ReportLab is used directly (no Platypus). Monospaced ``Courier`` at
  10pt gives stable per-character widths, which is what we need for
  offset-based redaction placement.
* Bates labels are global across the production: every page gets the
  next sequential number from ``district.bates``.
* The CSV log has one row per accepted redaction, so external systems
  (reviewer logs, court packets) can correlate.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen.canvas import Canvas

from .district import BatesConfig, DistrictConfig

log = logging.getLogger(__name__)


# Ordered list of body sources we render per email (in document order).
EMAIL_SOURCES: tuple[tuple[str, str], ...] = (
    ("email_subject", "Subject"),
    ("email_body_text", "Body"),
    ("email_body_html", "Body (HTML, sanitized)"),
)


@dataclass(frozen=True)
class ExportConfig:
    output_dir: Path
    font_name: str = "Courier"
    font_size: float = 9.5
    line_spacing: float = 1.25
    page_size: tuple[float, float] = letter
    margin: float = 0.75 * inch
    header_font: str = "Helvetica-Bold"
    header_size: float = 10
    label_font: str = "Helvetica"
    label_size: float = 9
    redaction_label_font: str = "Helvetica-Bold"
    redaction_label_size: float = 6.5


@dataclass
class ExportStats:
    emails_exported: int = 0
    attachments_exported: int = 0
    pages_written: int = 0
    redactions_burned: int = 0
    output_pdf: Path | None = None
    output_csv: Path | None = None
    bates_first: str | None = None
    bates_last: str | None = None

    def as_dict(self) -> dict:
        return {
            "emails_exported": self.emails_exported,
            "attachments_exported": self.attachments_exported,
            "pages_written": self.pages_written,
            "redactions_burned": self.redactions_burned,
            "output_pdf": str(self.output_pdf) if self.output_pdf else None,
            "output_csv": str(self.output_csv) if self.output_csv else None,
            "bates_first": self.bates_first,
            "bates_last": self.bates_last,
        }


@dataclass
class _Redaction:
    """Lightweight in-memory copy of an accepted redaction."""

    id: int
    source_type: str
    source_id: int
    start: int
    end: int
    exemption_code: str
    reviewer_id: str | None
    accepted_at: str  # actually `updated_at`


@dataclass
class _LogRow:
    bates_label: str
    redaction_id: int
    source_type: str
    source_id: int
    source_label: str        # e.g. "email 7 — Body"
    start_offset: int
    end_offset: int
    exemption_code: str
    reviewer_id: str | None
    accepted_at: str


@dataclass
class _Source:
    """One renderable text block — header + body."""

    source_type: str
    source_id: int
    label: str               # human-readable, shown above the body
    document_label: str      # short ref shown in the CSV ("email 7", "attach 12")
    headers: list[tuple[str, str]] = field(default_factory=list)
    text: str = ""
    redactions: list[_Redaction] = field(default_factory=list)


# ---------------------------------------------------------------------------
# DB → in-memory model
# ---------------------------------------------------------------------------


def _load_accepted_redactions(
    conn: sqlite3.Connection, *, only_email_ids: list[int] | None = None,
) -> dict[tuple[str, int], list[_Redaction]]:
    sql = "SELECT * FROM redactions WHERE status = 'accepted'"
    params: list = []
    if only_email_ids is not None:
        placeholders = ",".join("?" * len(only_email_ids))
        sql += (
            f" AND ((source_type LIKE 'email_%' AND source_id IN ({placeholders}))"
            f"   OR (source_type = 'attachment_text' AND source_id IN ("
            f"        SELECT id FROM attachments WHERE email_id IN ({placeholders}))))"
        )
        params.extend(only_email_ids)
        params.extend(only_email_ids)
    sql += " ORDER BY source_type, source_id, start_offset"
    out: dict[tuple[str, int], list[_Redaction]] = {}
    for row in conn.execute(sql, params):
        r = _Redaction(
            id=int(row["id"]),
            source_type=row["source_type"],
            source_id=int(row["source_id"]),
            start=int(row["start_offset"]),
            end=int(row["end_offset"]),
            exemption_code=row["exemption_code"],
            reviewer_id=row["reviewer_id"],
            accepted_at=row["updated_at"],
        )
        out.setdefault((r.source_type, r.source_id), []).append(r)
    return out


def _email_sources(
    conn: sqlite3.Connection,
    redactions: dict[tuple[str, int], list[_Redaction]],
    *, only_email_ids: list[int] | None = None,
) -> Iterable[_Source]:
    sql = (
        "SELECT id, subject, from_addr, to_addrs, cc_addrs, date_sent,"
        "       body_text, body_html_sanitized, mbox_source, mbox_index "
        "FROM emails"
    )
    params: list = []
    where: list[str] = ["excluded_at IS NULL"]
    if only_email_ids is not None:
        where.append(f"id IN ({','.join('?' * len(only_email_ids))})")
        params.extend(only_email_ids)
    sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY (date_sent IS NULL), date_sent, id"

    import json as _json
    for row in conn.execute(sql, params):
        eid = int(row["id"])
        headers = [
            ("From", row["from_addr"] or ""),
            ("To", ", ".join(_json.loads(row["to_addrs"] or "[]"))),
        ]
        ccs = _json.loads(row["cc_addrs"] or "[]")
        if ccs:
            headers.append(("Cc", ", ".join(ccs)))
        if row["date_sent"]:
            headers.append(("Date", row["date_sent"]))
        headers.append(("Source", f"{row['mbox_source']} #{row['mbox_index']}"))

        for source_type, label in EMAIL_SOURCES:
            text = (
                row["subject"] if source_type == "email_subject"
                else row["body_text"] if source_type == "email_body_text"
                else row["body_html_sanitized"]
            ) or ""
            yield _Source(
                source_type=source_type,
                source_id=eid,
                label=f"Email #{eid} — {label}",
                document_label=f"email {eid}",
                headers=headers if source_type == "email_subject" else [],
                text=text,
                redactions=redactions.get((source_type, eid), []),
            )


def _attachment_sources(
    conn: sqlite3.Connection,
    redactions: dict[tuple[str, int], list[_Redaction]],
    *, only_email_ids: list[int] | None = None,
) -> Iterable[_Source]:
    sql = (
        "SELECT a.id, a.filename, a.content_type, a.email_id,"
        "       t.extracted_text "
        "FROM attachments a "
        "JOIN attachments_text t ON t.attachment_id = a.id "
        "JOIN emails e ON e.id = a.email_id "
        "WHERE t.extraction_status = 'ok' "
        "  AND t.extracted_text IS NOT NULL "
        "  AND e.excluded_at IS NULL"
    )
    params: list = []
    if only_email_ids is not None:
        sql += f" AND a.email_id IN ({','.join('?' * len(only_email_ids))})"
        params.extend(only_email_ids)
    sql += " ORDER BY a.email_id, a.id"
    for row in conn.execute(sql, params):
        aid = int(row["id"])
        yield _Source(
            source_type="attachment_text",
            source_id=aid,
            label=(
                f"Attachment #{aid} — {row['filename'] or '(unnamed)'}"
                f" ({row['content_type'] or 'unknown type'})"
            ),
            document_label=f"attach {aid}",
            headers=[
                ("Email", str(row["email_id"])),
                ("File",  row["filename"] or ""),
                ("Type",  row["content_type"] or ""),
            ],
            text=row["extracted_text"] or "",
            redactions=redactions.get(("attachment_text", aid), []),
        )


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------


def _sanitize_for_pdf(text: str) -> str:
    """Replace characters Helvetica can't draw with spaces, one-for-one.

    Tabs and other control bytes render as solid ``■`` boxes through
    reportlab's default font (their glyph slots are missing). The
    common case is patch diffs and quoted code where ``\\t`` indents
    appear as walls of squares in the production PDF.

    We keep the substitution character-for-character so existing
    redaction offsets continue to align — tabs become single spaces,
    not expanded indents.
    """
    out_chars: list[str] = []
    for ch in text:
        if ch == "\n":
            out_chars.append(ch)
            continue
        code = ord(ch)
        # ASCII control range (incl. tab, vertical tab, form feed,
        # carriage return) plus DEL.
        if code < 0x20 or code == 0x7F:
            out_chars.append(" ")
            continue
        out_chars.append(ch)
    return "".join(out_chars)


def _wrap_lines(text: str, width_chars: int) -> list[tuple[int, str]]:
    """Hard-wrap to ``width_chars`` per line. Returns (offset, line) pairs."""
    text = _sanitize_for_pdf(text)
    out: list[tuple[int, str]] = []
    cursor = 0
    for raw_line in text.split("\n"):
        if not raw_line:
            out.append((cursor, ""))
            cursor += 1
            continue
        idx = 0
        while idx < len(raw_line):
            chunk = raw_line[idx:idx + width_chars]
            out.append((cursor + idx, chunk))
            idx += len(chunk)
        cursor += len(raw_line) + 1  # +1 for the consumed "\n"
    return out


def _line_intersections(
    line_start: int, line_text: str, redactions: list[_Redaction]
) -> list[tuple[int, int, _Redaction]]:
    """For one line, return the (start_in_line, end_in_line, redaction) triples."""
    line_end = line_start + len(line_text)
    out: list[tuple[int, int, _Redaction]] = []
    for r in redactions:
        if r.end <= line_start or r.start >= line_end:
            continue
        s = max(r.start, line_start) - line_start
        e = min(r.end, line_end) - line_start
        if e > s:
            out.append((s, e, r))
    out.sort(key=lambda t: t[0])
    return out


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


class _PageContext:
    def __init__(
        self,
        canvas: Canvas,
        config: ExportConfig,
        bates: BatesConfig,
        district: DistrictConfig,
    ):
        self.c = canvas
        self.cfg = config
        self.bates = bates
        self.district = district
        self.page_w, self.page_h = config.page_size
        self.left = config.margin
        self.right = self.page_w - config.margin
        self.top = self.page_h - config.margin
        self.bottom = config.margin + 30  # leave room for footer
        self.line_height = config.font_size * config.line_spacing

        self.char_w = stringWidth("M", config.font_name, config.font_size)
        self.body_width = self.right - self.left
        self.chars_per_line = max(1, int(self.body_width // self.char_w))

        self.pages_written = 0
        self.bates_seq = bates.start - 1
        self.current_bates_label: str | None = None
        self.first_bates_label: str | None = None
        self.last_bates_label: str | None = None
        self.y = self.top
        self._begin_page()

    # ---- page lifecycle

    def _begin_page(self) -> None:
        self.bates_seq += 1
        self.current_bates_label = self.bates.label(self.bates_seq)
        if self.first_bates_label is None:
            self.first_bates_label = self.current_bates_label
        self.last_bates_label = self.current_bates_label
        self.y = self.top

    def _finish_page(self) -> None:
        c = self.c
        # Bates label, bottom-right
        c.setFont(self.cfg.label_font, self.cfg.label_size)
        c.setFillGray(0.25)
        c.drawRightString(
            self.right, self.cfg.margin / 2,
            self.current_bates_label or "",
        )
        # Production banner, bottom-left
        c.drawString(
            self.left, self.cfg.margin / 2,
            f"{self.district.name} — FOIA production",
        )
        c.setFillGray(0.0)
        self.pages_written += 1

    def new_page(self) -> None:
        self._finish_page()
        self.c.showPage()
        self._begin_page()

    def finalize(self) -> None:
        self._finish_page()

    # ---- ensure-room helper

    def ensure_room(self, lines_needed: int = 1) -> None:
        needed_h = lines_needed * self.line_height
        if self.y - needed_h < self.bottom:
            self.new_page()

    # ---- text helpers

    def heading(self, text: str) -> None:
        self.ensure_room(2)
        self.c.setFont(self.cfg.header_font, self.cfg.header_size)
        self.c.setFillGray(0.0)
        self.c.drawString(self.left, self.y - self.cfg.header_size, text)
        self.y -= self.line_height * 1.6

    def headers_block(self, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        self.c.setFont(self.cfg.label_font, self.cfg.label_size)
        for k, v in items:
            self.ensure_room(1)
            self.c.setFillGray(0.4)
            self.c.drawString(self.left, self.y - self.cfg.label_size, f"{k}:")
            self.c.setFillGray(0.0)
            self.c.drawString(
                self.left + 0.6 * inch,
                self.y - self.cfg.label_size,
                v,
            )
            self.y -= self.line_height
        self.y -= self.line_height * 0.4

    def hr(self) -> None:
        self.ensure_room(1)
        self.c.setStrokeGray(0.7)
        self.c.line(self.left, self.y - 3, self.right, self.y - 3)
        self.y -= self.line_height * 0.6


def _draw_redaction_box(
    c: Canvas, x: float, y_baseline: float,
    chars_covered: int, char_w: float, font_size: float,
    label: str, label_font: str, label_size: float,
) -> None:
    """Black box covering N character widths, with exemption code stamped inside."""
    pad = 1.0
    w = char_w * chars_covered + pad
    h = font_size * 1.18
    rect_y = y_baseline - h * 0.18
    c.setFillGray(0.0)
    c.rect(x - pad / 2, rect_y, w, h, stroke=0, fill=1)
    if label:
        # White exemption code, centred. If too wide for the box, omit.
        text_w = stringWidth(label, label_font, label_size)
        if text_w + 4 <= w:
            c.setFillGray(1.0)
            c.setFont(label_font, label_size)
            c.drawString(
                x + (w - text_w) / 2 - pad / 2,
                rect_y + (h - label_size) / 2,
                label,
            )
            c.setFillGray(0.0)


def _render_body(
    page: _PageContext,
    text: str,
    redactions: list[_Redaction],
    log_rows: list[_LogRow],
    document_label: str,
    on_redaction_burned,
) -> None:
    """Render a body of text with burned-in redactions; append CSV log rows."""
    if not text:
        page.c.setFont(page.cfg.label_font, page.cfg.label_size)
        page.c.setFillGray(0.55)
        page.ensure_room(1)
        page.c.drawString(
            page.left, page.y - page.cfg.label_size,
            "(empty)",
        )
        page.y -= page.line_height
        page.c.setFillGray(0.0)
        return

    redactions_logged: set[int] = set()
    lines = _wrap_lines(text, page.chars_per_line)
    for line_offset, line_text in lines:
        page.ensure_room(1)
        # Reset font each line — the header_block above may have switched fonts.
        page.c.setFont(page.cfg.font_name, page.cfg.font_size)
        page.c.setFillGray(0.0)

        intersections = _line_intersections(line_offset, line_text, redactions)
        baseline_y = page.y - page.cfg.font_size

        if not intersections:
            if line_text:
                page.c.drawString(page.left, baseline_y, line_text)
        else:
            cursor = 0
            for s, e, r in intersections:
                # Visible segment before the redaction.
                if s > cursor:
                    seg = line_text[cursor:s]
                    page.c.drawString(
                        page.left + cursor * page.char_w, baseline_y, seg,
                    )
                # Black box.
                _draw_redaction_box(
                    page.c,
                    page.left + s * page.char_w,
                    baseline_y,
                    chars_covered=e - s,
                    char_w=page.char_w,
                    font_size=page.cfg.font_size,
                    label=r.exemption_code,
                    label_font=page.cfg.redaction_label_font,
                    label_size=page.cfg.redaction_label_size,
                )
                if r.id not in redactions_logged:
                    redactions_logged.add(r.id)
                    log_rows.append(
                        _LogRow(
                            bates_label=page.current_bates_label or "",
                            redaction_id=r.id,
                            source_type=r.source_type,
                            source_id=r.source_id,
                            source_label=document_label,
                            start_offset=r.start,
                            end_offset=r.end,
                            exemption_code=r.exemption_code,
                            reviewer_id=r.reviewer_id,
                            accepted_at=r.accepted_at,
                        )
                    )
                    on_redaction_burned()
                cursor = e
            # Trailing visible segment after the last redaction.
            if cursor < len(line_text):
                seg = line_text[cursor:]
                page.c.drawString(
                    page.left + cursor * page.char_w, baseline_y, seg,
                )

        page.y -= page.line_height


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_export(
    conn: sqlite3.Connection,
    district: DistrictConfig,
    config: ExportConfig,
    *,
    only_email_ids: list[int] | None = None,
    include_attachments: bool = True,
) -> ExportStats:
    """Generate a redacted PDF + CSV log for the configured scope."""
    config.output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = config.output_dir / "production.pdf"
    csv_path = config.output_dir / "redaction_log.csv"

    redactions = _load_accepted_redactions(conn, only_email_ids=only_email_ids)

    c = Canvas(str(pdf_path), pagesize=config.page_size)
    c.setTitle(f"FOIA production — {district.name}")
    c.setAuthor("FOIA Redaction Tool")

    pdf_metadata = {"creator": "FOIA Redaction Tool"}
    pdfmetrics.registerFontFamily("Courier", normal="Courier", bold="Courier-Bold")
    page = _PageContext(c, config, district.bates, district)

    log_rows: list[_LogRow] = []
    stats = ExportStats()

    def on_burn() -> None:
        stats.redactions_burned += 1

    # ---- emails
    seen_email_ids: set[int] = set()
    sources_by_email: dict[int, list[_Source]] = {}
    for src in _email_sources(conn, redactions, only_email_ids=only_email_ids):
        sources_by_email.setdefault(src.source_id, []).append(src)
        seen_email_ids.add(src.source_id)

    for email_id, sources in sources_by_email.items():
        page.heading(f"Email #{email_id}")
        for src in sources:
            if src.headers:
                page.headers_block(src.headers)
            if src.label.endswith("Subject"):
                # Subject text is short; render inline rather than as its own block.
                page.c.setFont(config.label_font, config.label_size)
                page.c.setFillGray(0.4)
                page.ensure_room(1)
                page.c.drawString(
                    page.left, page.y - config.label_size, "Subject:",
                )
                page.c.setFillGray(0.0)
                page.y -= page.line_height
            else:
                page.heading(src.label)
            _render_body(
                page, src.text, src.redactions, log_rows,
                document_label=src.document_label,
                on_redaction_burned=on_burn,
            )
            page.hr()
        page.new_page()
        stats.emails_exported += 1

    # ---- attachments
    if include_attachments:
        for src in _attachment_sources(
            conn, redactions, only_email_ids=only_email_ids
        ):
            page.heading(src.label)
            if src.headers:
                page.headers_block(src.headers)
            _render_body(
                page, src.text, src.redactions, log_rows,
                document_label=src.document_label,
                on_redaction_burned=on_burn,
            )
            page.hr()
            page.new_page()
            stats.attachments_exported += 1

    # If nothing rendered, emit a single page so the PDF is well-formed.
    if (stats.emails_exported + stats.attachments_exported) == 0:
        page.heading("No documents in scope")
        page.c.setFont(config.label_font, config.label_size)
        page.ensure_room(1)
        page.c.drawString(
            page.left, page.y - config.label_size,
            f"Generated at {datetime.now(timezone.utc).isoformat()}",
        )

    page.finalize()
    c.save()

    # ---- CSV log
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "bates_label", "redaction_id", "source_type", "source_id",
            "source_label", "start_offset", "end_offset", "length",
            "exemption_code", "reviewer_id", "accepted_at",
        ])
        for r in log_rows:
            writer.writerow([
                r.bates_label, r.redaction_id, r.source_type, r.source_id,
                r.source_label, r.start_offset, r.end_offset,
                r.end_offset - r.start_offset, r.exemption_code,
                r.reviewer_id or "", r.accepted_at,
            ])

    stats.output_pdf = pdf_path
    stats.output_csv = csv_path
    stats.pages_written = page.pages_written
    stats.bates_first = page.first_bates_label
    stats.bates_last = page.last_bates_label

    log.info(
        "export complete: %d emails, %d attachments, %d pages, %d redactions, %s..%s",
        stats.emails_exported, stats.attachments_exported,
        stats.pages_written, stats.redactions_burned,
        stats.bates_first, stats.bates_last,
    )
    _ = pdf_metadata  # reserved for future use
    return stats


__all__ = ["ExportConfig", "ExportStats", "run_export"]
