"""Pydantic response shapes for the API.

All list endpoints return :class:`Page[T]` — a consistent
``{items, total, limit, offset}`` envelope.
"""

from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class EmailSummary(BaseModel):
    id: int
    subject: str | None
    from_addr: str | None
    date_sent: str | None
    mbox_source: str
    mbox_index: int
    has_attachments: bool
    pii_count: int


class AttachmentSummary(BaseModel):
    id: int
    email_id: int
    filename: str | None
    content_type: str | None
    size_bytes: int
    is_inline: bool
    is_nested_eml: bool
    extraction_status: str | None = None


class EmailDetail(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=False)

    id: int
    message_id: str | None
    subject: str | None
    from_addr: str | None
    to_addrs: list[str]
    cc_addrs: list[str]
    bcc_addrs: list[str]
    date_sent: str | None
    date_raw: str | None
    body_text: str | None
    body_html_sanitized: str | None
    headers: dict[str, list[str]]
    mbox_source: str
    mbox_index: int
    ingested_at: str
    attachments: list[AttachmentSummary]
    pii_detections: list["PiiDetectionOut"]


class AttachmentDetail(BaseModel):
    id: int
    email_id: int
    filename: str | None
    content_type: str | None
    content_disposition: str | None
    size_bytes: int
    sha256: str
    is_inline: bool
    is_nested_eml: bool
    storage_path: str
    extracted_text: str | None = None
    extraction_method: str | None = None
    extraction_status: str | None = None
    ocr_applied: bool | None = None
    page_count: int | None = None
    character_count: int | None = None
    extraction_error: str | None = None


class PiiDetectionOut(BaseModel):
    id: int
    source_type: str
    source_id: int
    entity_type: str
    start_offset: int
    end_offset: int
    matched_text: str
    score: float
    recognizer: str | None = None
    detected_at: str


class EntityCount(BaseModel):
    entity_type: str
    count: int


class PersonSummary(BaseModel):
    id: int
    display_name: str
    primary_email: str | None
    is_internal: bool
    occurrences: int


class PersonDetail(BaseModel):
    id: int
    display_name: str
    names: list[str]
    is_internal: bool
    notes: str | None
    emails: list["PersonEmailOut"]
    occurrences_by_type: dict[str, int]
    created_at: str
    updated_at: str


class PersonEmailOut(BaseModel):
    email: str
    is_primary: bool
    first_seen: str


class SearchHit(BaseModel):
    source_type: str            # 'email' | 'attachment'
    source_id: int
    title: str                  # subject or filename
    snippet: str                # fts5 snippet with <mark>...</mark>
    rank: float
    email_id: int | None = None


class Stats(BaseModel):
    emails: int
    attachments: int
    attachments_with_text: int
    pii_detections: int
    persons: int
    redactions: int = 0
    redactions_accepted: int = 0


# Phase 6 — Redactions

_VALID_SOURCE_TYPES = {
    "email_subject", "email_body_text", "email_body_html", "attachment_text",
}
_VALID_STATUSES = {"proposed", "accepted", "rejected"}


class RedactionOut(BaseModel):
    id: int
    source_type: str
    source_id: int
    start_offset: int
    end_offset: int
    exemption_code: str
    reviewer_id: str | None
    status: str
    origin: str
    source_detection_id: int | None
    notes: str | None
    created_at: str
    updated_at: str


class RedactionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: str = Field(..., description="One of email_subject, email_body_text, email_body_html, attachment_text.")
    source_id: int
    start_offset: int = Field(..., ge=0)
    end_offset: int = Field(..., gt=0)
    exemption_code: str
    reviewer_id: str | None = None
    status: str = "proposed"
    notes: str | None = None


class RedactionPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str | None = None
    exemption_code: str | None = None
    reviewer_id: str | None = None
    notes: str | None = None


class ExemptionCodeOut(BaseModel):
    code: str
    description: str = ""


# Phase 9 — Audit log

class AuditEventOut(BaseModel):
    id: int
    event_at: str
    event_type: str
    actor: str
    source_type: str | None
    source_id: int | None
    payload: dict | None
    request_origin: str


# Phase 10 — AI QA

class AiFlagOut(BaseModel):
    id: int
    source_type: str
    source_id: int
    entity_type: str
    start_offset: int
    end_offset: int
    matched_text: str
    confidence: float
    rationale: str | None
    suggested_exemption: str | None
    provider: str
    model: str | None
    qa_run_id: str
    flagged_at: str
    review_status: str
    review_actor: str | None
    reviewed_at: str | None
    review_note: str | None
    promoted_redaction_id: int | None


class AiQaRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email_id: int | None = None
    attachment_id: int | None = None
    provider: str | None = Field(
        None,
        description="Override the configured provider for this run "
                    "(null | openai | anthropic | azure | ollama).",
    )
    model: str | None = Field(
        None, description="Override the configured model for this run."
    )


class AiPromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exemption_code: str | None = None
    note: str | None = None


class AiDismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = None


EmailDetail.model_rebuild()
PersonDetail.model_rebuild()
