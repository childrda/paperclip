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
  is_excluded: boolean;
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
  excluded_at: string | null;
  excluded_by_user_id: number | null;
  exclusion_reason: string | null;
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

export interface CurrentUser {
  user_id: number;
  username: string;
  display_name: string | null;
  email: string | null;
  expires_at: string;
}

export type CaseStatus =
  | "processing"
  | "ready"
  | "failed"
  | "exported"
  | "archived";

export interface Case {
  id: number;
  name: string;
  bates_prefix: string;
  status: CaseStatus;
  created_by: number | null;
  created_at: string;
  updated_at: string;
  error_message: string | null;
  failed_stage: string | null;
}

export interface CaseStats {
  emails: number;
  emails_excluded: number;
  attachments: number;
  pii_detections: number;
  redactions: number;
  redactions_accepted: number;
}

export interface PipelineJob {
  id: number;
  case_id: number;
  started_by: number | null;
  upload_path: string | null;
  label: string | null;
  propose_redactions: number;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  current_stage: string | null;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  failed_stage: string | null;
  created_at: string;
}

export interface CaseDetail {
  case: Case;
  stats: CaseStats;
  latest_job: PipelineJob | null;
}

export interface ImportSubmitted {
  job_id: number;
  case_id: number;
  case_name: string;
  bates_prefix: string;
  filename: string;
  saved_path: string;
  label: string | null;
  status: string;
  submitted_at: string;
}

export interface PipelineEvent {
  id: number;
  stage: string;
  kind: "started" | "progress" | "finished" | "failed";
  message: string | null;
  payload: Record<string, unknown> | null;
  event_at: string;
}

export interface ImportSummary {
  import_id: string;
  filename: string;
  saved_path: string;
  label: string | null;
  started_at: string;
  finished_at: string;
  stages: {
    ingest?: {
      mbox_source: string;
      emails_ingested: number;
      emails_skipped_duplicate: number;
      attachments_saved: number;
      errors: number;
    };
    extract?: {
      total: number;
      extracted_ok: number;
      extracted_empty: number;
      unsupported: number;
      failed: number;
      skipped_already_done: number;
    };
    detect?: {
      sources_scanned: number;
      sources_skipped: number;
      detections_written: number;
      by_entity: Record<string, number>;
    };
    resolve?: {
      emails_scanned: number;
      persons_created: number;
      persons_updated: number;
      occurrences_inserted: number;
      signatures_with_extra_emails: number;
    };
    propose?:
      | {
          detections_seen: number;
          proposed: number;
          skipped_existing: number;
          skipped_no_exemption: number;
          skipped_invalid: number;
          by_entity: Record<string, number>;
        }
      | { skipped: true };
  };
}

export interface ImportListItem {
  import_id: string | null;
  filename: string | null;
  label: string | null;
  actor: string;
  event_at: string;
  stages: ImportSummary["stages"];
}

export interface SearchHit {
  source_type: "email" | "attachment";
  source_id: number;
  title: string;
  snippet: string;
  rank: number;
  email_id: number | null;
}

export interface PersonSummary {
  id: number;
  display_name: string;
  primary_email: string | null;
  is_internal: boolean;
  occurrences: number;
}

export interface AuditEvent {
  id: number;
  event_at: string;
  event_type: string;
  actor: string;
  source_type: string | null;
  source_id: number | null;
  payload: Record<string, unknown> | null;
  request_origin: string;
}

export interface ExportListItem {
  export_id: string;
  pdf_url: string;
  csv_url: string;
  pdf_bytes: number;
  csv_bytes: number;
  created_at: string;
}

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
