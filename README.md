# FOIA Redaction Tool

A local-first system for processing FOIA (Freedom of Information Act) email
productions for K–12 school districts. Takes a Microsoft `.mbox` mailbox file,
extracts text from attachments, finds PII automatically, lets a human review
and accept proposed redactions, and produces a court-ready PDF with the
redactions burned in and Bates-numbered.

The whole stack runs on a single laptop. No cloud account is required (the AI
QA layer is optional and disabled by default).

## What the system does, end-to-end

```
.mbox file
   │
   │  ingest.py       Parse emails + extract attachments to disk + SQLite
   ▼
SQLite database (data/foia.db)  +  attachments/ on disk
   │
   │  extract.py      OCR images, parse PDFs, convert Office docs to text
   ▼
attachments_text table
   │
   │  detect.py       Find SSN / phones / emails / dates / student IDs
   ▼
pii_detections table
   │
   │  resolve.py      Group emails to unified "people"
   ▼
persons table
   │
   │  redact.py propose  Auto-create proposed redactions from PII detections
   ▼
redactions table  (status='proposed')
   │
   │  Web UI: review each one, accept or reject (reviewer name required)
   ▼
redactions table  (status='accepted')
   │
   │  export.py       Burn black boxes into a Bates-numbered PDF
   ▼
production.pdf  +  redaction_log.csv
```

Everything is auditable: a separate `audit_log` table records who did what
and when, and the SQL trigger blocks anyone from editing it after the fact.

---

## Prerequisites

You need three things on the host machine. The first two are required; the
third is optional.

### 1. Python 3.12 or newer (required)

