import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError, api } from "../api";
import { HighlightedText } from "../components/HighlightedText";
import type {
  AiFlag,
  EmailDetail,
  ExemptionCode,
  Redaction,
  RedactionStatus,
} from "../types";

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export default function EmailDetailPage() {
  const { id } = useParams<{ id: string }>();
  const emailId = Number(id);

  const [email, setEmail] = useState<EmailDetail | null>(null);
  const [redactions, setRedactions] = useState<Redaction[]>([]);
  const [exemptions, setExemptions] = useState<ExemptionCode[]>([]);
  const [aiFlags, setAiFlags] = useState<AiFlag[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [detail, reds, codes, flags] = await Promise.all([
        api.getEmail(emailId),
        api.getEmailRedactions(emailId),
        api.listExemptionCodes(),
        api.listAiFlags({ source_id: emailId, limit: 200 }),
      ]);
      setEmail(detail);
      setRedactions(reds);
      setExemptions(codes);
      // Filter to flags whose source ties to this email — the backend
      // only filters by source_id, not source_type, so attachment-text
      // flags with the same numeric id would slip in otherwise.
      setAiFlags(
        flags.items.filter((f) => f.source_type.startsWith("email_")),
      );
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  }, [emailId]);

  useEffect(() => {
    if (Number.isNaN(emailId)) {
      setError("invalid email id");
      setLoading(false);
      return;
    }
    reload();
  }, [emailId, reload]);

  async function handlePatch(
    redactionId: number,
    payload: Partial<{
      status: RedactionStatus;
      exemption_code: string;
      reviewer_id: string;
      notes: string;
    }>,
  ) {
    try {
      const updated = await api.patchRedaction(redactionId, payload);
      setRedactions((rs) =>
        rs.map((r) => (r.id === redactionId ? updated : r)),
      );
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`Failed to update redaction: ${msg}`);
    }
  }

  async function handleDismissFlag(flagId: number) {
    try {
      const updated = await api.dismissAiFlag(flagId);
      setAiFlags((fs) => fs.map((f) => (f.id === flagId ? updated : f)));
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`Failed to dismiss flag: ${msg}`);
    }
  }

  async function handlePromoteFlag(flagId: number) {
    try {
      const result = await api.promoteAiFlag(flagId);
      setAiFlags((fs) => fs.map((f) => (f.id === flagId ? result.flag : f)));
      // Pull the new proposed redaction into the local list so the
      // overlay updates without a full reload.
      setRedactions((rs) => [...rs, result.redaction]);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`Failed to promote flag: ${msg}`);
    }
  }

  const [aiScanBusy, setAiScanBusy] = useState(false);
  async function runAiScan() {
    if (aiScanBusy) return;
    setAiScanBusy(true);
    try {
      const r = await api.runAiQa({ email_id: emailId });
      // After scan completes, reload AI flags for this email.
      const flags = await api.listAiFlags({ source_id: emailId, limit: 200 });
      setAiFlags(flags.items.filter((f) => f.source_type.startsWith("email_")));
      const totalNew = r.flags_written ?? 0;
      const skipped = r.flags_skipped_existing ?? 0;
      const noun = totalNew === 1 ? "new flag" : "new flags";
      alert(
        `AI scan complete: ${totalNew} ${noun}` +
          (skipped ? ` (${skipped} already existed).` : "."),
      );
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      alert(`AI scan failed: ${msg}`);
    } finally {
      setAiScanBusy(false);
    }
  }

  if (loading) return <p className="muted">Loading…</p>;
  if (error) return <div className="error">{error}</div>;
  if (!email) return <p className="muted">Email not found.</p>;

  const counts = redactions.reduce(
    (acc, r) => {
      acc[r.status] = (acc[r.status] ?? 0) + 1;
      return acc;
    },
    { proposed: 0, accepted: 0, rejected: 0 } as Record<RedactionStatus, number>,
  );

  return (
    <>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          marginBottom: 8,
        }}
      >
        <Link to="/emails">← Back to list</Link>
        <span style={{ marginLeft: "auto" }}>
          <button onClick={runAiScan} disabled={aiScanBusy}>
            {aiScanBusy ? "Scanning…" : "Run AI scan on this email"}
          </button>
        </span>
      </div>

      <div className="detail-section">
        <h2>Headers</h2>
        <dl className="headers-grid">
          <dt>From</dt>
          <dd>{email.from_addr ?? <span className="muted">—</span>}</dd>
          <dt>To</dt>
          <dd>{email.to_addrs.join(", ") || <span className="muted">—</span>}</dd>
          {email.cc_addrs.length > 0 ? (
            <>
              <dt>Cc</dt>
              <dd>{email.cc_addrs.join(", ")}</dd>
            </>
          ) : null}
          <dt>Date</dt>
          <dd>{email.date_sent ?? email.date_raw ?? <span className="muted">—</span>}</dd>
          <dt>Source</dt>
          <dd>
            {email.mbox_source} #{email.mbox_index}
          </dd>
        </dl>
      </div>

      <div className="detail-section">
        <h2>
          Subject{" "}
          <small className="muted" style={{ fontWeight: 400 }}>
            (click any highlighted span to accept / reject)
          </small>
        </h2>
        <Legend />
        <HighlightedText
          text={email.subject ?? ""}
          redactions={redactions}
          sourceType="email_subject"
          sourceId={email.id}
          exemptionCodes={exemptions}
          onPatch={handlePatch}
        />
      </div>

      <div className="detail-section">
        <h2>
          Body{" "}
          <small className="muted" style={{ fontWeight: 400 }}>
            ({counts.proposed} proposed · {counts.accepted} accepted · {counts.rejected} rejected)
          </small>
        </h2>
        <Legend />
        <HighlightedText
          text={email.body_text ?? ""}
          redactions={redactions}
          sourceType="email_body_text"
          sourceId={email.id}
          exemptionCodes={exemptions}
          onPatch={handlePatch}
        />
      </div>

      {email.body_html_sanitized ? (
        <div className="detail-section">
          <h2>HTML body (sanitized text)</h2>
          <HighlightedText
            text={email.body_html_sanitized}
            redactions={redactions}
            sourceType="email_body_html"
            sourceId={email.id}
            exemptionCodes={exemptions}
            onPatch={handlePatch}
          />
        </div>
      ) : null}

      {aiFlags.length > 0 ? (
        <div className="detail-section">
          <h2>
            AI risk flags{" "}
            <small className="muted" style={{ fontWeight: 400 }}>
              ({aiFlags.filter((f) => f.review_status === "open").length} open;
              advisory only — promote to a redaction to act)
            </small>
          </h2>
          <table>
            <thead>
              <tr>
                <th style={{ width: 60 }}>ID</th>
                <th style={{ width: 130 }}>Status</th>
                <th style={{ width: 140 }}>Entity</th>
                <th style={{ width: 80 }}>Score</th>
                <th>Match</th>
                <th>Rationale</th>
                <th style={{ width: 220 }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {aiFlags.map((f) => (
                <tr key={f.id}>
                  <td>{f.id}</td>
                  <td>
                    <span className={`badge ${f.review_status === "promoted" ? "accepted" : f.review_status === "dismissed" ? "rejected" : "proposed"}`}>
                      {f.review_status}
                    </span>
                  </td>
                  <td>
                    {f.entity_type}
                    {f.suggested_exemption ? (
                      <span className="muted"> · {f.suggested_exemption}</span>
                    ) : null}
                  </td>
                  <td>{f.confidence.toFixed(2)}</td>
                  <td>
                    <code>{f.matched_text}</code>
                  </td>
                  <td className="muted">{f.rationale ?? ""}</td>
                  <td>
                    {f.review_status === "open" ? (
                      <>
                        <button
                          onClick={() => handlePromoteFlag(f.id)}
                          style={{ marginRight: 6 }}
                          title="Create a *proposed* redaction from this flag (still requires Accept)"
                        >
                          Promote
                        </button>
                        <button onClick={() => handleDismissFlag(f.id)}>
                          Dismiss
                        </button>
                      </>
                    ) : (
                      <span className="muted">
                        {f.review_status} by {f.review_actor ?? "unknown"}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {email.attachments.length > 0 ? (
        <div className="detail-section">
          <h2>Attachments</h2>
          <table>
            <thead>
              <tr>
                <th style={{ width: 60 }}>ID</th>
                <th>Filename</th>
                <th style={{ width: 200 }}>Type</th>
                <th style={{ width: 100 }}>Size</th>
                <th style={{ width: 120 }}>Extraction</th>
              </tr>
            </thead>
            <tbody>
              {email.attachments.map((a) => (
                <tr key={a.id}>
                  <td>{a.id}</td>
                  <td>
                    <a
                      href={`/api/v1/attachments/${a.id}/download`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {a.filename ?? `attachment-${a.id}`}
                    </a>
                  </td>
                  <td>{a.content_type ?? <span className="muted">—</span>}</td>
                  <td>{fmtBytes(a.size_bytes)}</td>
                  <td>{a.extraction_status ?? <span className="muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </>
  );
}

function Legend() {
  return (
    <div className="legend">
      <span>
        <span className="swatch" style={{ background: "#fff3a3", outline: "1px solid #d4ad15" }} />
        proposed
      </span>
      <span>
        <span className="swatch" style={{ background: "#111" }} /> accepted
      </span>
      <span>
        <span className="swatch" style={{ background: "transparent", outline: "1px dashed #999" }} />
        rejected
      </span>
    </div>
  );
}
