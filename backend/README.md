# FOIA Redaction Tool — Backend (technical reference)

> **First time setting up the system?** Start with the project-root
> [README.md](../README.md) — it has the step-by-step install,
> prerequisites for Windows / macOS / Linux, and a walkthrough of the
> bundled sample case. **This file is the per-phase technical
> reference**: schemas, endpoints, design tradeoffs, env vars, and
> testing notes.

Local-first FOIA redaction system for K–12 school districts. Built in
strict phases. Currently implemented:

- **Phase 1** — ingestion of `.mbox` files
- **Phase 2** — attachment processing + OCR
- **Phase 3** — PII detection + district YAML config + evaluation harness
- **Phase 4** — entity resolution (unified person records with manual override)
- **Phase 5** — FastAPI backend with pagination, filtering, and FTS5 full-text search
- **Phase 6** — non-destructive redaction system (spans, exemption codes, CRUD API, human approval gate)
- **Phase 7** — minimal React + TypeScript review UI (PII highlights, accept/reject, reviewer-name persisted)
- **Phase 8** — PDF export with burned-in redactions, Bates numbering, exemption-code stamps, and a per-redaction CSV log
- **Phase 9** — append-only audit log: every write across CLIs and API records actor + timestamp + payload, with DB-level UPDATE/DELETE blocked by triggers
- **Phase 10** — optional AI QA layer (Ollama / OpenAI / Azure / Anthropic) — advisory only; AI never auto-redacts, only flags are produced and a human must promote each one

## Phase 1 — Ingestion Engine

Standalone ingestion of `.mbox` mail files into a SQLite store.

Scope for this phase (nothing beyond):

- Parse emails: body (plain + sanitized HTML), headers
- Extract attachments: PDF, images, nested `.eml`
- Strip JavaScript, tracking pixels, external references from HTML bodies
- Persist in SQLite: `emails`, `attachments`, `raw_content`
- CLI only — no UI, no redaction, no AI

All ten phases are now in place. Future work is improvement, not
new-phase scaffolding.

## Phase 2 — Attachment Processing + OCR

Turns every attachment stored by Phase 1 into searchable text. Each
attachment gets exactly one row in `attachments_text` recording the
extraction method, status, optional page count, and OCR flag. The
original bytes on disk are never modified.

Handler routing by `content_type`:

| Content type                           | Handler                 | Notes                                                        |
|----------------------------------------|-------------------------|--------------------------------------------------------------|
| `application/pdf`                      | `pypdf`                 | Falls back to `pdf_ocr` (pypdfium2 + tesseract) for scans    |
| `image/*`                              | `ocr_tesseract`         | Pillow + pytesseract                                         |
| Office (docx/xlsx/pptx/odt/ods/odp/…)  | `libreoffice+pypdf`     | `soffice --headless --convert-to pdf`, then PDF handler      |
| `message/rfc822`                       | `eml_body`              | Parses nested email, emits From/To/Subject/Date + plain body |
| `text/plain`                           | `text`                  | BOM-sniffed; utf-8 → cp1252 → latin-1                        |
| `text/html`                            | `html`                  | Reuses sanitizer.html_to_text                                |
| anything else                          | `skipped` (unsupported) | Logged, not retried                                          |

A PDF with fewer than ~20 characters per page is treated as scanned and
routed through OCR when it is enabled.

### Binary dependencies (optional at runtime)

OCR and office conversion need external binaries. They are not shipped
by the Python wheels:

