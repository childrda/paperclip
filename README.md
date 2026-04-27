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
.mbox file uploaded in the browser
   │
   │  Ingest        Parse emails + extract attachments to disk + SQLite
   ▼
SQLite database (data/foia.db)  +  attachments/ on disk
   │
   │  Extract       OCR images, parse PDFs, convert Office docs to text
   ▼
attachments_text table
   │
   │  Detect        Find SSN / phones / emails / dates / student IDs
   ▼
pii_detections table
   │
   │  Resolve       Group senders/recipients into unified "people"
   ▼
persons table
   │
   │  Propose       Auto-create proposed redactions from PII detections
   ▼
redactions table  (status='proposed')
   │
   │  Web UI: review each one, accept or reject (reviewer name required)
   ▼
redactions table  (status='accepted')
   │
   │  Export        Burn black boxes into a Bates-numbered PDF
   ▼
production.pdf  +  redaction_log.csv
```

Steps 1-5 (ingest → propose) all run in one HTTP request when you drop a
`.mbox` file onto the **Import** page in the UI. Review and export each
have their own page. Everything is auditable: a separate `audit_log`
table records who did what and when, and a SQL trigger blocks any edit
after the fact.

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

Everything below happens in the browser. The CLI tools still exist for
power users and automation (see [The CLI alternative](#the-cli-alternative)
near the bottom), but the day-to-day reviewer flow is:

```
Open the UI → drop a .mbox file → wait a few seconds →
review proposed redactions → click "New export" → done.
```

The repo ships with a synthetic 5-message `.mbox` generator so you can
walk through the whole experience before touching real data.

### 1. Start the backend

In a terminal, with the venv active and your CWD in `backend/`:

```bash
cd backend
python serve.py --port 8000
```

Leave it running. You can sanity-check it by opening
<http://localhost:8000/docs> in a browser — that's the auto-generated
Swagger UI for the whole API.

### 2. Start the UI

Open a **second** terminal — the venv doesn't need to be active here:

```bash
cd D:\Apps\PaperClip\frontend     # or your equivalent path
npm run dev
```

Vite reports `Local: http://localhost:5173/`. Open that address.

### 3. Set your reviewer name

In the dark blue header, find the **Reviewer** box and type your name.
That value is saved in your browser and sent on every API write so the
audit log knows who's doing what. Without it, the Accept / Reject
buttons later in this walkthrough refuse to act.

### 4. Generate a sample mailbox (one-time, for the demo)

This is the only command you need a terminal for in the bundled walk-
through. From `backend/` with the venv active:

```bash
python scripts/generate_sample_mbox.py /tmp/sample.mbox          # macOS / Linux / Git Bash
python scripts/generate_sample_mbox.py %TEMP%\sample.mbox        # Windows CMD
```

Output: `Wrote 5 messages to ...sample.mbox`. The file contains a
plain-text email, an HTML newsletter with tracking pixels (which the
sanitizer strips), one email with a fake PDF attachment, one with a
PNG, and one with a forwarded `.eml` inside. Several emails have
realistic PII — student IDs, phone numbers, email addresses — in their
bodies.

(For real cases, your district mail provider exports `.mbox` directly;
no scripting is needed.)

### 5. Import the mailbox in the UI

Click **Import** in the top nav (it's the default landing page, so
you should already be there). Drag the `sample.mbox` file into the
drop target, or use the file picker. Optionally type a case label
("case-2024-01" etc). Click **Run import**.

A spinner appears for a few seconds. Behind the scenes the server runs
five stages — ingest, extract, detect, resolve, propose — and reports
each stage's stats in cards once it finishes. You'll see something
like:

| Stage    | Headline                          |
|----------|-----------------------------------|
| Ingest   | 5 emails, 3 attachments saved     |
| Extract  | 1 ok, 2 failed (no Tesseract)     |
| Detect   | 12 detections                     |
| Resolve  | 11 persons created                |
| Propose  | 12 redactions proposed            |

A "Recent imports" table at the bottom of the page tracks every
import you've ever run, with the same headline numbers and the actor.

### 6. Review redactions

Click **Emails** in the top nav. Pick an email — try **"Bus route
change"** or **"Budget draft for 12345678"**, both have several proposed
redactions on the body.

Each yellow-highlighted span is a `proposed` redaction. Click any of
them to open a popover with:

- An exemption-code dropdown (`FERPA`, `PII`, …) — change it before
  accepting if needed.
- A free-form note field that ends up in the audit trail.
- **Accept** (turns the span solid black) and **Reject** (grey with a
  dashed outline) buttons.

Color legend:

| Color           | Meaning   |
|-----------------|-----------|
| Yellow          | proposed  |
| Solid black     | accepted  |
| Dashed grey box | rejected  |

Work through the proposals. Email subjects, plain bodies, and sanitized
HTML bodies all support the overlay. Attachment text doesn't render in
the email viewer (yet), but its redactions still get burned into the
final PDF.

