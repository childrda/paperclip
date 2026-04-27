import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../api";
import type { EmailSummary, Page, Stats } from "../types";

const PAGE_SIZE = 25;

export default function EmailListPage() {
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
  }, [offset, search, hasPii]);

  useEffect(() => {
    api.getStats().then(setStats).catch(() => undefined);
  }, []);

  const totalPages = page ? Math.max(1, Math.ceil(page.total / PAGE_SIZE)) : 1;
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

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
          </tr>
        </thead>
        <tbody>
          {loading && !page ? (
            <tr>
              <td colSpan={6} className="muted">
                Loading…
              </td>
            </tr>
          ) : page && page.items.length === 0 ? (
            <tr>
              <td colSpan={6} className="muted">
                No emails match.
              </td>
            </tr>
          ) : (
            page?.items.map((row) => (
              <tr key={row.id}>
                <td>{row.id}</td>
                <td>{row.date_sent ?? <span className="muted">—</span>}</td>
                <td>
                  <Link to={`/emails/${row.id}`}>
                    {row.subject ?? <span className="muted">(no subject)</span>}
                  </Link>
                </td>
                <td>{row.from_addr ?? <span className="muted">—</span>}</td>
                <td>{row.pii_count > 0 ? row.pii_count : ""}</td>
                <td>{row.has_attachments ? "yes" : ""}</td>
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