- **Tesseract** — install from
  [UB-Mannheim's builds](https://github.com/UB-Mannheim/tesseract/wiki)
  on Windows, `brew install tesseract` on macOS, or the distro package.
  Point `FOIA_TESSERACT_CMD` at the binary if it is not on `PATH`.
- **LibreOffice** — install it so `soffice` is resolvable. On Windows
  set `FOIA_LIBREOFFICE_CMD` to the full path of `soffice.exe`.

If either binary is missing, the corresponding attachments are marked
`failed` in `attachments_text` with a clean error message. No other
attachments are affected.

### Run

After ingesting with Phase 1, from `backend/`:

```bash
python extract.py                              # process everything new
python extract.py --force                      # re-process everything
python extract.py --attachment-id 7            # just one
python extract.py --no-ocr --no-office         # text-layer / eml only
```

The CLI prints a JSON summary:

```json
{
  "total": 3,
  "extracted_ok": 1,
  "extracted_empty": 0,
  "unsupported": 0,
  "failed": 2,
  "skipped_already_done": 0
}
```

### Phase 2 env vars

| Variable                      | Default   | Purpose                                                 |
|-------------------------------|-----------|---------------------------------------------------------|
| `FOIA_OCR_ENABLED`            | `true`    | Master toggle for tesseract                             |
| `FOIA_OCR_LANG`               | `eng`     | Tesseract language code                                 |
| `FOIA_OCR_DPI`                | `200`     | Rasterization DPI for scanned-PDF OCR                   |
| `FOIA_TESSERACT_CMD`          | *(auto)*  | Override tesseract binary path                          |
| `FOIA_OFFICE_ENABLED`         | `true`    | Master toggle for LibreOffice conversion                |
| `FOIA_LIBREOFFICE_CMD`        | `soffice` | LibreOffice CLI (absolute path on Windows)              |
| `FOIA_EXTRACTION_TIMEOUT_S`   | `180`     | Per-subprocess timeout in seconds                       |

## Phase 3 — PII Detection + Evaluation

Scans every piece of text produced by Phase 1 and Phase 2 — email
subjects, plain-text bodies, sanitized-HTML bodies, and extracted
attachment text — for personally identifiable information. Every hit
lands in `pii_detections` with entity type, span, score, and recognizer
name.

The spec says **"missing PII is unacceptable"**, so the detector is
tuned for recall: low default `min_score` (0.3), all pattern-based
Presidio recognizers on by default, custom district patterns layered on
top. False-positive dual-classifications (e.g. an 8-digit student ID
also pattern-matching as a phone number) are preserved — downstream
redaction needs every signal.

### District YAML

All district-specific settings live in one file (set
`FOIA_CONFIG_FILE`, default `./config/district.yaml`). The template is
[config/district.example.yaml](config/district.example.yaml). Top-level
keys consumed today:

- `district.name` and `district.email_domains`
- `pii_detection.builtins` — list of Presidio built-in recognizer names
  to enable
- `pii_detection.min_score` — drop detections below this score
- `pii_detection.enable_ner` + `pii_detection.ner_language` — opt into
  spaCy-backed PERSON detection (requires `en_core_web_sm`, see below)
- `pii_detection.custom_recognizers` — regex-based recognizers per
  district (student IDs, employee IDs, narrative dates, …)

Phase 6/8/10 keys (`exemption_codes`, `bates`, `ai`) are accepted today
and exposed via `DistrictConfig.raw` for later phases to consume
without another loader rewrite.

### Supported entity types (pattern only)

Enabled by default: `US_SSN`, `PHONE_NUMBER`, `EMAIL_ADDRESS`,
`DATE_TIME`, `CREDIT_CARD`, `US_DRIVER_LICENSE`, `US_BANK_NUMBER`.

Also mapped if you add them to `builtins`: `US_ITIN`, `US_PASSPORT`,
`IBAN_CODE`, `IP_ADDRESS`, `MEDICAL_LICENSE`, `URL`.

### Optional: PERSON name detection (spaCy)

The built-in PERSON path requires spaCy and a model. Install them once
per host:

```bash
.venv/Scripts/python -m pip install spacy
.venv/Scripts/python -m spacy download en_core_web_sm
```

Then set `pii_detection.enable_ner: true` in the district YAML. Until
then PERSON recall is 0% — the evaluation harness surfaces this
clearly, it is not a silent failure.

### Run

```bash
python detect.py                          # scan everything in the DB
python detect.py --email-id 12
python detect.py --attachment-id 4
python detect.py --config config/district.yaml
```

```bash
python evaluate.py --n 200 --seed 7       # score against synthetic K-12 data
```

### Evaluation baseline

`evaluate.py` generates a deterministic, labelled K-12 dataset (IEP
notes, field-trip slips, discipline reports, …), runs the detector, and
reports precision / recall / F1 per entity type. With the example
district config, on 200 seeded docs:

| Entity          | Precision | Recall | F1    |
|-----------------|-----------|--------|-------|
| US_SSN          | 1.00      | 1.00   | 1.00  |
| EMAIL_ADDRESS   | 1.00      | 1.00   | 1.00  |
| PHONE_NUMBER    | 0.98      | 1.00   | 0.99  |
| DATE_TIME       | 1.00      | 1.00   | 1.00  |
| STUDENT_ID      | 1.00      | 1.00   | 1.00  |
| LUNCH_ACCT      | 1.00      | 1.00   | 1.00  |
| PERSON (NER off)| 1.00      | 0.00   | 0.00  |

The PHONE_NUMBER precision gap is dual-classification: 8-digit student
IDs also fit Presidio's weak phone-number pattern. Both entity types
are emitted for the same span, which is desirable for redaction.

## Phase 4 — Entity Resolution

Unifies identities across the dataset. Every email address seen in
From / To / Cc / Bcc headers — plus any email addresses scraped from
body signatures — becomes a row in ``persons``, linked back to every
email where it appeared via ``person_occurrences``.

### Design choices (legally defensible defaults)

- **Match on canonical email only.** Same name, different emails → two
  distinct persons. Common names ("John Smith") would otherwise
  cross-merge unrelated people. Use `resolve merge` to combine records
  when you have out-of-band confirmation that two addresses belong to
  the same person.
- **No plus-address or Gmail-dot stripping.** `jane+foia@example.com`
  and `jane@example.com` stay separate unless manually merged.
- **Signatures contribute emails only**, not inferred names. The sig
  heuristic looks for a sign-off marker (`-- `, `Best,`, `Regards,`,
  …) and extracts email-shaped substrings from the block that follows.
- **`is_internal`** is set at insert time based on
  `district.email_domains` in the YAML. Subdomain matches count
  (`mail.district.org` counts as `district.org`).

### Tables

- `persons` — one row per unified identity; tracks `display_name`,
  `names_json` (all variants), `is_internal`, free-form `notes`
- `person_emails` — (`person_id`, `email`) with `email` as PK so the
  same address cannot belong to two people
- `person_occurrences` — one row per `(person, source_type, source_id)`
  where `source_type` ∈ {`email_from`, `email_to`, `email_cc`,
  `email_bcc`, `signature`}

### CLI

```bash
python resolve.py                           # default subcommand: `run`
python resolve.py run --email-id 42         # one email only
python resolve.py list                      # JSON list, sorted by occurrence count
python resolve.py show 7                    # detail view incl. all emails and counts
python resolve.py merge 12 7                # merge person 12 into person 7
python resolve.py rename 7 "Jane Doe"
python resolve.py note 7 "Primary principal contact."
```

`run` is idempotent: re-scanning an already-seen email produces no
changes; re-scanning after new ingestion only adds the delta.

`merge` reassigns emails and occurrences to the winner (duplicates are
collapsed), merges name variants, and deletes the loser row. There is
no `split` subcommand by design — reassigning emails away from a
merged person is better handled by manual DB edit so intent is
explicit.

## Phase 5 — Backend API

Read-only FastAPI over everything Phases 1–4 produced. The write path
stays in the CLIs (ingest / extract / detect / resolve) so the API is
always safe to expose internally, and so Phase 6 redaction logic can
land without a mass rewrite of endpoints.

### Run

```bash
python serve.py                           # 127.0.0.1:8000
python serve.py --host 0.0.0.0 --port 8080
python serve.py --reload                  # dev autoreload
```

OpenAPI docs at `http://<host>/docs` (Swagger UI) and
`/openapi.json` (raw schema). Inside a container, the default CMD
boots uvicorn on `0.0.0.0:8000`.

### Endpoints

Versioned under `/api/v1/`. Every list endpoint takes `limit` (1–500,
default 50) and `offset` (≥0, default 0) and returns a
`{items, total, limit, offset}` envelope.

| Method | Path                                | Notes                                                    |
|--------|-------------------------------------|----------------------------------------------------------|
| GET    | `/health`                           | Liveness + DB existence                                  |
| GET    | `/stats`                            | Row counts across emails/attachments/detections/persons  |
| GET    | `/api/v1/emails`                    | Filters: `from_contains`, `subject_contains`, `date_from`, `date_to`, `has_attachments`, `has_pii`, `mbox_source` |
| GET    | `/api/v1/emails/{id}`               | Full record incl. headers, body, attachments, PII spans  |
| GET    | `/api/v1/emails/{id}/raw`           | Original RFC822 bytes; `Content-Type: message/rfc822`    |
| GET    | `/api/v1/attachments`               | Filters: `email_id`, `content_type`, `content_type_prefix`, `extraction_status`, `only_inline` |
| GET    | `/api/v1/attachments/{id}`          | Metadata + extracted text                                |
| GET    | `/api/v1/attachments/{id}/download` | Original bytes (MIME-typed)                              |
| GET    | `/api/v1/detections`                | Filters: `entity_type`, `source_type`, `source_id`, `min_score` |
| GET    | `/api/v1/detections/entities`       | Aggregate counts per entity type                         |
| GET    | `/api/v1/persons`                   | Filters: `is_internal`, `email_domain`, `name_contains`  |
| GET    | `/api/v1/persons/{id}`              | Emails + occurrences-by-type                             |
| GET    | `/api/v1/search?q=…`                | FTS5 over emails + attachments; `<mark>…</mark>` snippets; `scope=emails\|attachments` |

### Search

FTS5 virtual tables cover `subject + body_text` on emails and
`filename + extracted_text` on attachments. User queries are quoted as
literal phrases internally, so punctuation in the input never triggers
FTS5 syntax errors. Results are ordered by BM25 rank and accompanied by
12-token snippets with `<mark>` / `</mark>` highlighting.

### Gotchas

- Dates are compared as lexical strings against the ISO 8601 UTC
  values in `date_sent`. Pass `2024-01-15T00:00:00+00:00`-shaped
  values; open-ended ranges work by omitting either bound.
- `has_pii=true` uses an `EXISTS` subquery against email-scoped
  detections only; attachment detections don't flip that flag on the
  parent email yet.
- `/api/v1/search` requires at least one non-whitespace term; whitespace-
  only queries return 400.

## Phase 6 — Redaction System (Core Legal Layer)

Non-destructive redactions stored as spans into the canonical text.
Source bytes never change. Every redaction lives in one of three
states — `proposed`, `accepted`, `rejected` — and a reviewer is
required to leave the `proposed` state. Phase 8's PDF export will
read only `accepted` rows.

### Schema (`redactions`)

| Column                | Notes                                                                  |
|-----------------------|------------------------------------------------------------------------|
| `source_type`         | `email_subject` / `email_body_text` / `email_body_html` / `attachment_text` |
| `source_id`           | references `emails.id` or `attachments.id` depending on `source_type`  |
| `start_offset`, `end_offset` | character offsets into the canonical text; `0 ≤ start < end ≤ len(text)` |
| `exemption_code`      | must appear in `district.exemption_codes`                              |
| `status`              | CHECK-constrained to `proposed` / `accepted` / `rejected`              |
| `origin`              | `auto` (created from a PII detection) or `manual`                      |
| `source_detection_id` | nullable FK to `pii_detections.id`; `ON DELETE SET NULL`               |
| `reviewer_id`         | required to set status=`accepted` or `rejected`                        |
| `notes`               | free-form, optional                                                    |

A `UNIQUE (source_type, source_id, start_offset, end_offset, exemption_code)`
index makes the propose flow idempotent.

### District YAML

```yaml
exemption_codes:
  - code: FERPA
    description: "Family Educational Rights and Privacy Act"
  - code: PII
    description: "..."
redaction:
  default_exemption: FERPA
  entity_exemptions:
    US_SSN: PII
    CREDIT_CARD: PII
    EMAIL_ADDRESS: FERPA
    STUDENT_ID: FERPA
```

`redact propose` looks up each PII detection's `entity_type` in
`entity_exemptions`, falling back to `default_exemption`. If neither
is set for a given entity, the detection is reported as
`skipped_no_exemption` instead of being silently dropped.

### CLI

```bash
python redact.py propose                                # seed proposals from PII detections
python redact.py list --status proposed
python redact.py show 17
python redact.py accept 17 --reviewer "Records Clerk"
python redact.py reject 18 --reviewer "Records Clerk" --note "Public record"
python redact.py delete 19
python redact.py exemptions                             # list configured codes
```

`propose` is idempotent — re-running adds only new detections (counted
as `skipped_existing` in the JSON summary).

### API (writes!)

This is the first router that mutates state. Phase 5 endpoints stay
read-only; only `/redactions` and a couple of meta routes accept
`POST` / `PATCH` / `DELETE`.

| Method | Path                                | Notes                                                           |
|--------|-------------------------------------|-----------------------------------------------------------------|
| GET    | `/api/v1/exemption-codes`           | The codes the district allows                                   |
| GET    | `/api/v1/redactions`                | Filters: `source_type`, `source_id`, `status`, `origin`, `exemption_code` |
| GET    | `/api/v1/redactions/{id}`           |                                                                  |
| POST   | `/api/v1/redactions`                | `extra="forbid"` on the body — unknown fields ⇒ 422             |
| PATCH  | `/api/v1/redactions/{id}`           | Update `status` / `exemption_code` / `reviewer_id` / `notes`    |
| DELETE | `/api/v1/redactions/{id}`           | 204 on success                                                  |

Validation rules are shared between the CLI and HTTP layer (one
`validate_new_redaction` function in `foia.redaction`):

- `source_type` ∈ the four valid values
- `0 ≤ start_offset < end_offset ≤ len(source_text)`
- the source row exists
- `exemption_code` must be configured (or, if no `exemption_codes`
  list at all, any non-empty string is accepted so districts can
  bootstrap)
- `status='accepted'` or `status='rejected'` requires `reviewer_id`

Schema-level CHECK constraints back the offset / status / origin /
source_type rules so even a hand-written `INSERT` cannot bypass them.

## Phase 7 — Minimal Review UI

A small React + TypeScript app under [`frontend/`](../frontend/) that
talks to the Phase 5/6 API. Two pages, two components, no global state
library, no UI framework — just enough to read emails and accept or
reject the proposed redactions.

### Run

```bash
# Terminal 1 — backend
cd backend
python serve.py --port 8000

# Terminal 2 — frontend
cd frontend
npm install
npm run dev          # opens http://localhost:5173
```

The Vite dev server proxies `/api`, `/health`, `/stats` to the FastAPI
backend at `http://127.0.0.1:8000` (override with the
`VITE_BACKEND` env var when starting `npm run dev`). The backend's
`FOIA_CORS_ORIGINS` defaults to `http://localhost:5173` so a
non-proxied build can also call it.

### What's there

- `/emails` — paginated list with subject search, "with PII only"
  filter, and stats summary in the header
- `/emails/:id` — headers, attachments table, and three text panes
  (subject, plain body, sanitized HTML body) with redaction overlays
- Click any highlighted span to open a popover with **Accept** /
  **Reject** / exemption-code picker / note field
- The reviewer name lives in the page header and persists to
  `localStorage`; transitions to `accepted` / `rejected` send it as
  `reviewer_id` to satisfy the Phase 6 requirement

Color legend:

| Color  | Meaning   |
|--------|-----------|
| yellow | proposed  |
| black  | accepted  |
| dashed grey | rejected  |

### Build a static bundle

```bash
cd frontend
npm run build        # outputs dist/
npm run preview      # serve the bundle locally for a smoke test
```

`npm run typecheck` runs `tsc -b --noEmit` over the source. The
project compiles clean against TypeScript 5.6 in strict mode with
`noUnusedLocals` / `noUnusedParameters`.

### Deliberate non-goals (Phase 7)

- No CSS framework (Tailwind / MUI / etc.)
- No data-fetching library (TanStack Query, SWR)
- No state-management library (Redux, Zustand)
- No frontend test runner — backend tests cover the contract; the UI
  is small enough to verify by hand against the seeded sample DB

## Phase 8 — PDF Export

Generates a "production" PDF where every accepted redaction is burned
in (text never reaches the file), every page carries a sequential
Bates label, and exemption codes are stamped in white inside the
black box. A companion CSV maps each redaction back to its source row
for the official redaction log.

### Implementation choice

The architecture overview lists WeasyPrint as the PDF tool. We use
**ReportLab** instead because:

1. it's pure Python (no native cairo/pango/gdk-pixbuf chain on Windows
   or in the slim container);
