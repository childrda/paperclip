import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../api";
import type { ImportSubmitted, PipelineEvent } from "../types";

const STAGES: Array<[string, string]> = [
  ["ingest", "Reading mailbox"],
  ["extract", "Extracting attachments"],
  ["detect", "Scanning for PII"],
  ["resolve", "Building person index"],
  ["propose", "Auto-proposing redactions"],
];

interface StageState {
  status: "pending" | "running" | "done" | "failed";
  message?: string;
}

const initialStages = (): Record<string, StageState> =>
  Object.fromEntries(STAGES.map(([k]) => [k, { status: "pending" }]));

export default function NewCasePage() {
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [batesPrefix, setBatesPrefix] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submitted, setSubmitted] = useState<ImportSubmitted | null>(null);
  const [stages, setStages] = useState<Record<string, StageState>>(initialStages);
  const [terminalStatus, setTerminalStatus] =
    useState<"succeeded" | "failed" | null>(null);
  const [eventLog, setEventLog] = useState<PipelineEvent[]>([]);
  const esRef = useRef<EventSource | null>(null);

  // Clean up EventSource on unmount.
  useEffect(() => {
    return () => esRef.current?.close();
  }, []);

  // When the job hits a terminal state, navigate to the case.
  useEffect(() => {
    if (terminalStatus === "succeeded" && submitted) {
      const t = setTimeout(() => {
        navigate(`/cases/${submitted.case_id}`);
      }, 800);
      return () => clearTimeout(t);
    }
  }, [terminalStatus, submitted, navigate]);

  async function submit() {
    if (!file || !name.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    setStages(initialStages());
    setEventLog([]);
    setTerminalStatus(null);
    try {
      const result = await api.submitImport({
        file,
        name: name.trim(),
        bates_prefix: batesPrefix.trim() || undefined,
      });
      setSubmitted(result);
      subscribe(result.job_id);
    } catch (e) {
      const msg = e instanceof ApiError ? e.message : String(e);
      setError(msg);
      setSubmitting(false);
    }
  }

  function subscribe(jobId: number) {
    const es = api.importEventSource(jobId);
    esRef.current = es;
    const handle = (kind: PipelineEvent["kind"]) => (e: MessageEvent) => {
      try {
        const ev = JSON.parse(e.data) as PipelineEvent;
        setEventLog((log) => [...log, ev]);
        setStages((prev) => {
          const next = { ...prev };
          if (ev.stage in next) {
            next[ev.stage] = {
              status:
                kind === "started"
                  ? "running"
                  : kind === "failed"
                  ? "failed"
                  : "done",
              message: ev.message ?? undefined,
            };
          }
          return next;
        });
      } catch {
        // ignore malformed event lines
      }
    };
    es.addEventListener("started", handle("started"));
    es.addEventListener("progress", handle("progress"));
    es.addEventListener("finished", handle("finished"));
    es.addEventListener("failed", handle("failed"));
    es.addEventListener("done", () => {
      // Backend sends a final "done" sentinel; pull the job to read
      // its terminal status.
      es.close();
      esRef.current = null;
      api
        .getImport(jobId)
        .then((d) => {
          setTerminalStatus(
            d.job.status === "succeeded" ? "succeeded" : "failed",
          );
          if (d.job.status === "failed") {
            setError(d.job.error_message ?? "Pipeline failed.");
          }
        })
        .catch(() => undefined);
    });
    es.onerror = () => {
      // Network blip or job stream ended. Try to read terminal state.
      api.getImport(jobId).then((d) => {
        if (d.job.status === "failed") {
          setTerminalStatus("failed");
          setError(d.job.error_message ?? "Pipeline failed.");
        } else if (d.job.status === "succeeded") {
          setTerminalStatus("succeeded");
        }
      });
    };
  }

  async function retry() {
    if (!submitted) return;
    setError(null);
    setStages(initialStages());
    setEventLog([]);
    setTerminalStatus(null);
    try {
      await api.retryImport(submitted.job_id);
      subscribe(submitted.job_id);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  return (
    <>
      <div className="page-toolbar">
        <h2>New case</h2>
      </div>

      {!submitted ? (
        <div className="card form-card">
          <p className="muted">
            Drop a <code>.mbox</code> mailbox export. The server will run
            the full pipeline (ingest → extract → detect → resolve →
            propose) in the background and you'll watch live progress.
          </p>
          <label>
            Case name
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. 2024-FOIA-017 — Smith v. District"
              disabled={submitting}
            />
          </label>
          <label>
            Bates prefix
            <input
              type="text"
              value={batesPrefix}
              onChange={(e) => setBatesPrefix(e.target.value.toUpperCase())}
              placeholder="defaults to district setting"
              disabled={submitting}
            />
          </label>
          <label>
            Mailbox file
            <input
              type="file"
              accept=".mbox,application/mbox,application/octet-stream"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              disabled={submitting}
            />
            {file ? (
              <span className="muted small">
                {file.name} · {(file.size / 1024).toFixed(1)} KB
              </span>
            ) : null}
          </label>

          {error ? <div className="error">{error}</div> : null}

          <div className="form-actions">
            <button
              className="primary"
              disabled={!file || !name.trim() || submitting}
              onClick={submit}
            >
              {submitting ? "Uploading…" : "Start case"}
            </button>
          </div>
        </div>
      ) : (
        <div className="card progress-card">
          <h3>{submitted.case_name}</h3>
          <p className="muted">
            Bates prefix <code>{submitted.bates_prefix}</code> · file{" "}
            <code>{submitted.filename}</code>
          </p>
          <ol className="stage-list">
            {STAGES.map(([key, label]) => {
              const s = stages[key];
              return (
                <li key={key} className={`stage stage-${s.status}`}>
                  <span className="stage-icon">
                    {s.status === "done"
                      ? "✓"
                      : s.status === "running"
                      ? "…"
                      : s.status === "failed"
                      ? "!"
                      : "○"}
                  </span>
                  <span className="stage-label">{label}</span>
                  {s.message ? (
                    <span className="stage-msg muted">{s.message}</span>
                  ) : null}
                </li>
              );
            })}
          </ol>

          {error ? <div className="error">{error}</div> : null}

          {terminalStatus === "failed" ? (
            <div className="form-actions">
              <button onClick={retry}>Retry from failed stage</button>
              <button onClick={() => navigate("/cases")}>Back to cases</button>
            </div>
          ) : null}
          {terminalStatus === "succeeded" ? (
            <p className="muted">All stages complete — opening case…</p>
          ) : null}

          <details style={{ marginTop: 12 }}>
            <summary>Raw event log</summary>
            <pre className="event-log">
              {eventLog
                .map(
                  (e) =>
                    `[${e.event_at.slice(11, 19)}] ${e.stage} ${e.kind}` +
                    (e.message ? ` — ${e.message}` : ""),
                )
                .join("\n")}
            </pre>
          </details>
        </div>
      )}
    </>
  );
}
