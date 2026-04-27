import { useEffect, useState } from "react";
import { Link, useParams, useNavigate } from "react-router-dom";
import { ApiError, api } from "../api";
import type { CaseDetail, CaseStatus } from "../types";

const STATUS_TINT: Record<CaseStatus, string> = {
  processing: "#f0ad4e",
  ready: "#1a8d3f",
  failed: "#c82828",
  exported: "#1a73e8",
  archived: "#888",
};

export default function CaseDetailPage() {
  const { id } = useParams<{ id: string }>();
  const caseId = Number(id);
  const navigate = useNavigate();
  const [data, setData] = useState<CaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  async function reload() {
    try {
      setData(await api.getCase(caseId));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  useEffect(() => {
    if (Number.isNaN(caseId)) return;
    reload();
    // While processing, poll every 3s.
    const t = setInterval(() => {
      if (data?.case.status === "processing") reload();
    }, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId, data?.case.status]);

  async function runExport() {
    if (exporting || !data) return;
    setExporting(true);
    try {
      const m = await api.createExport({});
      window.open(m.pdf_url, "_blank", "noopener");
      await api.setCaseStatus(caseId, "exported");
      await reload();
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`Export failed: ${msg}`);
    } finally {
      setExporting(false);
    }
  }

  if (!data) {
    return (
      <>
        {error ? <div className="error">{error}</div> : <p className="muted">Loading…</p>}
      </>
    );
  }

  const c = data.case;
  return (
    <>
      <div className="page-toolbar">
        <Link to="/cases" className="back-link">← All cases</Link>
        <h2 style={{ flex: 1, marginLeft: 12 }}>{c.name}</h2>
        <span
          className="status-pill"
          style={{ background: STATUS_TINT[c.status] }}
        >
          {c.status === "ready" ? "Ready for review" : c.status}
        </span>
      </div>

      <div className="kvs">
        <span>id: {c.id}</span>
        <span>Bates: <code>{c.bates_prefix}</code></span>
        <span>created: {c.created_at.replace("T", " ").slice(0, 19)}</span>
        {c.failed_stage ? <span>failed at: {c.failed_stage}</span> : null}
      </div>

      {c.error_message ? (
        <div className="error" style={{ marginTop: 12 }}>
          {c.error_message}
        </div>
      ) : null}

      <div className="stat-row">
        <Stat label="Emails" value={data.stats.emails} />
        <Stat label="Attachments" value={data.stats.attachments} />
        <Stat label="PII spans" value={data.stats.pii_detections} />
        <Stat
          label="Redactions"
          value={`${data.stats.redactions_accepted} / ${data.stats.redactions} accepted`}
        />
      </div>

      <div className="form-actions" style={{ marginTop: 16 }}>
        <button
          className="primary"
          disabled={c.status !== "ready" && c.status !== "exported"}
          onClick={() => navigate(`/cases/${c.id}/emails`)}
        >
          Review emails →
        </button>
        <button
          disabled={
            exporting ||
            c.status === "processing" ||
            c.status === "failed"
          }
          onClick={runExport}
          style={{ background: "#1a8d3f", color: "#fff", borderColor: "#156c30" }}
        >
          {exporting ? "Generating…" : "Export production PDF"}
        </button>
        {c.status !== "archived" ? (
          <button
            onClick={async () => {
              await api.setCaseStatus(c.id, "archived");
              await reload();
            }}
          >
            Archive
          </button>
        ) : null}
      </div>
    </>
  );
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
    </div>
  );
}