2. it gives precise per-character positioning, which is what
   offset-based redaction placement actually needs (HTML/CSS
   layout makes it hard to map a `start_offset` to pixels reliably).

The text is rendered in monospaced **Courier** so character widths are
stable, the line-wrap is offset-preserving, and placing the black box
is just `n_chars * char_width`. Burning is done by *not drawing* the
redacted segments — we split each line into visible halves and only
emit the visible parts, then drop the box on the gap. The CSV log
records the Bates label of the page where each redaction was burned.

### Bates configuration

Lives in the district YAML:

```yaml
bates:
  prefix: ECPS
  start: 1
  width: 6   # zero-padded
```

`width` must be 1–20. The page label format is
`{prefix}-{n:0{width}d}` — e.g. `ECPS-000001`.

### CLI

```bash
python export.py --out exports/2026-04-27/                # everything in scope
python export.py --out exports/case-1234/ --emails 1,2,3  # filter by email id
python export.py --out exports/case-1234/ --no-attachments
```

Produces under `--out`:

- `production.pdf` — every email + extracted attachment text, Bates-numbered
- `redaction_log.csv` — one row per accepted redaction with
  `bates_label, redaction_id, source_type, source_id, source_label,
   start_offset, end_offset, length, exemption_code, reviewer_id, accepted_at`