### 7. (Optional) Run AI QA on an email

If you've configured an AI provider (see [AI QA](#ai-qa-optional)
below — Ollama is free and local), open any email and click the
**Run AI scan on this email** button at the top right. The model
returns flags into a new "AI risk flags" section under the body, and
you can press **Promote** on any flag to create a new `proposed`
redaction (which still needs Accept). Or **Dismiss** if it's a false
positive.

The hard rule: **AI never auto-redacts**. Every flag → redaction
transition is an explicit human action.

### 8. Search

Click **Search** in the top nav. Type a query. Results come from FTS5
across email subjects, bodies, and extracted attachment text, with
`<mark>…</mark>` highlights on matching tokens. Punctuation in your
query is treated as literal — no FTS5 syntax to memorise.

### 9. Browse people

The **Persons** page lists every unified identity the system built
during entity resolution. Filter by name or by internal-vs-external.
Helpful for sanity-checking that the same person hasn't accidentally
been split across multiple email aliases.

### 10. Export the production PDF

Click **Exports** in the top nav, then **New export**. After a couple
of seconds you'll see a manifest with:

- pages written, redactions burned, Bates first/last
- direct **Download PDF** and **Download CSV** links

The PDF has every accepted redaction burned in — the redacted text is
gone from the PDF entirely, not just covered with a black box. Each
page is Bates-numbered (`ECPS-000001`, `ECPS-000002`, …). The CSV
maps each burned redaction to the Bates page it appears on, the
exemption code, the reviewer's name, and the timestamp.

The page also lists every prior export so you can re-download an old
production without re-running.

### 11. Inspect the audit log

Click **Audit** in the top nav. Every write across the system is here:
imports, redaction creates / updates / deletes, exports, AI runs and
promotions. Filter by event type, origin (`api` vs `cli`), or actor.
Click any row's **Detail** to see the full JSON payload.

The table is append-only. Database triggers reject any `UPDATE` or
`DELETE` against it — even direct SQL.

### Starting over

To reset between experiments, stop the backend (Ctrl+C in its
terminal) and from `backend/`:

```bash
rm -rf data
```

The next import recreates everything.

---

## AI QA (optional)

The AI layer is off by default. To try it locally without paying for
API access, install [Ollama](https://ollama.com/) and pull a model:

```bash
ollama pull llama3.1
```

Then edit `backend/config/district.yaml` (copy from
`district.example.yaml` if you haven't already) and flip the `ai`
block on:

```yaml
ai:
  enabled: true
  provider: ollama
  model: llama3.1
```

Restart the backend. Now the **Run AI scan on this email** button on
any email-detail page works. Any flags appear in a section below the
body with **Promote** and **Dismiss** buttons — *promote* creates a
new `proposed` redaction (still needs Accept), *dismiss* silences the
flag with a note in the audit trail.

For OpenAI / Azure / Anthropic, the same `ai:` block applies; just set
the provider and model, and supply the API key via an environment
variable before launching the backend:

```bash
# Windows PowerShell
$env:FOIA_AI_API_KEY = "sk-..."

# Windows CMD
set FOIA_AI_API_KEY=sk-...

# macOS / Linux
export FOIA_AI_API_KEY=sk-...
```

---

## The CLI alternative

The browser flow described above calls every operation through the
HTTP API; the same operations are also available as CLIs in `backend/`
for power users, automation, and unattended ingest jobs:

| CLI            | Purpose                                                 |
|----------------|---------------------------------------------------------|
| `ingest.py`    | One mailbox into SQLite                                 |
| `extract.py`   | OCR / parse attachments                                 |
| `detect.py`    | Run PII detection                                       |
| `resolve.py`   | Build / merge / rename `persons`                        |
| `redact.py`    | Propose / accept / reject / delete redactions           |
| `export.py`    | Generate the redacted PDF + CSV                         |
| `qa.py`        | AI QA scan / dismiss / promote                          |
| `evaluate.py`  | Precision / recall against the synthetic dataset        |

Every CLI takes `--actor <name>` (recorded in the audit log just like
the UI does via `X-FOIA-Reviewer`) and `--config <path>` (district
YAML). See `python <cli>.py --help` for each one's flags, or
`backend/README.md` for the per-phase technical docs.

---

## Using your own real .mbox

The flow is identical to the bundled walkthrough — drop the file into
the **Import** page, fill in the case label, click Run import. The
server happily ingests `.mbox` files in the hundreds-of-megabytes range;
the request is synchronous, so the browser shows a spinner until every
stage finishes (typically tens of seconds for a few thousand emails).

If you have *several* mailbox exports for the same case, just import
them in sequence — duplicate Message-IDs are skipped automatically, so
re-uploading is harmless.

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

All ten phases of the build plan are implemented, plus a UI-driven
import flow on top so the day-to-day experience is browser-only. The
project ships with **346 backend tests** and a TypeScript-strict-mode-
clean frontend. The test suite runs without any optional binaries and
in under a minute.
