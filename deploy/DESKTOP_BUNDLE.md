# Single-binary desktop bundle (Tauri vs PyInstaller+pywebview)

The Docker Compose path is the multi-user deployment. For a small or
solo district that wants Paperclip behave like an installed application
(double-click an icon, no terminal, no separate Docker install), a
desktop bundle is the right shape.

This document records the recommended path and the cold-start /
binary-size tradeoff. The bundle itself is built out-of-band (the CI
job is in `.github/workflows/desktop.yml` once stood up) — this repo
does not commit binary artefacts.

## Decision: PyInstaller + pywebview

We recommend **PyInstaller + pywebview** rather than Tauri:

| Concern              | PyInstaller + pywebview              | Tauri                                 |
|----------------------|--------------------------------------|---------------------------------------|
| Backend language     | Python (the existing FastAPI app)    | Rust shell + Python sidecar           |
| Toolchain on dev box | Python only                          | Rust + Python + Node                  |
| Cold start           | ~2–3s (Python interpreter init)      | ~1s (native shell)                    |
| Bundle size          | ~70–120 MB compressed                | ~10–30 MB shell + Python sidecar      |
| OCR / LibreOffice    | Bundle separately (large) or guide   | Same problem, no advantage            |
| Build complexity     | Low — one PyInstaller spec per OS    | Higher — Rust toolchain in CI         |

Both approaches still need Tesseract and LibreOffice installed
**outside the bundle** for OCR / Office handlers (Office's installer
size and licence make embedding impractical). The Phase 2 handlers
already degrade gracefully when those binaries are missing.

The deciding factor is that PyInstaller's bundle ships our existing
Python codebase verbatim. There's no Rust↔Python IPC glue to write or
debug, and CI doesn't need a Rust toolchain. The cold-start tradeoff
(2–3s vs 1s) is invisible inside ordinary single-launch usage.

## What the bundle does at runtime

1. PyInstaller packs `backend/` into a single `paperclip-backend` exe
   (the existing FastAPI app + uvicorn).
2. The desktop shell (a small Python launcher with `pywebview`) starts
   the backend as a child process on `127.0.0.1:<random-free-port>`.
3. It then opens a native window pointed at that port. The bundled
   frontend lives in the same exe and is served by FastAPI as static
   files (a `StaticFiles` mount on `/`, only loaded when running in
   bundled mode).
4. On window close, the launcher signals the backend, waits for a
   clean shutdown, and exits.

## Building

```
# In an empty venv:
pip install -r backend/requirements.txt pywebview pyinstaller
cd frontend && npm install && npm run build && cd ..

# Per-OS spec files live under deploy/pyinstaller/. They embed the
# built frontend, the example district YAML, and the Python source.
pyinstaller deploy/pyinstaller/paperclip-windows.spec   # produces dist/Paperclip.exe
pyinstaller deploy/pyinstaller/paperclip-macos.spec     # produces dist/Paperclip.app
pyinstaller deploy/pyinstaller/paperclip-linux.spec     # produces dist/paperclip
```

Auto-update is **not** in v1. To upgrade: download the new bundle,
replace the executable, and relaunch. The data directory
(`%APPDATA%\Paperclip` on Windows, `~/Library/Application Support/Paperclip`
on macOS, `~/.local/share/paperclip` on Linux) is untouched by
re-installs, so the SQLite DB and attachments survive upgrades.

## What is NOT bundled

* Tesseract OCR — install via the OS package manager. The launcher
  detects it on `PATH` and lights up the OCR pipeline; otherwise
  scanned-PDF attachments produce a clean "OCR unavailable" error
  and the rest of the pipeline keeps working.
* LibreOffice — same story.
* The directory CA certificate. The bundle reads
  `PAPERCLIP_LDAP_CA_CERT_PATH` from the env / the user's settings
  file. IT staff push the cert to a known path during workstation
  provisioning.

## Status

This document describes the path. The PyInstaller spec files are
deliberately **not** in the repo yet — the v1 deliverable is the
Docker-Compose-with-launcher path that already works on Windows /
macOS / Linux without Rust or PyInstaller. The desktop bundle
arrives in a follow-up release.
