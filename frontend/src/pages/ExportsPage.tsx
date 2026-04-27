import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { ExportListItem, ExportManifest } from "../types";

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

export default function ExportsPage() {
  const [items, setItems] = useState<ExportListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [includeAttachments, setIncludeAttachments] = useState(true);
  const [lastResult, setLastResult] = useState<ExportManifest | null>(null);

  async function reload() {
    try {
      setItems(await api.listExports());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  useEffect(() => {
    reload();
  }, []);

  async function newExport() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const m = await api.createExport({
        include_attachments: includeAttachments,
      });
      setLastResult(m);
      await reload();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h2 style={{ marginTop: 0 }}>Exports</h2>
      <p className="muted">
        Each export is a Bates-numbered <code>production.pdf</code> with
        every <em>accepted</em> redaction burned in, plus a CSV log mapping
        each redaction to its Bates page. Generated PDFs are saved on the
        server and remain available here for download.
      </p>

      <div className="toolbar">
        <label>
          <input
            type="checkbox"
            checked={includeAttachments}
            onChange={(e) => setIncludeAttachments(e.target.checked)}
          />{" "}
          Include extracted attachment text
        </label>
        <button
          onClick={newExport}
          disabled={busy}
          style={{ marginLeft: "auto", background: "#1a8d3f", borderColor: "#156c30" }}
        >
          {busy ? "Generating…" : "New export"}
        </button>
      </div>

      {error ? <div className="error">{error}</div> : null}

      {lastResult ? (
        <div className="detail-section">
          <h2>Just generated</h2>
          <p className="kvs">
            <span>id: {lastResult.export_id}</span>
            <span>pages: {lastResult.pages_written}</span>
            <span>redactions burned: {lastResult.redactions_burned}</span>
            <span>
              Bates {lastResult.bates_first}..{lastResult.bates_last}
            </span>
          </p>
          <p>
            <a href={lastResult.pdf_url} target="_blank" rel="noreferrer">
              Download PDF
            </a>
            {" · "}
            <a href={lastResult.csv_url} target="_blank" rel="noreferrer">
              Download CSV
            </a>
          </p>
        </div>
      ) : null}

      <h3>Past exports</h3>
      {items.length === 0 ? (
        <p className="muted">No exports yet.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Export ID</th>
              <th style={{ width: 220 }}>Created</th>
              <th style={{ width: 110 }}>PDF size</th>
              <th style={{ width: 110 }}>CSV size</th>
              <th style={{ width: 160 }}>Files</th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.export_id}>
                <td>
                  <code>{row.export_id}</code>
                </td>
                <td>{row.created_at.replace("T", " ").slice(0, 19)}</td>
                <td>{fmtBytes(row.pdf_bytes)}</td>
                <td>{fmtBytes(row.csv_bytes)}</td>
                <td>
                  <a href={row.pdf_url} target="_blank" rel="noreferrer">
                    PDF
                  </a>
                  {" · "}
                  <a href={row.csv_url} target="_blank" rel="noreferrer">
                    CSV
                  </a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
