# Paperclip

Paperclip processes FOIA email productions for K–12 school districts.
Drop a `.mbox` file in, accept or reject the suggested redactions,
click Export, and Paperclip produces a court-ready, Bates-numbered PDF
along with a CSV redaction log.

This guide walks you through installing it from scratch. **The only
thing you need on your machine is Docker.** Everything else
(Python, Node.js, the React UI build) runs inside Docker for you.

---

## Step 1 — Install Docker Desktop

Docker is a free application that runs Paperclip's backend, database,
and web interface for you. You install it once and forget it.

### Windows

1. Open <https://www.docker.com/products/docker-desktop/> in your
   browser.
2. Click **Download for Windows** and run the installer.
3. **Tick "Use WSL 2 instead of Hyper-V"** when asked. Click *OK* and
   let the installer finish.
4. Reboot if the installer asks you to.
5. Launch *Docker Desktop* from the Start menu.
6. Wait until the whale icon in the system tray stops animating and
   the Docker Desktop window says **"Engine running"**. This can take
   a minute on first boot.

### macOS

1. Open <https://www.docker.com/products/docker-desktop/>.
2. Click **Download for Mac** — pick *Apple silicon* if your Mac is
   M1 / M2 / M3, otherwise *Intel chip*.
3. Open the downloaded `.dmg` and drag *Docker* into *Applications*.
4. Open Docker from Applications. Approve the privileged-helper
   prompt.
5. Wait for the menu-bar whale icon to say
   **"Docker Desktop is running"**.

### Linux

Use Docker Engine + the Compose plugin. In a terminal:

```
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker
```

(Or log out and back in instead of `newgrp docker`.)

### Confirm Docker is working

Open a terminal:

- **Windows**: press `Win` + `R`, type `cmd`, press Enter.
- **macOS**: open *Terminal* from Applications → Utilities.
- **Linux**: open your usual terminal.

Type:

```
docker --version
```

If you see something like `Docker version 27.x.x`, Docker is ready.

If you see *"command not found"* or *"docker is not recognized"*:
Docker Desktop isn't running. Open it and wait for **"Engine running"**.

---

## Step 2 — Download Paperclip

Pick **one** of the two options.

### Option A — Download a ZIP (easiest)

1. Open <https://github.com/childrda/paperclip>.
2. Click the green **Code** button → **Download ZIP**.
3. Unzip the file. You'll get a folder called `paperclip-main`.
4. Move it somewhere you can find — e.g. `C:\paperclip` on Windows or
   `~/paperclip` on macOS / Linux.

### Option B — Use Git

If you already have Git installed:

```
git clone https://github.com/childrda/paperclip.git
cd paperclip
```

---

## Step 3 — Start Paperclip

Open the Paperclip folder in your file explorer.

| OS      | Double-click          |
|---------|-----------------------|
| Windows | **`paperclip.bat`**   |
| macOS   | **`paperclip.command`** |
| Linux   | **`paperclip.desktop`** (or run `./paperclip.sh` in a terminal) |

> **macOS heads-up:** the first time you double-click, macOS may
> refuse with *"unidentified developer"*. Right-click the file →
> *Open* → click *Open* on the warning dialog. You only do this once.

A black terminal window opens and shows progress like:

```
Starting Paperclip via Docker Compose...
[+] Building 0.5s (24/24) FINISHED
[+] Running 2/2
 ✔ Container paperclip-backend-1   Started
 ✔ Container paperclip-frontend-1  Started

Paperclip is running at http://localhost:8080/
```

**The first run takes 3–5 minutes** — Docker downloads its base
images and builds the Paperclip backend and frontend. After that,
launches take ~10 seconds.

Your browser opens to <http://localhost:8080/>. If it doesn't, open
that address yourself.

---

## Step 4 — Sign in

Out of the box Paperclip starts in **development mode**: a small set
of test users (`alice`, `bob`) is accepted with **any** password.
This lets you confirm everything works before you wire up your
district's directory. Step 7 below switches it to real LDAPS auth.

In the browser:

- **Username**: `alice`
- **Password**: anything (`x` is fine)

You should land on an empty **Cases** screen.

---

## Step 5 — Run your first case

You can use any `.mbox` file you have. To try Paperclip without one,
the project ships with a synthetic sample generator.

### Generate the sample

In a *new* terminal in the Paperclip folder:

