# Paperclip

Paperclip processes FOIA email productions for K–12 school districts.
Drop a `.mbox` file in, accept or reject the suggested redactions,
click Export, and Paperclip produces a court-ready, Bates-numbered PDF
along with a CSV redaction log.

This guide walks you through installing it from scratch. If you've
never used Docker or run a script from a terminal, that's fine — every
step says exactly what to type and what to expect.

---

## Step 1 — Install Docker Desktop

Paperclip runs inside Docker. Docker Desktop is a free, free-to-install
application that runs the Paperclip backend and database for you.

### Windows

1. Open <https://www.docker.com/products/docker-desktop/> in your
   browser.
2. Click **Download for Windows** and run the installer.
3. **Tick "Use WSL 2 instead of Hyper-V"** if asked. Click *OK*. Let
   the installer finish.
4. Reboot if prompted.
5. Start *Docker Desktop* from the Start menu. Wait until the whale
   icon in the system tray stops animating and the app says
   **"Engine running"**.

### macOS

1. Open <https://www.docker.com/products/docker-desktop/>.
2. Click **Download for Mac** (pick *Apple silicon* if your Mac is
   M1/M2/M3, otherwise *Intel chip*).
3. Open the downloaded `.dmg` and drag *Docker* into *Applications*.
4. Open Docker from Applications. Approve the privileged-helper
   prompt. Wait until the menu-bar whale shows **"Docker Desktop is
   running"**.

### Linux

Use Docker Engine + Docker Compose plugin, not Desktop:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker     # or log out + back in
```

### Verify it's working

Open a terminal:

- **Windows**: press `Win` + `R`, type `cmd`, press Enter.
- **macOS**: open *Terminal* from Applications → Utilities.
- **Linux**: open your usual terminal.

Type:

```
docker --version
```

You should see something like `Docker version 27.x.x, build …`. If you
see *"command not found"* or *"docker is not recognized"*, Docker
Desktop isn't running yet — start it from the menu and try again.

---

## Step 2 — Download Paperclip

You have two options. Pick **one**.

### Option A — Download a ZIP (easiest)

1. Open <https://github.com/childrda/paperclip> in your browser.
2. Click the green **Code** button → **Download ZIP**.
3. Unzip the file. You'll get a folder called `paperclip-main` (or
   similar). Move it somewhere convenient — for example
   `C:\paperclip` on Windows or `~/paperclip` on macOS/Linux.

### Option B — Use Git

If you already have Git installed:

```bash
git clone https://github.com/childrda/paperclip.git
cd paperclip
```

---

## Step 3 — Install Node.js (one-time, only if you don't have it)

Paperclip's web interface is built once from source the first time you
launch it. Building it needs Node.js. If your IT department gave you a
copy of Paperclip with the `frontend/dist` folder already present, skip
this step.

| OS      | How to install                                                              |
|---------|-----------------------------------------------------------------------------|
| Windows | Download the LTS installer from <https://nodejs.org/> and run it. Defaults are fine. |
| macOS   | <https://nodejs.org/> or `brew install node`                                |
| Linux   | `sudo apt install nodejs npm` or use [nvm](https://github.com/nvm-sh/nvm)    |

Verify in a terminal:

```
node --version
```

Anything `v18` or higher is fine.

---

## Step 4 — Start Paperclip for the first time

Open the Paperclip folder in your file explorer.

### Windows

Double-click **`paperclip.bat`**.

### macOS

Double-click **`paperclip.command`**.

(If macOS refuses with *"unidentified developer"*: right-click →
*Open* → *Open* on the dialog.)

### Linux

In a terminal, run:

```
./paperclip.sh
```

Or double-click `paperclip.desktop` if your file manager allows it.

### What happens next

A black window opens and shows messages like:

```
Building Paperclip frontend (one-time)...
Starting Paperclip via Docker Compose...
[+] Running 2/2
 ✔ Container paperclip-backend-1   Started
 ✔ Container paperclip-frontend-1  Started