Only `status='accepted'` redactions are burned. `proposed` and
`rejected` rows are ignored (the spec's "human approval required"
rule).

### API

| Method | Path                                         | Notes                                                   |
|--------|----------------------------------------------|---------------------------------------------------------|
| POST   | `/api/v1/exports`                            | Body: `{email_ids?: int[], include_attachments?: bool}`; returns a manifest |
| GET    | `/api/v1/exports`                            | Lists previously generated exports (newest first)       |
| GET    | `/api/v1/exports/{export_id}/production.pdf` | Download PDF                                            |
| GET    | `/api/v1/exports/{export_id}/redaction_log.csv` | Download CSV                                          |

Each POST writes to a fresh `{FOIA_EXPORT_DIR}/{export_id}/`
subdirectory; previously generated exports stay around. The download
endpoint is path-traversal hardened — only the two known filenames
under a real export id resolve.

### From the UI

The email-list page has an **Export PDF** button next to the search
bar. Clicking it POSTs to `/api/v1/exports`, opens the produced PDF
in a new tab, and shows a brief manifest summary.

### Sample output

After `ingest → extract → detect → propose → accept`, the smoke run
on the bundled fixture produces:

```
data/exports/smoke/
├── production.pdf      # 7 pages, Bates ECPS-000001..ECPS-000007
└── redaction_log.csv
```