```
docker compose exec backend python scripts/generate_sample_mbox.py /data/inbox/sample.mbox
```

You should see `Wrote 5 messages to /data/inbox/sample.mbox`.

The file lives in `data/inbox/sample.mbox` inside the Paperclip
folder.

### Upload it

In the browser:

1. Click **+ New case** (top right of the Cases screen).
2. Type a name, e.g. `Demo case`.
3. Type a Bates prefix, e.g. `DEMO`. (Or leave blank to use the
   district default.)
4. Drag the `.mbox` file onto the file box.
5. Click **Start case**.

Five stage cards animate in turn:

- **Reading mailbox** → "5 email(s), 3 attachment(s)"
- **Extracting attachments** → "1 ok, 2 failed (no Tesseract)"
- **Scanning for PII** → "12 PII span(s)"
- **Building person index** → "11 person(s)"
- **Auto-proposing redactions** → "12 redaction(s) proposed"

When all five are green, Paperclip drops you on the case page.

### Review redactions

Click **Review emails →**. Click an email subject. Yellow boxes
mark proposed redactions. Click any of them and pick **Accept** or
**Reject** in the popover. Accepted redactions turn solid black.

### Export the PDF

Go back to the case page (use the *Cases* link at the top). Click
**Export production PDF**. The PDF opens in a new tab — every
accepted redaction is a black box, the original text is gone (not
just hidden), and every page is Bates-numbered. A CSV log of every
redaction sits alongside it under `data/exports/<run>/`.

You're done. The audit log records every action against your
username — visit the **Audit** tab to see it.

---

## Step 6 — Stopping and restarting

To **stop** Paperclip when you're done:

```
docker compose down
```

Run that in a terminal sitting in the Paperclip folder. The browser
window goes blank when you next try to use it; that's expected.

Your data is safe — it lives in the `data/` folder. Stopping the
containers doesn't touch it.

To **start it again**, double-click the same launcher you used in
Step 3. It comes back up in 5–10 seconds.

To **completely reset** (delete every case and start over): stop
Paperclip, then delete the `data/` folder. Re-launch.

---

## Step 7 — Connect to your district directory (LDAPS)

This is the production-ready step. After it, only real district
employees who are members of the configured FOIA security group can
sign in.

You'll need from your IT / identity team:

- The LDAPS URL (e.g. `ldaps://dc.lcps.org:636`).
- A **read-only service account** distinguished name and password —
  Paperclip uses it to look up users, not to authenticate them.
- The base DN under which staff users live
  (e.g. `OU=Staff,DC=lcps,DC=org`).
- The DN of the FOIA security group
  (e.g. `CN=FOIA-Officers,OU=Groups,DC=lcps,DC=org`).
- The CA certificate that signs your domain controller's TLS cert
  (a `.crt` or `.pem` file).

### Edit `docker-compose.yml`

Open `docker-compose.yml` in any text editor. Inside the
`environment:` block under `backend:`, replace the placeholder values
with the ones from IT. Set `PAPERCLIP_AUTH_DEV_MODE: "false"` to turn
off the dev users.

Example:

```yaml
      PAPERCLIP_LDAP_URI: "ldaps://dc.lcps.org:636"
      PAPERCLIP_LDAP_BIND_DN: "CN=svc-paperclip,OU=ServiceAccounts,DC=lcps,DC=org"
      PAPERCLIP_LDAP_BIND_PASSWORD: "REDACTED"
      PAPERCLIP_LDAP_USER_BASE_DN: "OU=Staff,DC=lcps,DC=org"
      PAPERCLIP_LDAP_USER_FILTER: "(sAMAccountName={username})"
      PAPERCLIP_LDAP_GROUP_DN: "CN=FOIA-Officers,OU=Groups,DC=lcps,DC=org"
      PAPERCLIP_LDAP_CA_CERT_PATH: "/config/ca.crt"
      PAPERCLIP_AUTH_DEV_MODE: "false"
```

Drop the CA certificate at `backend/config/ca.crt` (the
`PAPERCLIP_LDAP_CA_CERT_PATH` value above maps to that location
inside the container).

### Restart

In a terminal in the Paperclip folder:

```
docker compose down
docker compose up -d --build
```

Sign in again with a real district username and password. Members
of the FOIA security group get in. Anyone else — including a removed
member who was previously logged in — gets a generic
*"invalid credentials"* error.

