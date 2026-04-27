# Paperclip

A FOIA review tool for K–12 school districts. Built around the
day-to-day records officer, not the developer. Drop a mailbox export
in, accept or reject the suggested redactions, click Export, send
the resulting Bates-numbered PDF.

The original engine — ten phases of pipeline modules with 380+ tests —
sits underneath a real case-management UI with directory authentication
and live-streamed progress.

## What a reviewer sees

```
1.  Double-click the Paperclip icon on the desktop.
2.  Sign in with district credentials (LDAPS-bound, FOIA group enforced).
3.  Click "+ New case", pick a name + Bates prefix, drop the .mbox.
4.  Watch each pipeline stage tick across the screen with live counts.
5.  Click into the case, accept or reject the proposed redactions.
6.  Click "Export production PDF". Download the redacted PDF + CSV log.
7.  Done. The audit log records every action against your real user.
```

The CLIs (`ingest.py`, `extract.py`, `detect.py`, `resolve.py`,
`redact.py`, `export.py`, `qa.py`) still exist for scripting and the
test suite. They are no longer how an officer uses the system.

## Architecture, in one diagram

```
            ┌─────────────────────────────────┐
            │   Browser (React + TypeScript)  │
            │                                 │
            │  /login → /cases → /cases/:id   │
            │  /cases/new (file drop + SSE)   │
            └────────────────┬────────────────┘
                             │ HttpOnly session cookie
                             ▼
            ┌─────────────────────────────────┐
            │       FastAPI backend           │
            │                                 │
            │  /api/v1/auth     LDAPS bind    │
            │  /api/v1/cases    case mgmt     │
            │  /api/v1/imports  upload + SSE  │
            │  /api/v1/...      Phase 1-10    │
            └────────────────┬────────────────┘
                             │
                             ▼
            ┌─────────────────────────────────┐
            │     SQLite (data/foia.db)       │
            │                                 │
            │  cases  users  user_sessions    │
            │  emails  attachments  raw_content│
            │  pii_detections  redactions     │
            │  pipeline_jobs  pipeline_events │
            │  audit_log (append-only,        │
            │   tamper-blocked by triggers)   │
            └─────────────────────────────────┘
```

A pipeline job runs the existing phase modules in a background thread
and emits stage events to the database. The browser subscribes via
Server-Sent Events and updates the progress card in real time.

## Hard rules carried forward

The original specification's invariants are still enforced:

- **Local-first.** No case data leaves the deployment unless cloud AI
  is explicitly enabled per case (`ai.enabled: true` + a non-`null`
  provider in `district.yaml`).
- **Non-destructive redactions.** Source documents are immutable.
  Redactions live as `(source_type, source_id, [start, end))` spans
  on top.
- **Human authority.** AI never produces a final redaction. Every
  exported span has been explicitly accepted by a logged-in user.
- **Distributable.** New district = new YAML, not a new branch.
- **Auditable.** Every write goes through `audit.log_event`. The
  `audit_log` table has database triggers that reject any `UPDATE` or
  `DELETE`. With LDAPS auth, every API write also carries a real
  `user_id` foreign key.

## Authentication

LDAPS bind with security-group membership check.

```
1. The user types username + password into /login.
2. Backend opens an LDAPS connection (TLS cert validated; ldap:// is
   refused, STARTTLS is not used as a fallback).
3. Service account binds, searches for the user's DN.
4. Backend rebinds as the user with the supplied password.
5. The user's group memberships are checked; they must be in the
   configured FOIA security group.
6. A session token is issued (32 random bytes; stored hashed in
   user_sessions; sent as an HttpOnly Secure cookie).
7. Group membership is re-checked every N minutes on subsequent
   requests, so removing someone from the directory revokes their
   access on their next request — not their next login.
```

Failed attempts are written to `auth_failed_logins`. After
`PAPERCLIP_AUTH_LOCKOUT_THRESHOLD` failures within
`PAPERCLIP_AUTH_LOCKOUT_WINDOW_MINUTES` (defaults: 5 / 15), further
logins for that username are rejected for the rest of the window with
the same generic 401 a wrong password produces — we don't reveal
which condition failed.