The redaction log has one row per burned span and the produced PDF's
text layer never contains the redacted strings — verified in the test
suite by running pypdf over the output.

## Phase 9 — Audit Logging

Every write across the system — ingestion, extraction, detection,
entity resolution, redaction CRUD, exports — appends one row to the
`audit_log` table. Reads (Phase 5 list/detail endpoints) are not
logged, but the `actor` is captured on writes regardless of source.

### Immutability

The schema installs two BEFORE triggers:

```sql
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
  FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
  FOR EACH ROW BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
```

Direct SQL `UPDATE` or `DELETE` on `audit_log` raises an
`IntegrityError` and rolls back. Application code only ever
`INSERT`s.

### Schema

| Column           | Notes                                                          |
|------------------|----------------------------------------------------------------|
| `event_at`       | ISO 8601 UTC, set by the writer                                |
| `event_type`     | `ingest.run`, `extract.run`, `detection.run`, `resolve.run`, `resolve.merge`, `resolve.rename`, `resolve.note`, `redaction.propose`, `redaction.create`, `redaction.update`, `redaction.delete`, `export.run` |
| `actor`          | Resolved per the rules below; never NULL                       |
| `source_type` / `source_id` | The affected entity (`redaction`, `person`, `email`, `attachment`, `mbox`, `export`) |
| `payload_json`   | JSON object with operation-specific details (counts, before/after, scope filters) |
| `request_origin` | `cli` \| `api` \| `system`                                     |

### Actor resolution

| Source | Lookup order |
|--------|--------------|
| **CLI** | `--actor` flag → `FOIA_ACTOR` env → `cli:{getpass.getuser()}` |
| **API** | `X-FOIA-Reviewer` request header → `api:anonymous` |

The Phase 7 UI already keeps the reviewer name in `localStorage`. The
fetch wrapper sends it as `X-FOIA-Reviewer` on every call (reads as
well as writes), so the header is set the moment a name is typed.

### Read API

```
GET /api/v1/audit
    ?event_type=…&actor=…&source_type=…&source_id=…
    &after=…&before=…&origin=cli|api|system
    &limit=50&offset=0
```

Returns `{items, total, limit, offset}` newest-first. Each `item`
includes a parsed `payload` dict (decoded from `payload_json`).

There is **no** `POST /api/v1/audit` and never will be — application
code is the only authority that writes to this table, via
`foia.audit.log_event`.

### Worked example

```bash
# Same pipeline, different actors
python ingest.py  --file inbox.mbox --actor "alice"
python extract.py                    --actor "bob"
python detect.py                     --actor "carol"
python redact.py propose             --actor "dan"
python redact.py accept 17 --reviewer "Records Clerk" --actor "eve"
python export.py  --out exports/run-1 --actor "frank"
```