Paperclip is running at http://localhost:8080/
```

The first run takes 2–4 minutes (downloading the Docker images and
building the front end). Subsequent runs take 5–10 seconds.

Your default browser opens to <http://localhost:8080/>. If it doesn't,
open that address yourself.

---

## Step 5 — Sign in (first time)

Out of the box, Paperclip starts in **development mode**: any
username on a small allowlist is accepted with any password. This lets
you confirm the system works before you wire up your district's
directory. Once you've used it, **Step 8** below switches it to real
LDAPS auth.

The default development users are listed in `docker-compose.yml` under
`PAPERCLIP_AUTH_DEV_USERS`. To enable the bundled defaults, edit
`docker-compose.yml` and set:

```yaml
      PAPERCLIP_AUTH_DEV_MODE: "true"
      PAPERCLIP_AUTH_DEV_USERS: "alice,bob"
```

Save the file, then in the terminal that's running Paperclip press
`Ctrl` + `C` and run the launcher again.

In the browser, sign in:

- **Username**: `alice`
- **Password**: anything (`x` is fine)

You should land on an empty **Cases** screen.

---

## Step 6 — Run your first case

You can use any `.mbox` file you have. To try Paperclip without one,
the project ships with a synthetic sample generator.

### Generating the sample (one-time)

Open a *new* terminal in the Paperclip folder and run:

```bash
docker compose exec backend python scripts/generate_sample_mbox.py /data/inbox/sample.mbox
```

You'll see `Wrote 5 messages to /data/inbox/sample.mbox`.

Then in your file explorer, navigate to the Paperclip folder →
`data` → `inbox`. The file `sample.mbox` is sitting there. Drag a
copy to your desktop so you can drop it into the upload form.

(For real cases, your district's mail provider exports an `.mbox`
directly — no terminal command needed.)

### Run the case

In the browser:

1. Click **+ New case** in the top right.
2. Type a name, e.g. `Demo case`.
3. Type a Bates prefix (or leave blank to use the district default —
   `ECPS` in the bundled config).
4. Drag the `.mbox` into the file box.
5. Click **Start case**.

Five stage cards animate in turn:

- **Reading mailbox** → "1 email(s), 3 attachment(s)"
- **Extracting attachments** → "1 ok, 2 failed (no Tesseract)"
- **Scanning for PII** → "12 PII span(s)"
- **Building person index** → "11 person(s)"
- **Auto-proposing redactions** → "12 redaction(s) proposed"

When all five are green, Paperclip drops you on the case page.

### Review redactions

Click **Review emails →**. Click an email subject. Yellow boxes mark
proposed redactions. Click any of them and pick **Accept** or
**Reject** in the popover. Accepted redactions turn solid black.

### Export the production PDF

Go back to the case page. Click **Export production PDF**. The PDF
opens in a new tab — every accepted redaction is a black box, the
original text is gone (not just hidden), and every page is
Bates-numbered. A CSV redaction log sits alongside it.

You're done. The audit log records every action with your username.

---

## Step 7 — Stopping and restarting

To **stop** Paperclip when you're done for the day:

```
docker compose down
```

(Run that from a terminal sitting in the Paperclip folder. Or close
the launcher window — the containers keep running, but you can also
shut them down by re-launching and noting the message.)

Your data is safe — it lives in the `data/` folder next to
`docker-compose.yml`. Stopping the containers doesn't delete it.

To **start it again** later, double-click the same launcher you used
in Step 4.

To **completely reset** (delete every case and start over), close
Paperclip and delete the `data/` folder.

---

## Step 8 — Connect to your district directory (LDAPS)

This is the production-ready step. After it, Paperclip will only let
real district employees in, and only those in the configured FOIA
security group.

You'll need from your IT/identity team:

- The LDAPS URL (e.g. `ldaps://dc.lcps.org:636`).
- A read-only service account distinguished name and password
  (Paperclip uses it to look up users; users authenticate with their
  own passwords).
- The base DN under which staff users live
  (e.g. `OU=Staff,DC=lcps,DC=org`).
- The DN of the security group that gates Paperclip access
  (e.g. `CN=FOIA-Officers,OU=Groups,DC=lcps,DC=org`).
- The CA certificate that signs your domain controller's TLS cert
  (a `.crt` or `.pem` file).

### Edit `docker-compose.yml`

Open `docker-compose.yml` in any text editor. Find the `environment:`
block under `backend:` and set the values supplied by IT. Example:

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

Then drop the CA certificate into `backend/config/ca.crt`.

### Restart

In a terminal in the Paperclip folder:

```
docker compose down
docker compose up -d
```

Sign in again — you'll need to use a real district username and
password now. Members of the FOIA security group get in. Anyone
else, including a removed member who was previously logged in,
gets a generic "invalid credentials" message.