LDAPS configuration lives in environment variables (or `.env`),
**never** the district YAML, because credentials and infrastructure
endpoints are deployment secrets:

```
PAPERCLIP_LDAP_URI=ldaps://dc.example.org:636
PAPERCLIP_LDAP_BIND_DN=CN=svc-paperclip,OU=ServiceAccounts,DC=example,DC=org
PAPERCLIP_LDAP_BIND_PASSWORD=...
PAPERCLIP_LDAP_USER_BASE_DN=OU=Staff,DC=example,DC=org
PAPERCLIP_LDAP_USER_FILTER=(sAMAccountName={username})
PAPERCLIP_LDAP_GROUP_DN=CN=FOIA-Officers,OU=Groups,DC=example,DC=org
PAPERCLIP_LDAP_CA_CERT_PATH=/etc/paperclip/ca.crt
PAPERCLIP_LDAP_TIMEOUT_SECONDS=10
PAPERCLIP_AUTH_LOCKOUT_THRESHOLD=5
PAPERCLIP_AUTH_LOCKOUT_WINDOW_MINUTES=15
PAPERCLIP_AUTH_SESSION_LIFETIME_HOURS=8
PAPERCLIP_AUTH_GROUP_RECHECK_MINUTES=15
```

For local dev / laptop runs, set:

```
PAPERCLIP_AUTH_DEV_MODE=true
PAPERCLIP_AUTH_DEV_USERS=alice,bob
```

This bypasses LDAPS and accepts any password for the listed
usernames. **Do not enable in production.** The startup log emits a
`WARNING` whenever dev mode is active.

## Temporal entity classifier

When entity resolution sees a person in an email, it records a
`person_affiliations` row tagged with `observed_at = email.date_sent`.
The recorded evidence is the raw email_domain — not an
internal/external interpretation, which depends on rules that change.

`is_internal_at(person_id, when, internal_domains=...)` answers "what
did the corpus know about this person on date X?" by reading the most
recent `email_domain` observation at or before `when` and applying
the supplied rules. Same evidence + new rules = different answer,
without rewriting history. Different evidence over time produces a
genuine timeline.

This is the legal-defensibility argument: a redaction decision dated
March 2022 must be defensible against what we knew about the person
in March 2022, not what we know today. Test coverage in
`tests/test_temporal_classifier.py` pins this down — including the
case where the same human used a district address in 2022 and a
personal address in 2024, and the merged person record's
classification correctly differs by query date.

## Running it

### As a reviewer (no terminal needed)

Your IT staff installs Docker once, drops the `paperclip` folder on a
shared drive, and either pins the launcher to your taskbar or sets it
to run on login. From then on:

| OS      | What you double-click   |
|---------|-------------------------|
| Windows | `paperclip.bat`         |
| macOS   | `paperclip.command`     |
| Linux   | `paperclip.desktop`     |

The launcher starts the backend + frontend containers and opens your
default browser at <http://localhost:8080/>. Sign in with your
directory credentials.

### Single-binary desktop bundle (planned)

A PyInstaller + pywebview bundle is on the roadmap so officers don't
need Docker installed at all. The design choice is documented in
[`deploy/DESKTOP_BUNDLE.md`](deploy/DESKTOP_BUNDLE.md). Out of scope
for this release.

### As a developer

```bash
# 1. Backend
cd backend
python -m venv ../.venv
../.venv/Scripts/python -m pip install -r requirements.txt
PAPERCLIP_AUTH_DEV_MODE=true \
PAPERCLIP_AUTH_DEV_USERS=alice \
FOIA_CONFIG_FILE=config/district.example.yaml \
  ../.venv/Scripts/python serve.py --port 8000

# 2. Frontend (in another terminal)
cd frontend
npm install
npm run dev          # http://localhost:5173

# 3. Sign in as `alice` with any password.
```

### Tests

```bash
cd backend
python -m pytest                    # 380+ tests across all phases
```

The suite never touches a real DC; LDAPS is exercised through an
injected adapter. Optional binary deps (Tesseract, LibreOffice) are
mocked in the relevant tests too, so a fresh venv is enough.

