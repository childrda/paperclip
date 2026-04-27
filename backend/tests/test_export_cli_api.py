"""End-to-end tests for the export CLI and API endpoint."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from foia.api.app import create_app
from foia.config import Config
from foia.db import connect, init_schema


def _yaml(tmp_path: Path) -> Path:
    p = tmp_path / "d.yaml"
    p.write_text(
        """
district:
  name: Test
pii_detection:
  builtins: []
exemption_codes: [{code: FERPA}, {code: PII}]
redaction:
  default_exemption: FERPA
bates:
  prefix: ECPS
  start: 100
  width: 5
""",
        encoding="utf-8",
    )
    return p


def _seed(tmp_path: Path, *, with_redaction: bool = True) -> tuple[Path, Path, int]:
    db_path = tmp_path / "exp.db"
    yaml_path = _yaml(tmp_path)
    conn = connect(db_path)
    init_schema(conn)
    cur = conn.execute(
        """
        INSERT INTO emails (mbox_source, mbox_index, subject, body_text,
                            body_html_sanitized, ingested_at)
        VALUES ('t.mbox', 0, 'Subj', 'Body has SECRET token here.', '', ?)
        """,
        (datetime.now(timezone.utc).isoformat(),),
    )
    eid = int(cur.lastrowid)
    if with_redaction:
        body = "Body has SECRET token here."
        i = body.index("SECRET")
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO redactions (
                source_type, source_id, start_offset, end_offset,
                exemption_code, status, origin, reviewer_id,
                created_at, updated_at
            ) VALUES ('email_body_text', ?, ?, ?, 'FERPA', 'accepted',
                      'manual', 'Records Clerk', ?, ?)
            """,
            (eid, i, i + len("SECRET"), now, now),
        )
    conn.commit()
    conn.close()
    return db_path, yaml_path, eid


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_exports_pdf_and_csv(tmp_path: Path, capsys):
    db, cfg, _ = _seed(tmp_path)
    out_dir = tmp_path / "out"

    import export as export_cli
    rc = export_cli.main(
        ["--db", str(db), "--config", str(cfg), "--out", str(out_dir)]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["emails_exported"] == 1
    assert payload["redactions_burned"] == 1
    assert payload["bates_first"] == "ECPS-00100"
    assert (out_dir / "production.pdf").exists()
    assert (out_dir / "redaction_log.csv").exists()


def test_cli_missing_db(tmp_path: Path):
    import export as export_cli
    rc = export_cli.main(
        ["--db", str(tmp_path / "nope.db"),
         "--out", str(tmp_path / "x")]
    )
    assert rc == 2


def test_cli_invalid_emails_arg(tmp_path: Path):
    db, cfg, _ = _seed(tmp_path)
    import export as export_cli
    rc = export_cli.main(
        ["--db", str(db), "--config", str(cfg),
         "--out", str(tmp_path / "x"), "--emails", "not-a-number"]
    )
    assert rc == 2


def test_cli_emails_filter(tmp_path: Path, capsys):
    db, cfg, eid = _seed(tmp_path)
    out_dir = tmp_path / "out"
    import export as export_cli
    rc = export_cli.main(
        ["--db", str(db), "--config", str(cfg),
         "--out", str(out_dir), "--emails", str(eid)]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["emails_exported"] == 1


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture()
def api_client(tmp_path: Path, monkeypatch):
    db, yaml_path, eid = _seed(tmp_path)
    monkeypatch.setenv("FOIA_CONFIG_FILE", str(yaml_path))
    cfg = Config(
        db_path=db, attachment_dir=tmp_path / "att", log_level="WARNING",
        ocr_enabled=False, ocr_language="eng", ocr_dpi=200,
        tesseract_cmd=None, office_enabled=False,
        libreoffice_cmd="soffice", extraction_timeout_s=60,
        cors_origins=("http://localhost:5173",),
        export_dir=tmp_path / "exports",
    )
    return TestClient(create_app(cfg)), eid


def test_api_post_export_returns_manifest(api_client):
    client, _ = api_client
    r = client.post("/api/v1/exports", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["emails_exported"] == 1
    assert body["redactions_burned"] == 1
    assert body["bates_first"] == "ECPS-00100"
    assert body["pdf_url"].endswith("/production.pdf")
    assert body["csv_url"].endswith("/redaction_log.csv")
    assert body["export_id"]


def test_api_extra_field_rejected(api_client):
    client, _ = api_client
    r = client.post("/api/v1/exports", json={"evil": "x"})
    assert r.status_code == 422


def test_api_email_ids_filter(api_client):
    client, eid = api_client
    r = client.post("/api/v1/exports", json={"email_ids": [eid]})
    assert r.status_code == 200
    assert r.json()["emails_exported"] == 1


def test_api_download_pdf_and_csv(api_client):
    client, _ = api_client
    body = client.post("/api/v1/exports", json={}).json()
    pdf = client.get(body["pdf_url"])
    assert pdf.status_code == 200
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert pdf.content.startswith(b"%PDF")
    # Body of the PDF must NOT contain the redacted token.
    reader = PdfReader(io_bytes := __import__("io").BytesIO(pdf.content))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    assert "SECRET" not in text
    _ = io_bytes  # silence

    csv = client.get(body["csv_url"])
    assert csv.status_code == 200
    assert "FERPA" in csv.text


def test_api_download_unknown_filename_404(api_client):
    client, _ = api_client
    body = client.post("/api/v1/exports", json={}).json()
    r = client.get(f"/api/v1/exports/{body['export_id']}/evil.pdf")
    assert r.status_code == 404


def test_api_download_missing_export_404(api_client):
    client, _ = api_client
    r = client.get("/api/v1/exports/nope/production.pdf")
    assert r.status_code == 404


def test_api_path_traversal_rejected(api_client):
    client, _ = api_client
    # Both path-traversal forms should be either filtered by FastAPI or
    # rejected by the safety check inside the handler.
    r = client.get("/api/v1/exports/..%2F..%2Fetc/production.pdf")
    assert r.status_code in (400, 404)


def test_api_list_exports(api_client):
    client, _ = api_client
    body = client.post("/api/v1/exports", json={}).json()
    r = client.get("/api/v1/exports")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["export_id"] == body["export_id"] for row in rows)
    target = next(row for row in rows if row["export_id"] == body["export_id"])
    assert target["pdf_bytes"] > 0
    assert target["csv_bytes"] >= 0


def test_openapi_includes_export_routes(api_client):
    client, _ = api_client
    paths = set(client.get("/openapi.json").json()["paths"].keys())
    assert "/api/v1/exports" in paths
    assert "/api/v1/exports/{export_id}/{filename}" in paths
