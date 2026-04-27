import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiError, api } from "../api";
import type { Case, CaseStatus } from "../types";

const STATUS_TINT: Record<CaseStatus, string> = {
  processing: "#f0ad4e",
  ready: "#1a8d3f",
  failed: "#c82828",
  exported: "#1a73e8",
  archived: "#888",
};

function fmtTimestamp(iso: string): string {
  return iso.replace("T", " ").slice(0, 19);
}

export default function CasesPage() {
  const navigate = useNavigate();
  const [cases, setCases] = useState<Case[]>([]);
  const [filter, setFilter] = useState<CaseStatus | "">("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function reload() {
    setLoading(true);
    setError(null);
    try {
      const r = await api.listCases({ limit: 100, status: filter || undefined });
      setCases(r.items);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    reload();
    // Auto-refresh while a case is processing — cheap polling, only
    // when the page is open and there's something to refresh.
    const t = setInterval(() => {
      const anyProcessing = cases.some((c) => c.status === "processing");
      if (anyProcessing) reload();
    }, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  return (
    <>
      <div className="page-toolbar">
        <h2>Cases</h2>
        <div className="spacer" />
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value as CaseStatus | "")}
        >
          <option value="">All statuses</option>
          <option value="processing">Processing</option>
          <option value="ready">Ready for review</option>
          <option value="failed">Failed</option>
          <option value="exported">Exported</option>
          <option value="archived">Archived</option>
        </select>
        <button
          className="primary"
          onClick={() => navigate("/cases/new")}
        >
          + New case
        </button>
      </div>

      {error ? <div className="error">{error}</div> : null}

      {loading && cases.length === 0 ? (
        <p className="muted">Loading…</p>
      ) : cases.length === 0 ? (
        <div className="empty-state">
          <p>No cases yet.</p>
          <button className="primary" onClick={() => navigate("/cases/new")}>
            Start a new case
          </button>
        </div>
      ) : (
        <table>
          <thead>
            <tr>
              <th style={{ width: 60 }}>ID</th>
              <th>Name</th>
              <th style={{ width: 120 }}>Bates prefix</th>
              <th style={{ width: 160 }}>Status</th>
              <th style={{ width: 200 }}>Created</th>
            </tr>
          </thead>
          <tbody>
            {cases.map((c) => (
              <tr key={c.id} className="row-link">
                <td>{c.id}</td>
                <td>
                  <Link to={`/cases/${c.id}`}>{c.name}</Link>
                </td>
                <td>
                  <code>{c.bates_prefix}</code>
                </td>
                <td>
                  <span
                    className="status-pill"
                    style={{ background: STATUS_TINT[c.status] }}
                  >
                    {c.status === "ready" ? "Ready for review" : c.status}
                  </span>
                  {c.failed_stage ? (
                    <span className="muted small" style={{ marginLeft: 8 }}>
                      ({c.failed_stage} failed)
                    </span>
                  ) : null}
                </td>
                <td>{fmtTimestamp(c.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