## What's new in this release

- Real authentication. LDAPS bind, FOIA-group enforcement, session
  cookies, lockout, dev-mode for laptops. The legacy
  `X-FOIA-Reviewer` header is still accepted for the test suite and
  CLI parity, but the production path is the cookie.
- Cases as first-class entities. `cases` table with status pipeline,
  per-case Bates prefix, scoped emails. UI revolves around them.
- Background pipeline jobs. `POST /api/v1/imports` returns the moment
  the upload lands; the pipeline runs in a thread and writes events
  to `pipeline_events` row by row.
- Server-Sent-Events progress stream. The new-case page shows a
  live stage list with counts ("1 email, 3 attachments";
  "12 detections"; …) instead of a frozen spinner.
- Per-stage retry. If a stage fails, the case is marked failed with
  the stage name. Re-running just that stage onwards is one click.
- Temporal entity classifier. `is_internal_at(person, when, rules)`
  answers point-in-time questions correctly. Static `is_internal`
  is still cached on the persons row for fast UI rendering.
- Audit log gains `user_id` FK. Existing rows stay readable; new rows
  carry both the legacy `actor` string and the FK.
- Real launcher scripts (`.bat` / `.command` / `.desktop`/`.sh`) plus
  `docker-compose.yml` + nginx config for multi-user deployments.

## What's NOT changed

The Phase 1–10 modules under `backend/foia/` (ingestion, extraction,
detection, redaction, export, AI, audit) are reused as-is. The
non-destructive redaction model, append-only audit log triggers, AI
"never auto-redacts" rule, per-district YAML, and FTS5 search are
all carried forward without restructuring. The 380+ tests for the
phase modules still pass.

## Repository layout

```
paperclip/
├── README.md                 (this file)
├── docker-compose.yml        Multi-user deploy
├── deploy/
│   ├── nginx.conf            Reverse proxy: SPA + /api proxy + SSE
│   └── DESKTOP_BUNDLE.md     Tauri-vs-PyInstaller decision record
├── paperclip.bat             Windows launcher
├── paperclip.command         macOS launcher
├── paperclip.desktop         Linux launcher (calls paperclip.sh)
├── paperclip.sh              Linux launcher script
├── backend/
│   ├── README.md             Phase-by-phase technical reference
│   ├── foia/
│   │   ├── auth_service.py        LDAPS bind + group check + lockout
│   │   ├── cases.py               Case + pipeline-job state model
│   │   ├── api/routes/auth.py     /api/v1/auth/{login,me,logout}
│   │   ├── api/routes/cases.py    /api/v1/cases
│   │   ├── api/routes/imports.py  /api/v1/imports + SSE stream + retry
│   │   ├── ingestion.py           Phase 1
│   │   ├── extraction.py          Phase 2
│   │   ├── detection.py           Phase 3
│   │   ├── er_driver.py           Phase 4 + temporal classifier
│   │   ├── redaction.py           Phase 6
│   │   ├── export.py              Phase 8
│   │   ├── audit.py               Phase 9 (now with user_id FK)
│   │   ├── ai.py                  Phase 10
│   │   ├── schema.sql
│   │   └── ...
│   ├── tests/                340+ tests, no live DC required
│   └── ...
└── frontend/
    └── src/
        ├── App.tsx           AuthProvider + Routes
        ├── auth.tsx          AuthContext + RequireAuth
        ├── pages/
        │   ├── LoginPage.tsx
        │   ├── CasesPage.tsx
        │   ├── NewCasePage.tsx     (file drop + SSE progress)
        │   ├── CaseDetailPage.tsx
        │   ├── EmailListPage.tsx
        │   ├── EmailDetailPage.tsx
        │   ├── SearchPage.tsx
        │   ├── PersonsPage.tsx
        │   ├── ExportsPage.tsx
        │   └── AuditPage.tsx
        └── ...
```

For a per-phase walkthrough of the engine — what each table looks
like, which Presidio recognizers are wired up, how the export burns
text out of the PDF — see [`backend/README.md`](backend/README.md).