### What Paperclip enforces

- **LDAPS only.** Plain `ldap://` is rejected. STARTTLS is not used
  as a fallback. The TLS certificate is validated against the CA you
  provided.
- **Group membership re-checked on cadence.** When IT removes someone
  from the FOIA group, Paperclip blocks their next request — they
  don't have to log out first. Default cadence: every 15 minutes.
- **Lockout** after 5 failed login attempts in 15 minutes (per
  username, configurable). Failed attempts log the supplied username
  even if no such user exists, for security review.
- **The service-account password is read from `docker-compose.yml`**
  (or an `.env` file beside it) and never appears in logs.

---

## Updating Paperclip to a new version

```
git pull              # if you cloned with Git
docker compose down
docker compose up -d --build
```

`--build` rebuilds the backend AND frontend images from source, so
new code is always picked up. Database migrations run automatically
on the next start. Your `data/` folder is preserved.

If you downloaded a ZIP, replace the contents of the Paperclip folder
with the new ZIP's contents — but **leave `data/` and your edited
`docker-compose.yml` alone**. Then re-launch.

---

## Troubleshooting

**The launcher says "Docker is not installed."**
Docker Desktop isn't running yet. Open it from the Start menu /
Applications and wait for the whale icon to say **"Engine running"**,
then double-click the launcher again.

**The browser shows "This site can't be reached."**
First-run starts can take 3–5 minutes. If it's still not loading
after that, open a terminal in the Paperclip folder and run
`docker compose ps`. Both `paperclip-backend-1` and
`paperclip-frontend-1` should say `Up`. If one says `Restarting`,
run `docker compose logs backend` (or `logs frontend`) to see why.

**I'm seeing an old version of the UI without a sign-in screen.**
Something is stale. From the Paperclip folder run
`docker compose down && docker compose up -d --build`. The
`--build` flag forces a fresh frontend bundle.

