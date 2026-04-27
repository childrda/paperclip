import { useState } from "react";
import { Link } from "react-router-dom";
import { ApiError, api } from "../api";
import type { Page, SearchHit } from "../types";

export default function SearchPage() {
  const [q, setQ] = useState("");
  const [scope, setScope] = useState<"" | "emails" | "attachments">("");
  const [results, setResults] = useState<Page<SearchHit> | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function go(e?: React.FormEvent) {
    e?.preventDefault();
    if (!q.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.search(q.trim(), scope || undefined);
      setResults(r);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h2 style={{ marginTop: 0 }}>Full-text search</h2>
      <p className="muted">
        Searches across email subjects, bodies, and extracted attachment
        text. Punctuation is treated as literal — no FTS5 syntax to learn.
      </p>

      <form className="toolbar" onSubmit={go}>
        <input
          type="text"
          placeholder="Search…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          autoFocus
        />
        <select
          value={scope}
          onChange={(e) =>
            setScope(e.target.value as "" | "emails" | "attachments")
          }
        >
          <option value="">All sources</option>
          <option value="emails">Emails only</option>
          <option value="attachments">Attachments only</option>
        </select>
        <button type="submit" disabled={!q.trim() || busy}>
          {busy ? "Searching…" : "Search"}
        </button>
      </form>

      {error ? <div className="error">{error}</div> : null}

      {results ? (
        <>
          <p className="muted">
            {results.total} {results.total === 1 ? "result" : "results"}
          </p>
          {results.items.map((h, i) => (
            <Hit key={`${h.source_type}-${h.source_id}-${i}`} hit={h} />
          ))}
          {results.items.length === 0 ? (
            <p className="muted">No matches.</p>
          ) : null}
        </>
      ) : null}
    </>
  );
}

function Hit({ hit }: { hit: SearchHit }) {
  const target =
    hit.source_type === "email"
      ? `/emails/${hit.source_id}`
      : hit.email_id != null
      ? `/emails/${hit.email_id}`
      : `/emails`;
  return (
    <Link to={target} className="search-hit-link" style={{ textDecoration: "none", color: "inherit" }}>
      <div className="search-hit">
        <span className="source-tag">{hit.source_type}</span>
        <strong>{hit.title}</strong>
        <div
          style={{ marginTop: 6 }}
          dangerouslySetInnerHTML={{ __html: hit.snippet }}
        />
      </div>
    </Link>
  );
}