### Hard rules Paperclip enforces

- **LDAPS only.** Plain `ldap://` is rejected. STARTTLS is not used
  as a fallback. The TLS certificate is validated against the CA you
  provided.
- **Group membership re-checked on cadence.** If IT removes someone
  from the FOIA group, Paperclip blocks their next request — they
  don't have to log out first. Default cadence: every 15 minutes
  (`PAPERCLIP_AUTH_GROUP_RECHECK_MINUTES`).
- **Lockout after repeated failures.** Default: 5 failed attempts
  in 15 minutes locks the account out for the rest of the window.
  Failed attempts log the attempted username — even if it doesn't
  exist — for security review.
- **Service account password is read from `docker-compose.yml`** (or
  an `.env` file beside it) and never logged.

---

## Updating to a new version

```
git pull              # if you cloned with Git
docker compose down
docker compose pull   # pull any new image versions
docker compose build  # rebuild the backend image
docker compose up -d
```

Your `data/` folder is preserved. Database migrations run
automatically on the next start.

If you downloaded a ZIP, replace the contents of the Paperclip folder
with the new ZIP's contents — but **leave `data/` and your edited
`docker-compose.yml` alone**. Then run the launcher again.

---

## Troubleshooting

**The launcher says "Docker is not installed."**
Docker Desktop isn't running. Open it from the Start menu / Applications
folder and wait for the whale icon to say *"Engine running"*, then run
the launcher again.

**The browser shows "This site can't be reached"** at <http://localhost:8080/>.
Wait 30 seconds — first start can take a couple of minutes. Still
nothing? In a terminal in the Paperclip folder run `docker compose ps`.
Both `paperclip-backend-1` and `paperclip-frontend-1` should say `Up`.
If one is `Restarting`, run `docker compose logs backend` to see why.

