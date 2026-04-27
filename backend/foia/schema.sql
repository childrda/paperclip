-- FOIA redaction tool — Phase 1 schema.
-- Non-destructive: raw_content preserves the original RFC822 bytes exactly.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS emails (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id          TEXT,
    mbox_source         TEXT NOT NULL,
    mbox_index          INTEGER NOT NULL,
    subject             TEXT,
    from_addr           TEXT,
    to_addrs            TEXT,     -- JSON array of strings
    cc_addrs            TEXT,     -- JSON array of strings
    bcc_addrs           TEXT,     -- JSON array of strings
    date_sent           TEXT,     -- ISO 8601, best-effort
    date_raw            TEXT,     -- original Date: header
    body_text           TEXT,
    body_html_sanitized TEXT,
    headers_json        TEXT,     -- full headers as JSON object (key -> list[str])
    ingested_at         TEXT NOT NULL,
    case_id             INTEGER,  -- FK added by db.py migration; NULL = legacy
    UNIQUE (mbox_source, mbox_index)
);

CREATE INDEX IF NOT EXISTS idx_emails_case ON emails(case_id);

CREATE INDEX IF NOT EXISTS idx_emails_message_id ON emails(message_id);
CREATE INDEX IF NOT EXISTS idx_emails_from       ON emails(from_addr);
CREATE INDEX IF NOT EXISTS idx_emails_date       ON emails(date_sent);

CREATE TABLE IF NOT EXISTS attachments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id            INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    filename            TEXT,
    content_type        TEXT,
    content_disposition TEXT,
    size_bytes          INTEGER NOT NULL,
    sha256              TEXT NOT NULL,
    storage_path        TEXT NOT NULL,
    is_inline           INTEGER NOT NULL DEFAULT 0,
    is_nested_eml       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_attachments_email  ON attachments(email_id);
CREATE INDEX IF NOT EXISTS idx_attachments_sha256 ON attachments(sha256);

