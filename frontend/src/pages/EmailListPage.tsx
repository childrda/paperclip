import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api } from "../api";
import type { EmailSummary, Page, Stats } from "../types";

const PAGE_SIZE = 25;

export default function EmailListPage() {
  // The same component renders both the global ``/emails`` list and
  // the case-scoped ``/cases/:id/emails`` list. ``id`` is only present
  // on the case-scoped route; when missing we list every email.
  const { id: caseIdParam } = useParams<{ id: string }>();
  const caseId =
    caseIdParam && !Number.isNaN(Number(caseIdParam))
      ? Number(caseIdParam)
      : undefined;

  const [page, setPage] = useState<Page<EmailSummary> | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [pendingSearch, setPendingSearch] = useState("");
  const [hasPii, setHasPii] = useState<boolean | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let aborted = false;
    setLoading(true);
    setError(null);
    api
      .listEmails({
        limit: PAGE_SIZE,
        offset,
        subject_contains: search || undefined,
        has_pii: hasPii,
        case_id: caseId,
      })
      .then((p) => {
        if (!aborted) setPage(p);
      })
      .catch((e: unknown) => {
        if (!aborted) {
          setError(e instanceof ApiError ? e.message : "fetch failed");
        }
      })
      .finally(() => {
        if (!aborted) setLoading(false);
      });
    return () => {
      aborted = true;
    };
  }, [offset, search, hasPii, caseId]);

  useEffect(() => {
    api.getStats().then(setStats).catch(() => undefined);
  }, []);

  const totalPages = page ? Math.max(1, Math.ceil(page.total / PAGE_SIZE)) : 1;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  const [busyId, setBusyId] = useState<number | null>(null);
  async function handleInclude(id: number) {
    if (busyId !== null) return;
    setBusyId(id);
    try {
      await api.includeEmail(id);
      // Refresh in place — the row's struck-through styling drops off
      // and the case stats above will pick up the change on next load.
      setPage((p) =>
        p
          ? {
              ...p,
              items: p.items.map((row) =>
                row.id === id ? { ...row, is_excluded: false } : row,
              ),
            }
          : p,
      );
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`Include failed: ${msg}`);
    } finally {
      setBusyId(null);
    }
  }

  async function handleExclude(id: number) {
    if (busyId !== null) return;
    const reason = prompt(
      "Reason for withholding this email from the production " +
        "(e.g. 'attorney-client privileged', 'non-responsive', " +
        "'duplicate'). Optional but recommended for the audit trail.",
      "",
    );
    if (reason === null) return;
    setBusyId(id);
    try {
      await api.excludeEmail(id, reason || undefined);
      setPage((p) =>
        p
          ? {
              ...p,
              items: p.items.map((row) =>
                row.id === id ? { ...row, is_excluded: true } : row,
              ),
            }
          : p,
      );
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`Exclude failed: ${msg}`);
    } finally {
      setBusyId(null);
    }
  }

  const [exporting, setExporting] = useState(false);

  async function runExport() {
    if (exporting) return;
    setExporting(true);
    try {
      const m = await api.createExport({});
      // Open the PDF in a new tab; surface the manifest in a brief alert.
      window.open(m.pdf_url, "_blank", "noopener");
      alert(
        `Export ${m.export_id} ready:\n` +
          `${m.pages_written} pages · ${m.redactions_burned} redactions burned\n` +
          `Bates ${m.bates_first}..${m.bates_last}\n\n` +
          `CSV log: ${m.csv_url}`,
      );
    } catch (e) {
      alert(`Export failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setExporting(false);
    }
  }

  return (
    <>
      {caseId !== undefined ? (
        <div className="page-toolbar">
          <Link to={`/cases/${caseId}`} className="back-link">
            ← Back to case
          </Link>
        </div>
      ) : null}
      <div className="toolbar">
        <input
          type="text"
          placeholder="Search subject…"
          value={pendingSearch}
          onChange={(e) => setPendingSearch(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setOffset(0);
              setSearch(pendingSearch.trim());
            }
          }}
        />
        <button
          onClick={() => {
            setOffset(0);
            setSearch(pendingSearch.trim());
          }}
        >
          Search
        </button>
        <label style={{ marginLeft: 12 }}>
          <input
            type="checkbox"
            checked={hasPii === true}
            onChange={(e) => {
              setOffset(0);
              setHasPii(e.target.checked ? true : undefined);
            }}
          />{" "}
          With PII only
        </label>
        <button
          onClick={runExport}
          disabled={exporting}
          style={{ marginLeft: 12, background: "#1a8d3f", borderColor: "#156c30" }}
          title="Generate a redacted PDF + CSV of the entire production"
        >
          {exporting ? "Exporting…" : "Export PDF"}
        </button>
        {stats ? (
          <span className="muted" style={{ marginLeft: "auto" }}>
            {stats.emails} emails · {stats.pii_detections} PII spans · {stats.redactions_accepted}/{stats.redactions} accepted
          </span>
        ) : null}
      </div>

      {error ? <div className="error">{error}</div> : null}

      <table>
        <thead>
          <tr>
            <th style={{ width: 60 }}>ID</th>
            <th style={{ width: 220 }}>Date sent</th>
            <th>Subject</th>
            <th style={{ width: 240 }}>From</th>
            <th style={{ width: 90 }}>PII</th>
            <th style={{ width: 90 }}>Attach</th>
            <th style={{ width: 110 }}>Actions</th>
          </tr>
        </thead>
        <tbody>
          {loading && !page ? (
            <tr>
              <td colSpan={7} className="muted">
                Loading…
              </td>
            </tr>
          ) : page && page.items.length === 0 ? (
            <tr>
              <td colSpan={7} className="muted">
                No emails match.
              </td>
            </tr>
          ) : (
            page?.items.map((row) => (
              <tr
                key={row.id}
                style={
                  row.is_excluded
                    ? {
                        textDecoration: "line-through",
                        color: "#999",
                        background: "#fafafa",
                      }
                    : undefined
                }
              >
                <td>{row.id}</td>
                <td>{row.date_sent ?? <span className="muted">—</span>}</td>
                <td>
                  <Link to={`/emails/${row.id}`}>
                    {row.subject ?? <span className="muted">(no subject)</span>}
                  </Link>
                  {row.is_excluded ? (
                    <span
                      className="badge rejected"
                      style={{ marginLeft: 8, fontSize: 10 }}
                      title="Withheld from production"
                    >
                      excluded
                    </span>
                  ) : null}
                </td>
                <td>{row.from_addr ?? <span className="muted">—</span>}</td>
                <td>{row.pii_count > 0 ? row.pii_count : ""}</td>
                <td>{row.has_attachments ? "yes" : ""}</td>
                <td style={{ textDecoration: "none" }}>
                  {row.is_excluded ? (
                    <button
                      onClick={() => handleInclude(row.id)}
                      disabled={busyId !== null}
                      style={{ fontSize: 11, padding: "2px 8px" }}
                      title="Bring this email back into the production."
                    >
                      {busyId === row.id ? "…" : "Include"}
                    </button>
                  ) : (
                    <button
                      onClick={() => handleExclude(row.id)}
                      disabled={busyId !== null}
                      style={{
                        fontSize: 11,
                        padding: "2px 8px",
                        background: "#fff",
                        color: "#c82828",
                        borderColor: "#c82828",
                      }}
                      title="Withhold this email from the production."
                    >
                      {busyId === row.id ? "…" : "Exclude"}
                    </button>
                  )}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>

      <div className="pager">
        <button
          disabled={offset === 0 || loading}
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
        >
          ← Prev
        </button>
        <span>
          Page {currentPage} of {totalPages}
        </span>
        <button
          disabled={!page || offset + PAGE_SIZE >= page.total || loading}
          onClick={() => setOffset(offset + PAGE_SIZE)}
        >
          Next →
        </button>
      </div>
    </>
  );
}