Then query via the API after authenticating as the UI user:

```bash
curl -H 'X-FOIA-Reviewer: Inspector Janet' http://localhost:8000/api/v1/audit
```

### Why not log reads too?

Phase 9's spec says "track ingestion, detection, redactions, edits,
exports". Reads aren't on that list and would balloon the log on a
busy review session without adding evidentiary value. The actor still
flows through every read header so it's trivial to opt-in later
without a schema change.

## Phase 10 — Optional AI QA Layer

Pluggable LLM-assisted review. Runs in advisory mode only — the spec's
hard rule "AI never auto-redacts" is enforced at every layer.

### Hard rule, enforced

There is no path from an AI flag to an `accepted` redaction without a
human action. The flow is:

```
AI provider → ai_flags (status=open)
     │
     │  human action: qa.py promote OR POST /api/v1/ai-flags/{id}/promote
     ▼
redactions (status=proposed, origin=manual)
     │
     │  human action: redact.py accept OR PATCH /api/v1/redactions/{id}
     ▼
redactions (status=accepted)
     │
     ▼
production.pdf (Phase 8 burns only accepted rows)
```

Two human gates separate the LLM output from a burned redaction.

### Pluggable providers

| Provider name | Backend                       | Notes                                                                |
|---------------|-------------------------------|----------------------------------------------------------------------|
| `null`        | none — always returns []      | Default. Safe to keep `enabled: true` while experimenting.           |
| `openai`      | OpenAI `/v1/chat/completions` | Bearer auth from `FOIA_AI_API_KEY`.                                  |
| `azure`       | Azure OpenAI                  | Same wire format; uses `api-key` header. `base_url` to your deployment. |
| `ollama`      | Local Ollama at `:11434/v1`   | OpenAI-compatible; no API key required.                              |
| `anthropic`   | Anthropic `/v1/messages`      | `x-api-key` + `anthropic-version` headers.                           |

The OpenAI / Azure / Ollama path lives in one `OpenAICompatibleProvider`
class; Anthropic has a small dedicated class for its different request
shape. New providers are a constructor away.

### District YAML

```yaml
ai:
  enabled: false                  # off by default
  provider: null                  # null | openai | anthropic | azure | ollama
  model: gpt-4o-mini              # provider-default applies if omitted
  base_url: null                  # provider-default applies if omitted
  api_key_env: FOIA_AI_API_KEY    # which env var holds the key
  max_input_chars: 8000           # truncate long bodies before sending
  request_timeout_s: 60
```

### Per-case overrides

Both the CLI and the API let a single run pick a different provider /
model from the YAML default — useful for spot-checks against a more
expensive model:

```bash
python qa.py run --provider anthropic --model claude-3-5-sonnet-latest
```

```bash
curl -X POST -H "X-FOIA-Reviewer: alice" \
     -d '{"provider": "ollama", "model": "llama3.1:70b"}' \
     http://localhost:8000/api/v1/ai-flags/run
```

### Schema

`ai_flags` table with fields: `entity_type`, `start_offset`,
`end_offset`, `matched_text`, `confidence`, `rationale`,
`suggested_exemption`, `provider`, `model`, `qa_run_id`,
`flagged_at`, `review_status`, `review_actor`, `reviewed_at`,
`review_note`, `promoted_redaction_id`. CHECK constraints enforce
`start < end`, valid `review_status` (`open`/`dismissed`/`promoted`)
and valid `source_type`. `UNIQUE(source_type, source_id, start, end,
entity_type, provider)` makes runs idempotent. The
`promoted_redaction_id` FK uses `ON DELETE SET NULL` so deleting the
generated redaction never breaks the flag's audit trail.

### Reliability quirks handled

- Models often return JSON wrapped in prose or fenced blocks — the
  parser tolerates both.
- Models often return character offsets that are wrong — every flag is
  re-anchored by exact-substring search before it lands in the DB. If
  the `matched_text` isn't actually present in the source, the flag is
  dropped (not stored under bogus offsets).
- Confidence values are clamped to `[0.0, 1.0]`; non-numeric values
  collapse to `0.5`.

### CLI

```bash
python qa.py run --actor analyst             # full corpus
python qa.py run --email-id 7
python qa.py run --provider null             # safe dry-run
python qa.py list --status open
python qa.py show 17
python qa.py promote 17 --actor "Records Clerk"
python qa.py dismiss 18 --actor "Records Clerk" --note "false positive"
```

### API

| Method | Path                                       | Notes                                                     |
|--------|--------------------------------------------|-----------------------------------------------------------|
| GET    | `/api/v1/ai-flags`                         | Filters: `status`, `source_type`, `source_id`, `entity_type`, `provider`, `qa_run_id` |
| GET    | `/api/v1/ai-flags/{id}`                    |                                                           |
| POST   | `/api/v1/ai-flags/run`                     | Body: `{email_id?, attachment_id?, provider?, model?}`    |
| PATCH  | `/api/v1/ai-flags/{id}/dismiss`            | Body: `{note?}`                                           |
| POST   | `/api/v1/ai-flags/{id}/promote`            | Body: `{exemption_code?, note?}` — returns the new redaction |

