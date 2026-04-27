import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import type { AuditEvent, Page } from "../types";

const PAGE_SIZE = 100;

const KNOWN_EVENT_TYPES = [
  "import.run",
  "ingest.run",
  "extract.run",
  "detection.run",
  "resolve.run",
  "resolve.merge",
  "resolve.rename",
  "resolve.note",
  "redaction.propose",
  "redaction.create",
  "redaction.update",
  "redaction.delete",
  "export.run",
  "ai_qa.run",
  "ai_qa.dismiss",
  "ai_qa.promote",
];

export default function AuditPage() {
  const [data, setData] = useState<Page<AuditEvent> | null>(null);
  const [offset, setOffset] = useState(0);
  const [eventType, setEventType] = useState<string>("");
  const [actor, setActor] = useState("");
  const [origin, setOrigin] = useState<string>("");
  const [pendingActor, setPendingActor] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .listAudit({
        limit: PAGE_SIZE,
        offset,
        event_type: eventType || undefined,
        actor: actor || undefined,
        origin: origin || undefined,
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
  }, [offset, eventType, actor, origin]);

  return (
    <>
      <h2 style={{ marginTop: 0 }}>Audit log</h2>
      <p className="muted">
        Append-only record of every write to the system. Database triggers
        block any UPDATE or DELETE on this table — even direct SQL.
      </p>

      <div className="toolbar">
        <select
          value={eventType}
          onChange={(e) => {
            setOffset(0);
            setEventType(e.target.value);
          }}
        >
          <option value="">All event types</option>
          {KNOWN_EVENT_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <select
          value={origin}
          onChange={(e) => {
            setOffset(0);
            setOrigin(e.target.value);
          }}
        >
          <option value="">All origins</option>
          <option value="cli">cli</option>
          <option value="api">api</option>
          <option value="system">system</option>
        </select>
        <input
          type="text"
          placeholder="Filter by actor…"
          value={pendingActor}
          onChange={(e) => setPendingActor(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              setOffset(0);
              setActor(pendingActor.trim());
            }
          }}
        />
        <button
          onClick={() => {
            setOffset(0);
            setActor(pendingActor.trim());
          }}
        >
          Filter
        </button>
        {data ? (
          <span className="muted" style={{ marginLeft: "auto" }}>
            {data.total} events
          </span>
        ) : null}
      </div>

      {error ? <div className="error">{error}</div> : null}

      <table>
        <thead>
          <tr>
            <th style={{ width: 60 }}>ID</th>
            <th style={{ width: 200 }}>When</th>
            <th style={{ width: 180 }}>Event</th>
            <th style={{ width: 160 }}>Actor</th>
            <th style={{ width: 80 }}>Origin</th>
            <th style={{ width: 130 }}>Source</th>
            <th>Detail</th>
          </tr>
        </thead>
        <tbody>
          {loading && !data ? (
            <tr>
              <td colSpan={7} className="muted">
                Loading…
              </td>
            </tr>
          ) : data && data.items.length === 0 ? (
            <tr>
              <td colSpan={7} className="muted">
                No events match.
              </td>
            </tr>
          ) : (
            data?.items.map((e) => (
              <tr key={e.id}>
                <td>{e.id}</td>
                <td>{e.event_at.replace("T", " ").slice(0, 19)}</td>
                <td>
                  <code>{e.event_type}</code>
                </td>
                <td>{e.actor}</td>
                <td>
                  <span className="badge">{e.request_origin}</span>
                </td>
                <td>
                  {e.source_type ? (
                    <>
                      {e.source_type}
                      {e.source_id != null ? `#${e.source_id}` : ""}
                    </>
                  ) : (
                    <span className="muted">—</span>
                  )}
                </td>
                <td>
                  <PayloadSummary payload={e.payload} />
                </td>
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
          ← Newer
        </button>
        <span>
          {data?.total ? (
            <>
              Showing {offset + 1}–
              {Math.min(offset + PAGE_SIZE, data.total)} of {data.total}
            </>
          ) : null}
        </span>
        <button
          onClick={() => setOffset(offset + PAGE_SIZE)}
          disabled={
            loading || !data || offset + PAGE_SIZE >= data.total
          }
        >
          Older →
        </button>
      </div>
    </>
  );
}

function PayloadSummary({
  payload,
}: {
  payload: Record<string, unknown> | null;
}) {
  if (!payload) return <span className="muted">—</span>;
  // Show a couple of headline fields for the common event types; full
  // payload is one click away in <details>.
  const headline =
    pickHeadline(payload) || `${Object.keys(payload).length} fields`;
  return (
    <details>
      <summary style={{ cursor: "pointer", fontSize: 12 }}>{headline}</summary>
      <pre style={{ fontSize: 11, margin: "4px 0", whiteSpace: "pre-wrap" }}>
        {JSON.stringify(payload, null, 2)}
      </pre>
    </details>
  );
}

function pickHeadline(p: Record<string, unknown>): string | null {
  for (const k of [
    "emails_ingested",
    "detections_written",
    "proposed",
    "redactions_burned",
    "new_status",
    "redaction_id",
    "import_id",
    "filename",
    "provider",
    "qa_run_id",
  ]) {
    if (k in p && p[k] !== null && p[k] !== undefined) {
      return `${k}: ${String(p[k])}`;
    }
  }
  return null;
}