**"Sign in failed"** even though I typed my password correctly.
In dev mode, you have to use a username from
`PAPERCLIP_AUTH_DEV_USERS` — check `docker-compose.yml`.
With real LDAPS configured, the supplied username must be a member of
the configured FOIA security group. The error message is intentionally
generic (it doesn't tell you whether the password was wrong, the user
doesn't exist, or the group check failed) so attackers can't probe.
Ask your IT contact.

**"AI scan failed"**
That's fine — the AI layer is optional and disabled by default.
Everything else works without it.

**The case page says a stage failed.**
Click into the case to see which one. The most common cause is
optional binaries (Tesseract for OCR, LibreOffice for `.docx`
conversion) not being available inside the container. Those stages
fail with `extract failed: …` but the rest of the pipeline keeps
going. The Docker image already includes both binaries — if you see
this, your Docker image build was incomplete; re-run
`docker compose build`.

**I want to start over with no data.**
`docker compose down` then delete the `data/` folder. Re-launch.

**I'm behind a corporate firewall and Docker can't pull images.**
Configure your proxy in *Docker Desktop* → *Settings* → *Resources*
→ *Proxies*. Or ask IT to mirror the images internally.

**I want to expose Paperclip on a port other than 8080.**
Set `PAPERCLIP_PORT` in `docker-compose.yml` (or in a `.env` file in
the same folder). Default is `8080`.

---

## Reference

### What Paperclip does, end-to-end

```
.mbox file uploaded in the browser
   │
   ▼
SQLite database  +  attachments stored on disk
   │
   ▼  Ingest    Read emails, save attachments, sanitize HTML
   ▼  Extract   OCR images, parse PDFs, convert Office docs to text
   ▼  Detect    Find SSNs, phones, emails, dates, student IDs
   ▼  Resolve   Group senders/recipients into unified people
   ▼  Propose   Auto-suggest redactions from each PII detection
   │
   ▼ Reviewer accepts or rejects in the browser (LDAPS-authenticated)
   │
   ▼  Export    Burn black boxes into a Bates-numbered PDF
   │
   ▼
production.pdf  +  redaction_log.csv
```

Steps 1–5 (ingest → propose) run as a background pipeline in a single
HTTP upload. The browser watches live progress via Server-Sent
Events. Review and export are explicit reviewer actions.

### Hard guarantees the system enforces

- **Local-first.** No case data leaves your deployment unless cloud
  AI is explicitly enabled per case.
- **Non-destructive redactions.** Source documents are never modified.
  Redactions are span overlays on top.
- **Human authority.** AI never produces a final redaction. Every
  exported black box was explicitly accepted by a logged-in reviewer.
- **Auditable.** Every write goes through an append-only `audit_log`
  table whose UPDATE / DELETE is blocked by database triggers. With
  LDAPS auth, every API write also carries a real `user_id` foreign
  key.
- **Distributable.** New district = new YAML
  ([`backend/config/district.example.yaml`](backend/config/district.example.yaml))
  and new env values. No code changes per district.

### Environment variables

You usually only set these in `docker-compose.yml`. The full table
lives in [`backend/README.md`](backend/README.md#authentication); the
ones you'll touch most often:

| Variable                              | Purpose                                                  |
|---------------------------------------|----------------------------------------------------------|
| `PAPERCLIP_PORT`                      | Public port for the web UI. Default `8080`.              |
| `PAPERCLIP_AUTH_DEV_MODE`             | `true` to skip LDAPS for laptop / first-run testing.     |
| `PAPERCLIP_AUTH_DEV_USERS`            | Comma-separated allowlist for dev mode.                  |
| `PAPERCLIP_LDAP_URI`                  | `ldaps://...`. Required for production auth.             |
| `PAPERCLIP_LDAP_BIND_DN` / `_PASSWORD`| Read-only service account for the directory lookup.      |
| `PAPERCLIP_LDAP_USER_BASE_DN`         | Where staff users live in the directory.                 |
| `PAPERCLIP_LDAP_GROUP_DN`             | The FOIA security group that gates access.               |
| `PAPERCLIP_LDAP_CA_CERT_PATH`         | Mount path of the CA certificate file.                   |
| `PAPERCLIP_AUTH_LOCKOUT_THRESHOLD`    | Failed-attempt lockout count. Default `5`.               |
| `PAPERCLIP_AUTH_LOCKOUT_WINDOW_MINUTES` | Window for the count. Default `15`.                    |
| `PAPERCLIP_AUTH_GROUP_RECHECK_MINUTES` | How often to re-verify group membership. Default `15`.  |
| `FOIA_CONFIG_FILE`                    | District YAML path. Default `/config/district.yaml`.     |

### Repository layout

```
paperclip/
├── README.md                 (this file)
├── docker-compose.yml        Production-shape deployment
├── paperclip.bat             Windows launcher  (double-click to start)
├── paperclip.command         macOS launcher    (double-click to start)
├── paperclip.desktop / .sh   Linux launchers
├── deploy/
│   ├── nginx.conf            Reverse proxy: SPA + /api proxy + SSE
│   └── DESKTOP_BUNDLE.md     Future single-binary distribution plan
├── backend/                  Python (FastAPI), pipeline modules
│   ├── README.md             Phase-by-phase technical reference
│   ├── foia/                 Source code
│   ├── tests/                386 tests, no live DC required
│   └── Dockerfile
└── frontend/                 React + TypeScript UI
    ├── src/
    └── dist/                 Built bundle (created by the launcher)
```

For the per-phase technical writeup — schemas, endpoints, design
tradeoffs, and the post-Phase-10 auth/cases/SSE/temporal-classifier
layer — see [`backend/README.md`](backend/README.md).

---

## For developers

The everyday path is the launcher + Docker. To work *on* Paperclip:

```bash
# 1. From the repo root, create + activate a venv.
python -m venv .venv
# Windows (CMD/PowerShell):  .venv\Scripts\activate
# macOS / Linux:             source .venv/bin/activate

# 2. Backend
cd backend
python -m pip install -r requirements.txt

# Set dev-mode auth in your shell. Pick the syntax for your shell:
#   bash / zsh:    export PAPERCLIP_AUTH_DEV_MODE=true && export PAPERCLIP_AUTH_DEV_USERS=alice
#   PowerShell:    $env:PAPERCLIP_AUTH_DEV_MODE='true'; $env:PAPERCLIP_AUTH_DEV_USERS='alice'
#   CMD:           set PAPERCLIP_AUTH_DEV_MODE=true && set PAPERCLIP_AUTH_DEV_USERS=alice
# Then:
python serve.py --port 8000

# 3. Frontend (in another terminal)
cd frontend
npm install
npm run dev          # http://localhost:5173

# 4. Sign in as `alice` with any password.

# 5. Tests (from backend/, venv active)
python -m pytest
```

The test suite is 386 tests. It never touches a real DC; LDAPS is
exercised through an injected adapter.