**"Sign in failed"** with the right password.
In dev mode, the username has to be one of the names in
`PAPERCLIP_AUTH_DEV_USERS` (defaults: `alice`, `bob`). With LDAPS
configured, the user must exist in the directory AND be a member of
the FOIA security group. The error message is intentionally generic
(it doesn't say which condition failed) so attackers can't probe.
Ask IT.

**The "Reading mailbox" stage fails.**
Check that the file you uploaded really is a `.mbox`. If it's
`.eml` or `.msg`, it won't parse. Most mail providers can export
to `.mbox` directly.

**The case page says "extract failed" for some attachments.**
Tesseract (for OCR) or LibreOffice (for `.docx`) couldn't process
those files. The Paperclip Docker image already includes both — if
you're seeing this consistently, run
`docker compose down && docker compose up -d --build` to make sure
your backend image is current.

**I want to start over with no data.**
`docker compose down`, then delete the `data/` folder, then
double-click the launcher.

**I'm behind a corporate firewall and Docker can't pull images.**
In Docker Desktop: *Settings* → *Resources* → *Proxies*. Or ask IT
to mirror the images internally.

**I want to use a different port than 8080.**
Set `PAPERCLIP_PORT` in `docker-compose.yml` (or in a `.env` file in
the same folder), then `docker compose up -d --build`.

---

## Reference

### What Paperclip does, end-to-end

```
.mbox file uploaded in the browser
   ▼ Ingest    Read emails, save attachments, sanitize HTML
   ▼ Extract   OCR images, parse PDFs, convert Office docs to text
   ▼ Detect    Find SSNs, phones, emails, dates, student IDs
   ▼ Resolve   Group senders/recipients into unified people
   ▼ Propose   Auto-suggest redactions from each PII detection
   │
   ▼ Reviewer accepts or rejects in the browser (LDAPS-authenticated)
   │
   ▼ Export    Burn black boxes into a Bates-numbered PDF
   │
   ▼
production.pdf  +  redaction_log.csv
```

The first five stages run as a background pipeline in a single HTTP
upload; the browser watches live progress via Server-Sent Events.
Review and export are explicit reviewer actions.

### Hard guarantees the system enforces

- **Local-first.** No case data leaves your deployment unless cloud
  AI is explicitly enabled per case.
- **Non-destructive redactions.** Source documents are never modified.
  Redactions are span overlays on top.
- **Human authority.** AI never produces a final redaction. Every
  exported black box was explicitly accepted by a logged-in reviewer.
- **Auditable.** Every write goes through an append-only `audit_log`
  table whose `UPDATE` and `DELETE` are blocked by database triggers.
  Each row carries a real `user_id` foreign key when the actor was a
  signed-in reviewer.
- **Distributable.** New district = new YAML + new env values. No
  code changes per district.

### Environment variables (most useful)

Set in `docker-compose.yml` under `backend.environment`, or in a
`.env` file beside `docker-compose.yml`:

| Variable                                | Purpose                                                |
|-----------------------------------------|--------------------------------------------------------|
| `PAPERCLIP_PORT`                        | Web UI port. Default `8080`.                           |
| `PAPERCLIP_AUTH_DEV_MODE`               | `true` skips LDAPS for first-run testing.              |
| `PAPERCLIP_AUTH_DEV_USERS`              | Comma-separated dev-mode allowlist.                    |
| `PAPERCLIP_LDAP_URI`                    | `ldaps://...` — required for production auth.          |
| `PAPERCLIP_LDAP_BIND_DN` / `_PASSWORD`  | Read-only service account for the directory lookup.    |
| `PAPERCLIP_LDAP_USER_BASE_DN`           | Where staff users live in the directory.               |
| `PAPERCLIP_LDAP_GROUP_DN`               | The FOIA security group that gates access.             |
| `PAPERCLIP_LDAP_CA_CERT_PATH`           | Mount path of the CA certificate inside the container. |
| `PAPERCLIP_AUTH_LOCKOUT_THRESHOLD`      | Failed-attempt lockout count. Default `5`.             |
| `PAPERCLIP_AUTH_LOCKOUT_WINDOW_MINUTES` | Window for the count. Default `15`.                    |
| `PAPERCLIP_AUTH_GROUP_RECHECK_MINUTES`  | How often to re-verify group membership. Default `15`. |
| `FOIA_CONFIG_FILE`                      | District YAML path. Default `/config/district.yaml`.   |

The full reference, including per-phase technical details, lives in
[`backend/README.md`](backend/README.md).

### Repository layout

```
paperclip/
├── README.md                 (this file — install + usage)
├── docker-compose.yml        Production deployment
├── paperclip.bat             Windows launcher  (double-click)
├── paperclip.command         macOS launcher    (double-click)
├── paperclip.desktop / .sh   Linux launchers
├── deploy/
│   ├── nginx.conf            Reverse proxy: SPA + /api proxy + SSE
│   └── DESKTOP_BUNDLE.md     Future single-binary distribution plan
├── backend/                  Python (FastAPI) — built into a Docker image
│   ├── README.md             Phase-by-phase technical reference
│   ├── foia/                 Source code
│   ├── tests/                386 tests
│   └── Dockerfile
└── frontend/                 React + TypeScript — built into a Docker image
    ├── src/
    └── Dockerfile            Multi-stage: Node builder → nginx server
```

For the engineering reference — schemas, endpoints, design tradeoffs,
and the post-Phase-10 auth/cases/SSE/temporal-classifier layer — see
[`backend/README.md`](backend/README.md).

---

## For developers

The everyday path is the launcher above. To work *on* Paperclip:

```bash
# 1. From the repo root, create + activate a venv.
python -m venv .venv
# Windows (CMD/PowerShell):  .venv\Scripts\activate
# macOS / Linux:             source .venv/bin/activate

# 2. Backend
cd backend
python -m pip install -r requirements.txt

# Set dev-mode auth before launching:
#   bash / zsh:    export PAPERCLIP_AUTH_DEV_MODE=true && export PAPERCLIP_AUTH_DEV_USERS=alice
#   PowerShell:    $env:PAPERCLIP_AUTH_DEV_MODE='true'; $env:PAPERCLIP_AUTH_DEV_USERS='alice'
#   CMD:           set PAPERCLIP_AUTH_DEV_MODE=true && set PAPERCLIP_AUTH_DEV_USERS=alice
# Then:
python serve.py --port 8000

# 3. Frontend (in another terminal — venv not required)
cd frontend
npm install
npm run dev          # http://localhost:5173

# 4. Sign in as `alice` with any password.

# 5. Tests (from backend/, venv active)
python -m pytest
```

The test suite is 386 tests. It never touches a real DC — LDAPS is
exercised through an injected adapter. Optional binary deps
(Tesseract, LibreOffice) are mocked too.