Each write logs the actor (from `X-FOIA-Reviewer`) into Phase 9's
`audit_log` with `event_type` = `ai_qa.run` / `ai_qa.dismiss` / `ai_qa.promote`.

### From the UI

The email detail page now shows a "AI risk flags" section listing
every flag for that email, with **Promote** and **Dismiss** buttons.
Promote creates a `proposed` redaction visible immediately in the
existing PII overlay; the reviewer still has to Accept it through the
Phase 7 popover before it's burned by Phase 8.

## Docker (Phase 3 deliverable)

A backend Dockerfile is provided at
[backend/Dockerfile](Dockerfile). The image bakes in tesseract,
LibreOffice, and poppler-utils so all Phase 2 handlers work
unconditionally inside the container, and its default `CMD` launches
the Phase 5 API on `0.0.0.0:8000`.

Build:

```bash
docker build -t foia-backend -f backend/Dockerfile backend
```

Run the API server:

```bash
docker run --rm -p 8000:8000 \
  -v "$PWD/data:/data" \
  -v "$PWD/backend/config:/config:ro" \
  -e FOIA_CONFIG_FILE=/config/district.yaml \
  foia-backend
```

Or run a one-shot CLI task (overrides the default CMD):

```bash
docker run --rm \
  -v "$PWD/data:/data" \
  -v "$PWD/backend/config:/config:ro" \
  -e FOIA_CONFIG_FILE=/config/district.yaml \
  foia-backend \
  python ingest.py --file /data/incoming.mbox
```

Phase 6+ will add a full Docker Compose stack (backend + frontend).

## Repository layout

```
backend/
  foia/
    __init__.py
    config.py            # env-backed deploy configuration
    db.py                # SQLite connect + schema init
    schema.sql           # DDL (emails, attachments, raw_content,
                         #      attachments_text, pii_detections)
    ingestion.py         # mbox parser + attachment extractor             (Phase 1)
    sanitizer.py         # HTML sanitization                              (Phase 1)
    extraction.py        # per-type text handlers (PDF/OCR/office/eml)    (Phase 2)
    processing.py        # batch extraction driver                        (Phase 2)
    district.py          # YAML district-config loader                    (Phase 3)
    detection.py         # PII detector (Presidio PatternRecognizers)     (Phase 3)
    detection_driver.py  # DB-backed scan of emails + attachment_text     (Phase 3)
    evaluation.py        # synthetic K-12 dataset + precision/recall       (Phase 3)
    entity_resolution.py # address/signature/name normalization           (Phase 4)
    er_driver.py         # DB-backed person builder + manual ops           (Phase 4)
    api/                 # FastAPI app + routes + pydantic schemas         (Phase 5)
    redaction.py         # validation + propose-from-detections            (Phase 6)
    export.py            # burned-in PDF + Bates + CSV log                 (Phase 8)
    audit.py             # append-only event log + actor resolution        (Phase 9)
    ai.py                # provider ABC + Null/OpenAI-compat/Anthropic     (Phase 10)
    ai_driver.py         # DB-backed scan + dismiss/promote                (Phase 10)
  config/
    district.example.yaml
  scripts/
    generate_sample_mbox.py
  tests/                 # 340 tests across phases 1–10
  ingest.py              # Phase 1 CLI
  extract.py             # Phase 2 CLI
  detect.py              # Phase 3 CLI
  evaluate.py            # Phase 3 evaluation CLI
  resolve.py             # Phase 4 CLI (entity resolution + manual ops)
  serve.py               # Phase 5 API launcher (uvicorn)
  redact.py              # Phase 6 CLI (propose / accept / reject / list / show)
  export.py              # Phase 8 CLI (redacted PDF + CSV log)
  qa.py                  # Phase 10 CLI (AI QA: run / list / dismiss / promote)
  Dockerfile             # backend image (Phase 3 deliverable)
  requirements.txt
  requirements-dev.txt   # tests pull from requirements.txt
  pyproject.toml
  .env.example
```

Runtime data (the SQLite database and extracted attachment bytes) lands
under `data/` by default and is git-ignored.

## Local setup

Python 3.12+ recommended.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r backend/requirements.txt
# For running the test suite (adds reportlab):
.venv/Scripts/python -m pip install -r backend/requirements-dev.txt
```

Copy [backend/.env.example](.env.example) to `backend/.env` and edit if
you want to override defaults.

## Run

From `backend/`:

```bash
python scripts/generate_sample_mbox.py tests/fixtures/sample.mbox
python ingest.py --file tests/fixtures/sample.mbox
```

The CLI prints an ingestion summary as JSON, for example:

```json
{
  "mbox_source": "D:/.../tests/fixtures/sample.mbox",
  "emails_ingested": 5,
  "emails_skipped_duplicate": 0,
  "attachments_saved": 3,
  "errors": 0
}
```

Overrides are available per-invocation:

```bash
python ingest.py --file some.mbox \
  --db ./data/custom.db \
  --attachments ./data/custom_attachments \
  --label "district-xyz-2026-01"