CREATE TABLE IF NOT EXISTS raw_content (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id    INTEGER NOT NULL UNIQUE REFERENCES emails(id) ON DELETE CASCADE,
    raw_rfc822  BLOB NOT NULL,
    raw_sha256  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_raw_content_sha256 ON raw_content(raw_sha256);

-- Phase 2: derived text per attachment. One row per attachment.
-- Non-destructive: the original bytes in `attachments` remain untouched;
-- this table holds the searchable text extracted from them.
CREATE TABLE IF NOT EXISTS attachments_text (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_id     INTEGER NOT NULL UNIQUE REFERENCES attachments(id) ON DELETE CASCADE,
    extracted_text    TEXT,
    extraction_method TEXT NOT NULL,     -- 'pypdf', 'ocr_tesseract', 'pdf_ocr',
                                         -- 'libreoffice+pypdf', 'eml_body', 'text',
                                         -- 'html', 'skipped'
    ocr_applied       INTEGER NOT NULL DEFAULT 0,
    page_count        INTEGER,
    character_count   INTEGER NOT NULL DEFAULT 0,
    extraction_status TEXT NOT NULL,     -- 'ok', 'empty', 'unsupported', 'failed'
    error_message     TEXT,
    extracted_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_attachments_text_status
    ON attachments_text(extraction_status);

-- Phase 3: PII detections. One row per detected span. Detections are
-- derived content (re-runnable); the underlying bytes/text are never mutated.
--
-- source_type enumerates where the span was found:
--   'email_subject'   -> source_id = emails.id
--   'email_body_text' -> source_id = emails.id
--   'email_body_html' -> source_id = emails.id (sanitized HTML, text view)
--   'attachment_text' -> source_id = attachments.id
CREATE TABLE IF NOT EXISTS pii_detections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type  TEXT    NOT NULL,
    source_id    INTEGER NOT NULL,
    entity_type  TEXT    NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset   INTEGER NOT NULL,
    matched_text TEXT    NOT NULL,
    score        REAL    NOT NULL,
    recognizer   TEXT,
    detected_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pii_source
    ON pii_detections(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_pii_entity ON pii_detections(entity_type);
CREATE INDEX IF NOT EXISTS idx_pii_score  ON pii_detections(score);

-- Phase 4: entity resolution. A `person` is the unified identity for one
-- or more observed email addresses. Name variants are preserved in
-- names_json (JSON array). Occurrences map a person to each email where
-- they appear as sender/recipient or in the signature.
CREATE TABLE IF NOT EXISTS persons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name    TEXT    NOT NULL,
    names_json      TEXT    NOT NULL DEFAULT '[]',
    is_internal     INTEGER NOT NULL DEFAULT 0,
    notes           TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

-- Email → person index. One email belongs to at most one person so the
-- `email` column is PK. A person may have multiple emails (e.g. after a
-- manual merge). Exactly one row per person has is_primary=1.
CREATE TABLE IF NOT EXISTS person_emails (
    person_id   INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    email       TEXT    NOT NULL PRIMARY KEY,
    is_primary  INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_person_emails_pid
    ON person_emails(person_id);

-- One row per (person × source). source_type enumerates the role:
--   'email_from'  -> source_id = emails.id, raw_text = original "Name <a@b>"
--   'email_to'    -> source_id = emails.id
--   'email_cc'    -> source_id = emails.id
--   'email_bcc'   -> source_id = emails.id
--   'signature'   -> source_id = emails.id, raw_text = found email substring
CREATE TABLE IF NOT EXISTS person_occurrences (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id     INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    source_type   TEXT    NOT NULL,
    source_id     INTEGER NOT NULL,
    raw_text      TEXT,
    created_at    TEXT    NOT NULL,
    UNIQUE (person_id, source_type, source_id)
);

CREATE INDEX IF NOT EXISTS idx_person_occ_person ON person_occurrences(person_id);
CREATE INDEX IF NOT EXISTS idx_person_occ_source ON person_occurrences(source_type, source_id);

-- Phase 4 (temporal classifier): every observation a person makes in
-- the corpus is logged with the date it was *observed*, so downstream
-- code can ask "what did we know about this person on date X?" rather
-- than "what do we know now?". Required for legal defensibility — a
-- redaction decision dated March 2022 must be defensible from what
-- the corpus knew in March 2022, not from later signal.
--
-- Common affiliation_type values:
--   'email_domain'     value = the domain the person was using
--   'is_internal'      value = 'true' | 'false' under district rules at the time
--   'signature_email'  value = an alternate email seen in that email's signature
CREATE TABLE IF NOT EXISTS person_affiliations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id         INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
    affiliation_type  TEXT    NOT NULL,
    affiliation_value TEXT    NOT NULL,
    observed_at       TEXT    NOT NULL,
    source_email_id   INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    created_at        TEXT    NOT NULL,
    UNIQUE (person_id, affiliation_type, affiliation_value, source_email_id)
);

CREATE INDEX IF NOT EXISTS idx_aff_person_time
    ON person_affiliations(person_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_aff_type ON person_affiliations(affiliation_type);

-- Phase 5: FTS5 virtual tables for full-text search over email bodies and
-- extracted attachment text. Content is owned (default) so snippets / highlights
-- work without an external content reference. Triggers mirror inserts from the
-- canonical tables; deletes cascade via CASCADE on the FK tables, so we also
-- wire an AFTER DELETE sync.
CREATE VIRTUAL TABLE IF NOT EXISTS emails_fts USING fts5(
    subject,
    body_text,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS emails_fts_ai AFTER INSERT ON emails BEGIN
    INSERT INTO emails_fts(rowid, subject, body_text)
    VALUES (
        new.id,
        COALESCE(new.subject, ''),
        COALESCE(new.body_text, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS emails_fts_ad AFTER DELETE ON emails BEGIN
    DELETE FROM emails_fts WHERE rowid = old.id;
END;

CREATE VIRTUAL TABLE IF NOT EXISTS attachments_fts USING fts5(
    filename,
    extracted_text,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS attachments_fts_ai AFTER INSERT ON attachments_text BEGIN
    INSERT INTO attachments_fts(rowid, filename, extracted_text)
    VALUES (
        new.attachment_id,
        COALESCE(
            (SELECT filename FROM attachments WHERE id = new.attachment_id),
            ''
        ),
        COALESCE(new.extracted_text, '')
    );
END;

CREATE TRIGGER IF NOT EXISTS attachments_fts_ad AFTER DELETE ON attachments_text BEGIN
    DELETE FROM attachments_fts WHERE rowid = old.attachment_id;
END;

-- Phase 6: redaction spans. The source is never mutated; redactions are
-- recorded as offsets into the canonical text. A redaction starts life
-- as 'proposed' (auto-created from a PII detection or hand-entered) and
-- only counts as authoritative once a reviewer transitions it to
-- 'accepted'. Phase 8's PDF export must read only accepted rows.
--
-- source_type values:
--   'email_subject'   -> emails.subject
--   'email_body_text' -> emails.body_text
--   'email_body_html' -> emails.body_html_sanitized
--   'attachment_text' -> attachments_text.extracted_text
CREATE TABLE IF NOT EXISTS redactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type         TEXT    NOT NULL,
    source_id           INTEGER NOT NULL,
    start_offset        INTEGER NOT NULL,
    end_offset          INTEGER NOT NULL,
    exemption_code      TEXT    NOT NULL,
    reviewer_id         TEXT,
    status              TEXT    NOT NULL DEFAULT 'proposed',
    origin              TEXT    NOT NULL DEFAULT 'manual',
    source_detection_id INTEGER REFERENCES pii_detections(id) ON DELETE SET NULL,
    notes               TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    CHECK (start_offset >= 0),
    CHECK (end_offset > start_offset),
    CHECK (status IN ('proposed', 'accepted', 'rejected')),
    CHECK (origin IN ('auto', 'manual')),
    CHECK (source_type IN ('email_subject', 'email_body_text',
                           'email_body_html', 'attachment_text')),
    UNIQUE (source_type, source_id, start_offset, end_offset, exemption_code)
);

CREATE INDEX IF NOT EXISTS idx_redactions_source
    ON redactions(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_redactions_status
    ON redactions(status);
CREATE INDEX IF NOT EXISTS idx_redactions_origin
    ON redactions(origin);

-- Phase 9: append-only audit log. Every write across the system —
-- ingestion runs, extraction runs, detection runs, entity-resolution
-- runs, redaction CRUD, exports — emits one row here. Rows are
-- never updated or deleted; the triggers below enforce that at the
-- DB level, so even direct SQL can't tamper with the trail.
--
-- request_origin: 'cli' | 'api' | 'system'
-- payload_json:   JSON object with operation-specific details
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_at        TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    actor           TEXT    NOT NULL,
    user_id         INTEGER,            -- added by db.py migration; NULL = legacy CLI/API actor only
    source_type     TEXT,
    source_id       INTEGER,
    payload_json    TEXT,
    request_origin  TEXT    NOT NULL DEFAULT 'cli'
);

CREATE INDEX IF NOT EXISTS idx_audit_event_at   ON audit_log(event_at);
CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_actor      ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_audit_user       ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_source     ON audit_log(source_type, source_id);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

-- Authentication: users mirror the directory after a successful LDAPS
-- login. We never store passwords here. ``directory_dn`` is the bind DN
-- so a stable foreign key survives renames.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    directory_dn    TEXT    UNIQUE,
    display_name    TEXT,
    email           TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    last_login_at   TEXT,
    last_seen_at    TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

-- Issued opaque session tokens. Tokens are stored hashed (SHA-256) so a
-- DB read alone can't impersonate. ``expires_at`` is a hard timeout;
-- ``last_refresh_at`` is bumped on every authenticated request so we
-- can refuse stale sessions.
CREATE TABLE IF NOT EXISTS user_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash        TEXT    NOT NULL UNIQUE,
    issued_at         TEXT    NOT NULL,
    expires_at        TEXT    NOT NULL,
    last_refresh_at   TEXT    NOT NULL,
    last_group_check_at TEXT,
    revoked_at        TEXT,
    source_ip         TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_token ON user_sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions(user_id);

-- Failed login attempts for lockout policy. Append-only.
CREATE TABLE IF NOT EXISTS auth_failed_logins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL,
    source_ip     TEXT,
    reason        TEXT,
    attempted_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_failed_user_time
    ON auth_failed_logins(username, attempted_at);

-- Cases: top-level grouping for one FOIA production. Created when a
-- reviewer uploads a mailbox; carries the per-case Bates prefix and
-- review status.
CREATE TABLE IF NOT EXISTS cases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    bates_prefix    TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'processing',
    created_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    error_message   TEXT,
    failed_stage    TEXT,
    CHECK (status IN ('processing', 'ready', 'failed', 'exported', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status);
CREATE INDEX IF NOT EXISTS idx_cases_created_at ON cases(created_at);

-- Background pipeline jobs. One row per ``imports`` upload; the SSE
-- endpoint streams a row's stage events as they're published.
CREATE TABLE IF NOT EXISTS pipeline_jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id         INTEGER REFERENCES cases(id) ON DELETE CASCADE,
    started_by      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    upload_path     TEXT,
    label           TEXT,
    propose_redactions INTEGER NOT NULL DEFAULT 1,
    status          TEXT    NOT NULL DEFAULT 'queued',
    current_stage   TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    error_message   TEXT,
    failed_stage    TEXT,
    created_at      TEXT    NOT NULL,
    CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_case ON pipeline_jobs(case_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON pipeline_jobs(status);

-- Per-stage progress events streamed to clients. Append-only.
CREATE TABLE IF NOT EXISTS pipeline_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES pipeline_jobs(id) ON DELETE CASCADE,
    stage       TEXT    NOT NULL,
    kind        TEXT    NOT NULL,    -- 'started' | 'progress' | 'finished' | 'failed'
    message     TEXT,
    payload_json TEXT,
    event_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_job_id ON pipeline_events(job_id, id);

-- Scope emails to cases. Existing rows pre-Phase-X have NULL until the
-- migration in db.py assigns them to a default "Legacy" case.
-- (SQLite ALTER TABLE can't add a column conditionally; we handle the
-- legacy-row migration in code.)

-- Phase 10: AI QA flags. Output of an LLM-assisted scan. These are
-- *advisory* only — AI must never auto-redact. A flag transitions to
-- 'promoted' when a human explicitly creates a redaction from it via
-- the API/CLI; the link to the resulting redaction lives in
-- ``promoted_redaction_id``.
--
-- review_status: 'open' | 'dismissed' | 'promoted'
-- provider:      'null' | 'openai' | 'anthropic' | 'azure' | 'ollama'
CREATE TABLE IF NOT EXISTS ai_flags (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type           TEXT    NOT NULL,
    source_id             INTEGER NOT NULL,
    entity_type           TEXT    NOT NULL,
    start_offset          INTEGER NOT NULL,
    end_offset            INTEGER NOT NULL,
    matched_text          TEXT    NOT NULL,
    confidence            REAL    NOT NULL,
    rationale             TEXT,
    suggested_exemption   TEXT,
    provider              TEXT    NOT NULL,
    model                 TEXT,
    qa_run_id             TEXT    NOT NULL,
    flagged_at            TEXT    NOT NULL,
    review_status         TEXT    NOT NULL DEFAULT 'open',
    review_actor          TEXT,
    reviewed_at           TEXT,
    review_note           TEXT,
    promoted_redaction_id INTEGER REFERENCES redactions(id) ON DELETE SET NULL,
    CHECK (start_offset >= 0),
    CHECK (end_offset > start_offset),
    CHECK (review_status IN ('open', 'dismissed', 'promoted')),
    CHECK (source_type IN ('email_subject', 'email_body_text',
                           'email_body_html', 'attachment_text')),
    UNIQUE (source_type, source_id, start_offset, end_offset, entity_type, provider)
);

CREATE INDEX IF NOT EXISTS idx_ai_flags_source
    ON ai_flags(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_ai_flags_status
    ON ai_flags(review_status);
CREATE INDEX IF NOT EXISTS idx_ai_flags_run ON ai_flags(qa_run_id);
CREATE INDEX IF NOT EXISTS idx_ai_flags_provider ON ai_flags(provider);
