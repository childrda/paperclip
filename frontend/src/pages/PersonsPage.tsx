import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { Page, PersonSummary } from "../types";

const PAGE_SIZE = 50;

export default function PersonsPage() {
  const [data, setData] = useState<Page<PersonSummary> | null>(null);
  const [offset, setOffset] = useState(0);
  const [nameFilter, setNameFilter] = useState("");
  const [pendingName, setPendingName] = useState("");
  const [internalOnly, setInternalOnly] = useState<boolean | undefined>(
    undefined,
  );
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .listPersons({
        limit: PAGE_SIZE,
        offset,
        name_contains: nameFilter || undefined,
        is_internal: internalOnly,
      })
      .then((p) => {
        if (!cancelled) setData(p);
      })
      .catch((e) =>
        setError(e instanceof ApiError ? e.message : String(e)),
      )
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [offset, nameFilter, internalOnly]);

  return (
    <>
      <h2 style={{ marginTop: 0 }}>People</h2>
      <p className="muted">
        Unified identities across the corpus. The same email always maps to
        one person; multiple email aliases per person come from manual merges
        on the back end.
      </p>

      <div className="toolbar">
        <input
          type="text"
          placeholder="Filter by name…"
          value={pendingName}
          onChange={(e) => setPendingName(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setOffset(0);
              setNameFilter(pendingName.trim());
            }
          }}
        />
        <button
          onClick={() => {
            setOffset(0);
            setNameFilter(pendingName.trim());
          }}
        >
          Filter
        </button>
        <label style={{ marginLeft: 12 }}>
          <input
            type="checkbox"
            checked={internalOnly === true}
            onChange={(e) =>
              setInternalOnly(e.target.checked ? true : undefined)
            }
          />{" "}
          Internal only
        </label>
        <label style={{ marginLeft: 8 }}>
          <input
            type="checkbox"
            checked={internalOnly === false}
            onChange={(e) =>
              setInternalOnly(e.target.checked ? false : undefined)
            }
          />{" "}
          External only
        </label>
        {data ? (
          <span className="muted" style={{ marginLeft: "auto" }}>
            {data.total} total
          </span>
        ) : null}
      </div>

      {error ? <div className="error">{error}</div> : null}

      <table>
        <thead>
          <tr>
            <th style={{ width: 60 }}>ID</th>
            <th>Display name</th>
            <th>Primary email</th>
            <th style={{ width: 90 }}>Internal?</th>
            <th style={{ width: 130 }}>Occurrences</th>
          </tr>
        </thead>
        <tbody>
          {loading && !data ? (
            <tr>
              <td colSpan={5} className="muted">
                Loading…
              </td>
            </tr>
          ) : data && data.items.length === 0 ? (
            <tr>
              <td colSpan={5} className="muted">
                No people match.
              </td>
            </tr>
          ) : (
            data?.items.map((p) => (
              <tr key={p.id}>
                <td>{p.id}</td>
                <td>{p.display_name}</td>
                <td>{p.primary_email}</td>
                <td>
                  {p.is_internal ? (
                    <span className="badge internal">internal</span>
                  ) : (
                    <span className="muted">external</span>
                  )}
                </td>
                <td>{p.occurrences}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>

      <div className="pager">
        <button
          onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          disabled={offset === 0 || loading}
        >
          ← Prev
        </button>
        <span>
          Showing {offset + 1}–
          {data ? Math.min(offset + PAGE_SIZE, data.total) : 0} of{" "}
          {data?.total ?? 0}
        </span>
        <button
          onClick={() => setOffset(offset + PAGE_SIZE)}
          disabled={
            loading || !data || offset + PAGE_SIZE >= data.total
          }
        >
          Next →
        </button>
      </div>
    </>
  );
}
