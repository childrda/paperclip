// Mirrors foia/api/schemas.py — keep in sync when the backend evolves.

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface EmailSummary {
  id: number;
  subject: string | null;
  from_addr: string | null;
  date_sent: string | null;
  mbox_source: string;
  mbox_index: number;
  has_attachments: boolean;
  pii_count: number;
}

export interface AttachmentSummary {
  id: number;
  email_id: number;
  filename: string | null;
  content_type: string | null;
  size_bytes: number;
  is_inline: boolean;
  is_nested_eml: boolean;
  extraction_status: string | null;
}

export interface PiiDetection {
  id: number;
  source_type: string;
  source_id: number;
  entity_type: string;
  start_offset: number;
  end_offset: number;
  matched_text: string;
  score: number;
  recognizer: string | null;
  detected_at: string;
}

export interface EmailDetail {
  id: number;
  message_id: string | null;
  subject: string | null;
  from_addr: string | null;
  to_addrs: string[];
  cc_addrs: string[];
  bcc_addrs: string[];
  date_sent: string | null;
  date_raw: string | null;
  body_text: string | null;
  body_html_sanitized: string | null;
  headers: Record<string, string[]>;
  mbox_source: string;
  mbox_index: number;
  ingested_at: string;
  attachments: AttachmentSummary[];
  pii_detections: PiiDetection[];
}

export type RedactionStatus = "proposed" | "accepted" | "rejected";
export type RedactionOrigin = "auto" | "manual";

export interface Redaction {
  id: number;
  source_type: string;
  source_id: number;
  start_offset: number;
  end_offset: number;
  exemption_code: string;
  reviewer_id: string | null;
  status: RedactionStatus;
  origin: RedactionOrigin;
  source_detection_id: number | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
}

export interface ExemptionCode {
  code: string;
  description: string;
}

export interface Stats {
  emails: number;
  attachments: number;
  attachments_with_text: number;
  pii_detections: number;
  persons: number;
  redactions: number;
  redactions_accepted: number;
}

export interface ExportManifest {
  export_id: string;
  pdf_url: string;
  csv_url: string;
  emails_exported: number;
  attachments_exported: number;
  pages_written: number;
  redactions_burned: number;
  bates_first: string | null;
  bates_last: string | null;
  created_at: string;
}

export type AiFlagStatus = "open" | "dismissed" | "promoted";

export interface AiFlag {
  id: number;
  source_type: string;
  source_id: number;
  entity_type: string;
  start_offset: number;
  end_offset: number;
  matched_text: string;
  confidence: number;
  rationale: string | null;
  suggested_exemption: string | null;
  provider: string;
  model: string | null;
  qa_run_id: string;
  flagged_at: string;
  review_status: AiFlagStatus;
  review_actor: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  promoted_redaction_id: number | null;
}