| OS       | How to install                                                                                  |
|----------|-------------------------------------------------------------------------------------------------|
| Windows  | Download from <https://www.python.org/downloads/windows/>. **Tick "Add python.exe to PATH"** during the installer. |
| macOS    | `brew install python@3.12` (after installing Homebrew from <https://brew.sh>)                   |
| Linux    | `sudo apt install python3.12 python3.12-venv` on Debian / Ubuntu, or your distro equivalent     |

Verify it worked by opening a fresh terminal and running:

```bash
python --version
```

You should see `Python 3.12.x` or higher. (On some macOS / Linux installs you
may need to type `python3` instead of `python`.)

### 2. Node.js 18 or newer (required only if you use the web UI)

The CLI tools alone do not need Node. Install Node only if you want to use
the review UI in your browser.

| OS       | How to install                                                       |
|----------|----------------------------------------------------------------------|
| Windows  | Download the LTS installer from <https://nodejs.org/>                |
| macOS    | `brew install node`                                                  |
| Linux    | `sudo apt install nodejs npm` or use [nvm](https://github.com/nvm-sh/nvm) |

Verify:

```bash
node --version    # v18.x or newer
npm --version
```

### 3. Optional binaries — Tesseract and LibreOffice

These power Phase 2's OCR (for scanned PDFs and images) and Office-document
conversion. **The system runs fine without them**; attachments that need them
just get marked as `failed` in the database with a clean error message and
the rest of the pipeline keeps going.

Install them only if your real FOIA dataset has scanned PDFs, images of
text, or `.docx` / `.xlsx` / `.pptx` attachments.

| OS       | Tesseract                                                                       | LibreOffice                          |
|----------|---------------------------------------------------------------------------------|--------------------------------------|
| Windows  | UB-Mannheim build: <https://github.com/UB-Mannheim/tesseract/wiki>              | <https://www.libreoffice.org/download/> |
| macOS    | `brew install tesseract`                                                        | `brew install --cask libreoffice`    |
| Linux    | `sudo apt install tesseract-ocr`                                                | `sudo apt install libreoffice`       |

After installing on Windows, you may need to point the tool at the binaries
because they are often not on `PATH`. See [Configuration](#configuration)
below.

---

## Installation

These steps assume you have the project folder somewhere on disk. The
example uses `D:\Apps\PaperClip\` on Windows; substitute the actual path.

### Step 1 — Open a terminal in the project root

```bash
cd D:\Apps\PaperClip      # Windows
cd ~/PaperClip            # macOS / Linux
```

You should be sitting at the level that contains the `backend/` and
`frontend/` folders.

### Step 2 — Create a Python virtual environment

A virtual environment keeps this project's Python packages separate from
any other Python work on the machine.

```bash
python -m venv .venv
```

This creates a `.venv/` folder. **Do not delete it.**

### Step 3 — Activate the virtual environment

This step has to be repeated every time you open a new terminal.

| OS                       | Command                          |
|--------------------------|----------------------------------|
| Windows (Command Prompt) | `.venv\Scripts\activate.bat`     |
| Windows (PowerShell)     | `.venv\Scripts\Activate.ps1`     |
| Windows (Git Bash / WSL) | `source .venv/Scripts/activate`  |
| macOS / Linux            | `source .venv/bin/activate`      |

After activation, your prompt should show `(.venv)` at the front. From now
on, when this guide says "run `python …`", it means with the venv active.

### Step 4 — Install Python dependencies

```bash
python -m pip install --upgrade pip
python -m pip install -r backend/requirements.txt
```

The `requirements.txt` install pulls in about 50 packages (Presidio brings in
spaCy as a transitive dep but no language model is downloaded). Expect
30–90 seconds depending on your network.

> If you plan to **run the test suite**, also do
> `python -m pip install -r backend/requirements-dev.txt` — it currently
> just re-uses `requirements.txt`, so the previous step is enough on its own.

### Step 5 — Install the frontend (skip if you only use CLIs)

```bash
cd frontend
npm install
cd ..
```

This creates `frontend/node_modules/` (also do not delete) and pulls in
React, Vite, TypeScript, and a couple of small support libraries.

### Step 6 — Verify the install

From the **project root**, with the venv active:

```bash
cd backend
python -m pytest -q
cd ..
```

Expected: a line that ends with `340 passed` (or higher). If pytest reports
collection errors, your venv is probably not active — go back to Step 3.

---

## Your first FOIA case (using the bundled sample)

The repo ships with a synthetic 5-message `.mbox` generator so you can walk
through every phase before touching real data.

Throughout this section, run all commands from inside `backend/` with the
virtual environment active:

```bash
cd backend
```

### 1. Generate the sample mailbox

```bash
python scripts/generate_sample_mbox.py tests/fixtures/sample.mbox
```

Output: `Wrote 5 messages to tests/fixtures/sample.mbox`. The file contains
a plain text email, an HTML newsletter with tracking pixels (which the
sanitizer strips), an email with a fake PDF attachment, an email with a
PNG image, and an email with a forwarded `.eml` inside.

### 2. Ingest the mailbox

```bash
python ingest.py --file tests/fixtures/sample.mbox --actor "your-name"
```

Expected output (numbers may vary):

```json
{
  "mbox_source": "...sample.mbox",
  "emails_ingested": 5,
  "emails_skipped_duplicate": 0,
  "attachments_saved": 3,
  "errors": 0
}
```

The `--actor` flag tags every audit-log row with your name. It's optional;
if omitted, the actor defaults to `cli:<your-username>`.

The first run creates `backend/data/foia.db` (the SQLite database) and
`backend/data/attachments/` (where attachment bytes live). Re-running on
the same `.mbox` is safe — duplicate messages are skipped.

### 3. Extract searchable text from attachments

```bash
python extract.py --actor "your-name"
```

If you didn't install Tesseract or LibreOffice, you'll see two `failed`
entries — those are the image and the fake PDF, which both need binaries
this guide marked optional. The `eml_body` extraction of the nested
forwarded email succeeds. That's fine; on the bundled sample, only the
nested email has any text content worth scanning.

To force-disable both binaries explicitly:

```bash
python extract.py --no-ocr --no-office --actor "your-name"
```

### 4. Detect PII

```bash
python detect.py --config config/district.example.yaml --actor "your-name"
```

The example district config ships with built-in Presidio recognizers
(SSN, phone, email, dates, credit card, US driver's licence, US bank
number) plus four custom recognizers (8-digit student IDs, district
employee IDs, lunch account numbers, narrative dates).

On the sample fixture, the detector finds about a dozen spans across
email subjects and bodies — student IDs, phone numbers, email
addresses, a date, a lunch account number — plus the email addresses
inside the nested `.eml`. The exact count is shown in the JSON
summary the command prints.

### 5. Build unified person records

```bash
python resolve.py --config config/district.example.yaml --actor "your-name" run
```

This walks every email and groups the From / To / Cc / Bcc addresses (and
any extra addresses that appear in body signatures) into unified
`persons`. On the sample you should see roughly a dozen people created,
flagged as internal where the email domain matches
`district.example.org` and external otherwise.

To list them:

```bash
python resolve.py list
```

### 6. Auto-propose redactions from the PII detections

```bash
python redact.py --config config/district.example.yaml --actor "your-name" propose
```

This converts each PII detection into a `proposed` redaction. **Nothing has
been redacted yet** — proposals are a starting point that a human reviews.

### 7. Start the API server

```bash
python serve.py --port 8000
```

Leave that running in this terminal window. You can confirm it's up by
opening <http://localhost:8000/docs> in a browser — that's the auto-
generated Swagger UI.

### 8. Start the web UI in a second terminal

Open a new terminal, activate the venv if you'd like to keep your tools
on hand (the UI itself doesn't need Python), and run:

```bash
cd D:\Apps\PaperClip\frontend
npm run dev
```

You should see Vite report `Local: http://localhost:5173/`. Open that
address.

### 9. Review redactions in the UI

1. **Type your name** into the **Reviewer** box at the top right. The
   value persists in your browser, and gets sent on every API write so
   the audit log knows who's doing what. Without it, accept / reject
   buttons will refuse to act.
2. Click an email subject in the table — try **"Bus route change"** or
   **"Budget draft for 12345678"**, both have several proposed
   redactions on the body.
3. Each yellow-highlighted span is a `proposed` redaction. Click it to
   open a popover and pick **Accept** or **Reject**. Accepted spans
   turn solid black; rejected ones go grey with a dashed outline.
4. You can change the exemption code (`FERPA`, `PII`, …) in the
   popover before clicking Accept. Add a free-form note if you want
   one in the audit trail.

Color legend:

| Color           | Meaning   |
|-----------------|-----------|
| Yellow          | proposed  |
| Solid black     | accepted  |
| Dashed grey box | rejected  |

### 10. Export the production PDF

Once you've accepted at least one redaction:

- Click the green **Export PDF** button at the top of the email list, or
- From the CLI: `python export.py --config config/district.example.yaml --out data/exports/case-1`

The result is two files in `data/exports/<run-id>/`:

- `production.pdf` — every email and extracted attachment text, with
  every accepted redaction burned in (the redacted text is **not** in
  the PDF — it's gone, not just covered with a black box). Each page
  is Bates-numbered (`ECPS-000001`, `ECPS-000002`, …).
- `redaction_log.csv` — one row per burned redaction, with the Bates
  page it appears on, the exemption code, the reviewer's name, and
  the timestamp.

### 11. Inspect the audit log

```bash
python -c "import sqlite3; c=sqlite3.connect('data/foia.db'); c.row_factory=sqlite3.Row; [print(r['event_at'][:19], r['event_type'], r['actor']) for r in c.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 20')]"
```

…or hit the API:

```
http://localhost:8000/api/v1/audit
```

You'll see one row per CLI you ran, plus one per UI action. The audit
table is append-only — even direct SQL `UPDATE` and `DELETE` against it
are blocked by triggers.

### 12. (Optional) Run AI QA

The AI layer is off by default. To try it locally without paying for
API access, install [Ollama](https://ollama.com/) and pull a model:

```bash
ollama pull llama3.1
```

Then edit `config/district.example.yaml` to flip the AI block on:

```yaml
ai:
  enabled: true
  provider: ollama
  model: llama3.1
```

Run a scan:

```bash
python qa.py --config config/district.example.yaml --actor "your-name" run
```

Any flags it produces appear in the email-detail page under "AI risk
flags". **AI never auto-redacts** — for each flag a human can press
**Promote** to create a `proposed` redaction (which still has to be
accepted in the normal flow), or **Dismiss** to silence it.

For OpenAI / Azure / Anthropic, set the API key as an environment
variable before running `qa.py`:

| Variable             | When to use                       |
|----------------------|-----------------------------------|
| `FOIA_AI_API_KEY`    | The default lookup name           |
| Set the YAML's `ai.api_key_env` to a different name to override. |     |

```bash
# Windows PowerShell
$env:FOIA_AI_API_KEY = "sk-..."

# Windows CMD
set FOIA_AI_API_KEY=sk-...

# macOS / Linux
export FOIA_AI_API_KEY=sk-...
```

---

## Using your own real .mbox

The pipeline is identical. Just point `ingest.py` at your mailbox file:

```bash
python ingest.py --file "/path/to/your-export.mbox" --label "case-2024-001"
```

The `--label` flag is a free-form string saved on every email so you can
filter productions later (e.g. by case number).

### Starting over

If you want a clean slate (after experimenting with the sample, for
instance), close the API server and delete the database and attachment
folders:

```bash
# from backend/
rm -rf data
```

The next `ingest.py` run will recreate them.

---

## Configuration

### District YAML

All per-district settings live in **one** file. The default location is
`backend/config/district.yaml`. The committed
[backend/config/district.example.yaml](backend/config/district.example.yaml)
is a worked template; copy it and edit:

```bash
cd backend
copy config\district.example.yaml config\district.yaml      # Windows
cp config/district.example.yaml config/district.yaml        # macOS / Linux
```

Every CLI takes `--config <path>` to override the location, and the
`FOIA_CONFIG_FILE` env var sets a default for the API server.

The YAML covers:

| Section          | Purpose                                                                             |
|------------------|-------------------------------------------------------------------------------------|
| `district.name`  | Name of the school district. Shown on the bottom of every PDF page.                 |
| `district.email_domains` | What counts as "internal" for entity resolution.                            |
| `pii_detection`  | Which Presidio recognizers to enable, custom regex patterns (student IDs, etc), minimum confidence score, and whether to enable name detection (NER, requires spaCy model). |
| `exemption_codes`| The list of FOIA exemption codes valid in your jurisdiction (`FERPA`, `HIPAA`, etc).|
| `redaction`      | Default exemption code, plus a per-PII-entity-type mapping so auto-propose picks sensible exemptions per entity. |
| `bates`          | Prefix and starting number for the production's Bates labels.                        |
| `ai`             | AI QA backend (off by default). Provider, model, API key env var name.               |

### Environment variables

Most env vars are deploy-time concerns. Override them before launching the
server or CLIs.

| Variable                    | Default                                | Purpose                                 |
|-----------------------------|----------------------------------------|-----------------------------------------|
| `FOIA_DB_PATH`              | `./data/foia.db`                       | SQLite database path                    |
| `FOIA_ATTACHMENT_DIR`       | `./data/attachments`                   | Where attachment bytes are stored       |
| `FOIA_EXPORT_DIR`           | `./data/exports`                       | Where exports are written               |
| `FOIA_CONFIG_FILE`          | `./config/district.yaml`               | Path to the district YAML               |
| `FOIA_LOG_LEVEL`            | `INFO`                                 | Python logging level                    |
| `FOIA_OCR_ENABLED`          | `true`                                 | Master OCR switch                       |
| `FOIA_TESSERACT_CMD`        | *(auto)*                               | Full path to tesseract.exe on Windows   |
| `FOIA_OFFICE_ENABLED`       | `true`                                 | Master Office-conversion switch         |
| `FOIA_LIBREOFFICE_CMD`      | `soffice`                              | Full path to soffice.exe on Windows     |
| `FOIA_CORS_ORIGINS`         | `http://localhost:5173`                | Browser origins allowed to call the API |
| `FOIA_AI_API_KEY`           | *(unset)*                              | API key for the AI provider             |
| `FOIA_ACTOR`                | *(unset)*                              | Default audit-log actor for CLI runs    |

You can put them in a `.env` file in `backend/` and they'll be picked up at
startup. Use `backend/.env.example` as a template.

### Pointing at a Tesseract installed on Windows

After running the UB-Mannheim installer, tesseract typically lands at
`C:\Program Files\Tesseract-OCR\tesseract.exe`. The Phase 2 extractor
won't find it on `PATH`, so set:

```bash
# CMD
set FOIA_TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe

# PowerShell
$env:FOIA_TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

LibreOffice on Windows is similar — `FOIA_LIBREOFFICE_CMD` to the full path
of `soffice.exe`.

---

## Running with Docker

A backend Dockerfile bundles tesseract, LibreOffice, and poppler so OCR /
Office handlers always work inside the container. From the project root:

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

The frontend can be built into a static bundle (`cd frontend && npm run build`)
and served from any HTTP server, or run directly through the Vite dev
server during development.

---

## Troubleshooting

**`pytest` reports `ModuleNotFoundError: foia`** — the virtual environment
is not active for the current terminal. Re-run the activate command from
[Step 3](#step-3--activate-the-virtual-environment).

**`pip install` hangs forever on Windows** — corporate proxies sometimes
block PyPI. Try `pip install --proxy http://your.proxy:port -r backend/requirements.txt`.

**`extract.py` says `tesseract binary not found on PATH`** — either Tesseract
isn't installed (it's optional — see Prerequisites) or it's installed but
not on PATH. Set `FOIA_TESSERACT_CMD` to the full path.

**The web UI loads but every API call is blocked by CORS** — your browser
is hitting the API server on a different port than the configured CORS
origin. Either run the UI through `npm run dev` (which uses port 5173) or
add your frontend's URL to `FOIA_CORS_ORIGINS`.

**I accepted a redaction but it doesn't appear in the export** — make sure
you actually clicked **Accept** (it should turn solid black in the UI),
not just **Promote** on an AI flag (which only creates a `proposed`
redaction). The export burns only `accepted` rows.

**The PDF still contains text I redacted** — verify the redaction is in
`status='accepted'` (`SELECT status FROM redactions`). Phase 8 ignores
`proposed` and `rejected` rows by design.

**The audit log claims someone called `api:anonymous` made a change** — the
UI didn't have a reviewer name set when that call went out. Have everyone
type their name into the header on first load; the value persists in
their browser's localStorage thereafter.

---

## Project layout

```
PaperClip/
├── backend/                  # All Python code, CLIs, and API
│   ├── foia/                 # The application package
│   ├── config/               # District YAML lives here
│   ├── tests/                # 340 tests across all 10 phases
│   ├── ingest.py             # Phase 1 CLI
│   ├── extract.py            # Phase 2 CLI
│   ├── detect.py             # Phase 3 CLI
│   ├── evaluate.py           # Phase 3 evaluation harness
│   ├── resolve.py            # Phase 4 CLI
│   ├── serve.py              # Phase 5 API launcher
│   ├── redact.py             # Phase 6 CLI
│   ├── export.py             # Phase 8 CLI
│   ├── qa.py                 # Phase 10 CLI (AI QA)
│   ├── Dockerfile
│   ├── requirements.txt
│   └── README.md             # Phase-by-phase technical reference
├── frontend/                 # Phase 7 UI (React + TypeScript + Vite)
│   ├── src/
│   ├── package.json
│   └── vite.config.ts
├── prompts/                  # Build-plan prompt that produced this system
└── README.md                 # You are here
```

For the per-phase technical writeup — what each table looks like, what every
endpoint does, design tradeoffs, etc — see
[backend/README.md](backend/README.md).

---

## Status

All ten phases of the build plan are implemented. The project ships with
**340 backend tests** and a TypeScript-strict-mode-clean frontend. The
test suite runs without any optional binaries and in a few seconds.