```

## Configuration

All tunables come from environment variables (see `.env.example`):

| Variable                | Default                 | Purpose                                 |
|-------------------------|-------------------------|-----------------------------------------|
| `FOIA_DB_PATH`          | `./data/foia.db`        | SQLite database location                |
| `FOIA_ATTACHMENT_DIR`   | `./data/attachments`    | Where extracted attachment bytes live   |
| `FOIA_LOG_LEVEL`        | `INFO`                  | Python logging level                    |

No paths are hard-coded in the source. A later phase wires this into a
single district YAML; phase 1 intentionally stays env-only.

## Data model (Phase 1)

All timestamps are ISO-8601 UTC. Addresses are stored as JSON arrays so
entity resolution in phase 4 can operate on structured data.

- `emails` — one row per ingested message. `(mbox_source, mbox_index)`
  is the unique identifier; re-running ingestion on the same file is a
  no-op (duplicates are counted in `emails_skipped_duplicate`).
- `attachments` — one row per extracted part. Deduped on disk by
  `sha256`; the DB row still records every occurrence (same file sent
  twice ⇒ two DB rows, one file on disk).
- `raw_content` — the original RFC822 bytes of the message, plus a
  sha256 for integrity checks. **This table is the source of truth.**
  Nothing later in the pipeline is allowed to mutate it.

The schema enables `PRAGMA foreign_keys = ON` and uses `ON DELETE
CASCADE` from `emails` to the other two tables, so removing a dataset
for a district is a single `DELETE FROM emails WHERE mbox_source = ?`.

## HTML sanitization rules

The `body_html_sanitized` column contains a display-safe copy of the
HTML part. Sanitization is done in two passes:

1. BeautifulSoup structural pre-pass drops `<script>`, `<style>`,
   `<iframe>`, `<object>`, `<embed>`, `<link>`, `<meta>`, `<base>`,
   `<svg>`, `<canvas>`, `<img>`, `<picture>`, `<video>`, `<audio>`,
   `<form>`, `<input>`, `<button>`, and any `on*` event attributes.
2. `bleach.clean` enforces a strict tag/attribute allowlist and
   protocol allowlist (`http`, `https`, `mailto`).

This eliminates tracking pixels (by virtue of removing all `<img>`),
JavaScript in `href="javascript:..."` and inline handlers, and
externally loaded resources. The original, untouched HTML remains in
`raw_content` — sanitization is lossy on purpose.

## Tests

```bash
cd backend
../.venv/Scripts/python -m pytest
```

340 tests across all ten phases cover the DB schema (FTS5 + Phase 6
CHECK constraints + cascade rules), HTML sanitizer, mbox parser, Phase
1 CLI, Phase 2 text handlers (PDF, image OCR, office, eml, text, html)
and batch processor, Phase 2 CLI, YAML config parsing
(incl. exemption codes + redaction mapping), PII detection engine and
driver, evaluation harness, Phase 3 CLIs, entity-resolution
primitives, the Phase 4 driver, Phase 4 CLI, every Phase 5 endpoint
(pagination, filter combinations, 404s, FTS5 snippets, OpenAPI
completeness), and the full Phase 6 surface — validation (offset
range, exemption allowlist, reviewer requirement on accept/reject),
schema-level CHECK enforcement, propose-from-detections idempotency,
the CRUD endpoints (including `extra="forbid"` rejection of unknown
fields), the redact CLI, and the Phase 7 backend additions
(`/api/v1/emails/{id}/redactions` and CORS preflight handling for
configured / disallowed / disabled origins), Phase 8 export —
end-to-end PDF rendering through pypdf assertions (redacted strings
absent from the text layer, surrounding context preserved, accepted
status only, Bates labels per page, CSV columns, path-traversal
guards on the download endpoint, and OpenAPI completeness),
Phase 9 audit — append-only triggers (UPDATE / DELETE both
raise), actor resolution (`--actor` / `FOIA_ACTOR` / username for
CLI; `X-FOIA-Reviewer` / `api:anonymous` for API), every CLI hook
emitting its expected event, every API write logging origin=`api`
with the header value as actor, and the read endpoint's pagination /
filter / `after` / `before` semantics; and Phase 10 AI QA — provider
JSON parsing (fenced, embedded, malformed), confidence clamping,
re-anchoring of LLM-returned offsets via exact-substring search, the
factory's per-case overrides and required-API-key checks, the driver's
idempotent UNIQUE handling, dismiss/promote state-machine guards
(can't promote dismissed, can't dismiss promoted, can't double-promote),
and the central rule expressed as a test: **promoted AI flag → proposed
redaction → still requires reviewer to Accept**.

OCR, LibreOffice, and spaCy NER handlers are **mocked** in tests so
the suite runs clean on a vanilla Python install after
`pip install -r requirements-dev.txt`. Presidio pattern recognizers
run for real — no mocks — so any regression in Presidio behavior will
be caught.

Real PDFs for the PDF tests are built at runtime via reportlab. No
binary fixtures are checked into the repo.

## Container-readiness

Phase 1 is intentionally local-only, but the structure is already set
up for the later Docker Compose stack:

- No hard-coded paths — everything routes through env vars
- Attachments stored on a configurable directory (mountable as a volume)
- SQLite path is configurable (mountable as a volume)
- Dependencies pinned in `requirements.txt`

The backend `Dockerfile` (added in Phase 3) bakes in tesseract and
LibreOffice so the optional binary deps become mandatory in the image.
See the Docker section above.
