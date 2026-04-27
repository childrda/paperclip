import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../api";
import type { ImportListItem, ImportSummary } from "../types";

export default function ImportPage() {
  const [file, setFile] = useState<File | null>(null);
  const [label, setLabel] = useState("");
  const [proposeRedactions, setProposeRedactions] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ImportSummary | null>(null);
  const [history, setHistory] = useState<ImportListItem[]>([]);
  const [dragOver, setDragOver] = useState(false);

  useEffect(() => {
    api.listImports().then(setHistory).catch(() => undefined);
  }, [result]);

  async function submit() {
    if (!file || busy) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const summary = await api.uploadImport({
        file,
        label: label.trim() || undefined,
        propose_redactions: proposeRedactions,
      });
      setResult(summary);
      setFile(null);
      setLabel("");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function onDrop(e: React.DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragOver(false);
    const f = e.dataTransfer.files?.[0];
    if (f) setFile(f);
  }

  return (
    <>
      <h2 style={{ marginTop: 0 }}>Import a mailbox</h2>
      <p className="muted">
        Upload an <code>.mbox</code> file. The server runs the full pipeline
        (ingest → extract → detect → resolve → propose) and you'll see the
        per-stage stats below when it finishes. After import, head to the{" "}
        <Link to="/emails">Emails</Link> page to review and accept the
        proposed redactions.
      </p>

      <div className="upload-card">
        <h2>Upload</h2>
        <div
          className={`drop-target ${dragOver ? "over" : ""}`}
          onDragOver={(e) => {
            e.preventDefault();
            setDragOver(true);
          }}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        >
          {file ? (
            <>
              <div>
                <strong>{file.name}</strong>{" "}
                <span className="muted">
                  ({(file.size / 1024).toFixed(1)} KB)
                </span>
              </div>
              <button
                onClick={() => setFile(null)}
                style={{ marginTop: 8 }}
                disabled={busy}
              >
                Choose a different file
              </button>
            </>
          ) : (
            <>
              <div>Drop a .mbox file here, or</div>
              <input
                type="file"
                accept=".mbox,application/mbox,application/octet-stream"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
                disabled={busy}
                style={{ marginTop: 8 }}
              />
            </>
          )}
        </div>

        <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
            Label:
            <input
              type="text"
              placeholder="optional case ID"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              disabled={busy}
              style={{ padding: "4px 8px" }}
            />
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <input
              type="checkbox"
              checked={proposeRedactions}
              onChange={(e) => setProposeRedactions(e.target.checked)}
              disabled={busy}
            />
            Auto-propose redactions from PII detections
          </label>
          <button
            onClick={submit}
            disabled={!file || busy}
            style={{ marginLeft: "auto" }}
          >
            {busy ? "Processing…" : "Run import"}
          </button>
        </div>
      </div>

      {error ? <div className="error">{error}</div> : null}

      {result ? <ImportResult summary={result} /> : null}

      <h2 style={{ marginTop: 24 }}>Recent imports</h2>
      {history.length === 0 ? (
        <p className="muted">No imports yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th style={{ width: 220 }}>When</th>
              <th>Filename</th>
              <th style={{ width: 140 }}>Label</th>
              <th style={{ width: 120 }}>Actor</th>
              <th style={{ width: 90 }}>Emails</th>
              <th style={{ width: 90 }}>Detect</th>
              <th style={{ width: 100 }}>Proposed</th>
            </tr>
          </thead>
          <tbody>
            {history.map((row, i) => {
              const ingested =
                row.stages.ingest?.emails_ingested ?? "—";
              const detections =
                row.stages.detect?.detections_written ?? "—";
              const propose = row.stages.propose;
              const proposed =
                propose && "skipped" in propose
                  ? "skipped"
                  : (propose?.proposed ?? "—");
              return (
                <tr key={i}>
                  <td>{row.event_at.replace("T", " ").slice(0, 19)}</td>
                  <td>{row.filename ?? <span className="muted">(missing)</span>}</td>
                  <td>{row.label ?? <span className="muted">—</span>}</td>
                  <td>{row.actor}</td>
                  <td>{ingested}</td>
                  <td>{detections}</td>
                  <td>{proposed}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}

function ImportResult({ summary }: { summary: ImportSummary }) {
  const elapsedMs =
    new Date(summary.finished_at).getTime() -
    new Date(summary.started_at).getTime();
  return (
    <div className="detail-section">
      <h2>Import {summary.import_id} — succeeded</h2>
      <p className="kvs">
        <span>file: {summary.filename}</span>
        {summary.label ? <span>label: {summary.label}</span> : null}
        <span>elapsed: {(elapsedMs / 1000).toFixed(1)} s</span>
      </p>
      <div className="stage-grid">
        <StageCard
          name="Ingest"
          rows={
            summary.stages.ingest
              ? Object.entries({
                  "emails ingested": summary.stages.ingest.emails_ingested,
                  "duplicates skipped":
                    summary.stages.ingest.emails_skipped_duplicate,
                  "attachments saved":
                    summary.stages.ingest.attachments_saved,
                  errors: summary.stages.ingest.errors,
                })
              : []
          }
          bad={(summary.stages.ingest?.errors ?? 0) > 0}
        />
        <StageCard
          name="Extract"
          rows={
            summary.stages.extract
              ? Object.entries({
                  total: summary.stages.extract.total,
                  ok: summary.stages.extract.extracted_ok,
                  empty: summary.stages.extract.extracted_empty,
                  unsupported: summary.stages.extract.unsupported,
                  failed: summary.stages.extract.failed,
                })
              : []
          }
          bad={(summary.stages.extract?.failed ?? 0) > 0}
        />
        <StageCard
          name="Detect"
          rows={
            summary.stages.detect
              ? [
                  ["sources scanned", summary.stages.detect.sources_scanned],
                  ["detections", summary.stages.detect.detections_written],
                  ...Object.entries(summary.stages.detect.by_entity ?? {}),
                ]
              : []
          }
        />
        <StageCard
          name="Resolve"
          rows={
            summary.stages.resolve
              ? Object.entries({
                  "emails scanned": summary.stages.resolve.emails_scanned,
                  "persons created":
                    summary.stages.resolve.persons_created,
                  "name variants added":
                    summary.stages.resolve.persons_updated,
                  "signatures w/ extra emails":
                    summary.stages.resolve.signatures_with_extra_emails,
                })
              : []
          }
        />
        <StageCard
          name="Propose"
          rows={
            summary.stages.propose && !("skipped" in summary.stages.propose)
              ? [
                  ["proposed", summary.stages.propose.proposed],
                  [
                    "skipped (existing)",
                    summary.stages.propose.skipped_existing,
                  ],
                  [
                    "no exemption",
                    summary.stages.propose.skipped_no_exemption,
                  ],
                  ...Object.entries(
                    summary.stages.propose.by_entity ?? {},
                  ),
                ]
              : [["status", "skipped"]]
          }
        />
      </div>
      <p style={{ marginTop: 12 }}>
        <Link to="/emails">→ Review the emails and proposed redactions</Link>
      </p>
    </div>
  );
}

function StageCard({
  name,
  rows,
  bad = false,
}: {
  name: string;
  rows: Array<[string, number | string]>;
  bad?: boolean;
}) {
  return (
    <div className={`stage-card ${bad ? "bad" : ""}`}>
      <h3>{name}</h3>
      {rows.length === 0 ? (
        <p className="muted">no data</p>
      ) : (
        rows.map(([k, v]) => (
          <div className="row" key={k}>
            <span className="k">{k}</span>
            <span className="v">{v}</span>
          </div>
        ))
      )}
    </div>
  );
}
