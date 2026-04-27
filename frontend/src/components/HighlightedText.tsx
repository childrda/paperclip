import { useEffect, useRef, useState } from "react";
import type { ExemptionCode, Redaction, RedactionStatus } from "../types";

interface Props {
  /** The canonical text to render. */
  text: string;
  /** Redactions to overlay; only those whose source matches are rendered. */
  redactions: Redaction[];
  /** Filter to one source_type within the text. */
  sourceType: string;
  /** Filter to one source_id (email id or attachment id). */
  sourceId: number;
  /** Exemption-code allowlist for the picker. */
  exemptionCodes: ExemptionCode[];
  /** Update / delete callbacks bubbled to the parent. */
  onPatch: (
    redactionId: number,
    payload: Partial<{
      status: RedactionStatus;
      exemption_code: string;
      reviewer_id: string;
      notes: string;
    }>,
  ) => Promise<void>;
}

interface Segment {
  start: number;
  end: number;
  redaction?: Redaction;
}

function buildSegments(text: string, redactions: Redaction[]): Segment[] {
  // Sort and clip overlapping redactions: same-source overlaps shouldn't
  // happen often (the unique index forbids identical spans), but defend
  // against them anyway by rendering the highest-id one on top.
  const sorted = [...redactions].sort(
    (a, b) => a.start_offset - b.start_offset || b.id - a.id,
  );

  const segments: Segment[] = [];
  let cursor = 0;
  for (const r of sorted) {
    const start = Math.max(r.start_offset, cursor);
    const end = Math.min(r.end_offset, text.length);
    if (start >= end) continue;
    if (start > cursor) {
      segments.push({ start: cursor, end: start });
    }
    segments.push({ start, end, redaction: r });
    cursor = end;
  }
  if (cursor < text.length) {
    segments.push({ start: cursor, end: text.length });
  }
  if (segments.length === 0) {
    segments.push({ start: 0, end: text.length });
  }
  return segments;
}

export function HighlightedText({
  text,
  redactions,
  sourceType,
  sourceId,
  exemptionCodes,
  onPatch,
}: Props) {
  const filtered = redactions.filter(
    (r) => r.source_type === sourceType && r.source_id === sourceId,
  );
  const segments = buildSegments(text, filtered);

  const [openId, setOpenId] = useState<number | null>(null);
  const [popoverPos, setPopoverPos] = useState<{ top: number; left: number } | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Click outside / Escape to dismiss the popover.
  useEffect(() => {
    if (openId === null) return;
    function handler(e: MouseEvent) {
      const target = e.target as HTMLElement | null;
      if (!target?.closest(".span-actions") && !target?.closest(".span-marker")) {
        setOpenId(null);
      }
    }
    function key(e: KeyboardEvent) {
      if (e.key === "Escape") setOpenId(null);
    }
    document.addEventListener("mousedown", handler);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("keydown", key);
    };
  }, [openId]);

  return (
    <div className="body-text" ref={containerRef}>
      {segments.map((seg, i) => {
        const slice = text.slice(seg.start, seg.end);
        if (!seg.redaction) {
          return <span key={i}>{slice}</span>;
        }
        const r = seg.redaction;
        return (
          <span
            key={`r-${r.id}-${i}`}
            className={`span-marker ${r.status}`}
            title={`${r.exemption_code} · ${r.status} · id=${r.id}`}
            onClick={(e) => {
              e.stopPropagation();
              const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
              setPopoverPos({
                top: rect.bottom + window.scrollY + 4,
                left: rect.left + window.scrollX,
              });
              setOpenId(r.id);
            }}
          >
            {slice || "·"}
          </span>
        );
      })}

      {openId !== null && popoverPos ? (
        <SpanActions
          redaction={filtered.find((r) => r.id === openId)!}
          exemptionCodes={exemptionCodes}
          position={popoverPos}
          onPatch={async (payload) => {
            await onPatch(openId, payload);
            setOpenId(null);
          }}
          onCancel={() => setOpenId(null)}
        />
      ) : null}
    </div>
  );
}

interface ActionsProps {
  redaction: Redaction;
  exemptionCodes: ExemptionCode[];
  position: { top: number; left: number };
  onPatch: (
    payload: Partial<{
      status: RedactionStatus;
      exemption_code: string;
      reviewer_id: string;
      notes: string;
    }>,
  ) => Promise<void>;
  onCancel: () => void;
}

function SpanActions({
  redaction,
  exemptionCodes,
  position,
  onPatch,
  onCancel,
}: ActionsProps) {
  const [exemption, setExemption] = useState(redaction.exemption_code);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(redaction.notes ?? "");

  // The reviewer's identity comes from the authenticated session — the
  // backend audit log records user_id directly. We still pass
  // ``reviewer_id`` so the redaction row carries a human-readable name
  // when the auditor reads the table later; pull it from the cookie-
  // backed session via /auth/me on demand.
  async function fetchReviewerName(): Promise<string | null> {
    try {
      const r = await fetch("/api/v1/auth/me", { credentials: "include" });
      if (!r.ok) return null;
      const data = (await r.json()) as { username?: string; display_name?: string };
      return data.display_name || data.username || null;
    } catch {
      return null;
    }
  }

  async function transition(status: RedactionStatus) {
    const who = await fetchReviewerName();
    if (!who) {
      alert("Your session has expired. Please sign in again.");
      return;
    }
    setBusy(true);
    try {
      await onPatch({
        status,
        reviewer_id: who,
        exemption_code: exemption,
        notes: note || undefined as unknown as string,
      });
    } finally {
      setBusy(false);
    }
  }

  async function changeExemption() {
    if (exemption === redaction.exemption_code) return;
    setBusy(true);
    try {
      await onPatch({ exemption_code: exemption });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div
      className="span-actions"
      style={{ top: position.top, left: position.left }}
      onClick={(e) => e.stopPropagation()}
    >
      <h4>Redaction #{redaction.id}</h4>
      <p>
        Status: <span className={`badge ${redaction.status}`}>{redaction.status}</span>
        {" · "}origin: {redaction.origin}
      </p>
      <p>
        <label>
          Exemption:{" "}
          <select
            value={exemption}
            onChange={(e) => setExemption(e.target.value)}
            onBlur={changeExemption}
            disabled={busy}
          >
            {exemptionCodes.map((c) => (
              <option key={c.code} value={c.code}>
                {c.code}
              </option>
            ))}
            {!exemptionCodes.find((c) => c.code === exemption) ? (
              <option value={exemption}>{exemption}</option>
            ) : null}
          </select>
        </label>
      </p>
      <p>
        <label>
          Note:{" "}
          <input
            type="text"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            disabled={busy}
            style={{ width: "100%" }}
          />
        </label>
      </p>
      <div className="row">
        <button
          className="accept"
          disabled={busy || redaction.status === "accepted"}
          onClick={() => transition("accepted")}
        >
          Accept
        </button>
        <button
          className="reject"
          disabled={busy || redaction.status === "rejected"}
          onClick={() => transition("rejected")}
        >
          Reject
        </button>
        <button onClick={onCancel} disabled={busy}>
          Close
        </button>
      </div>
    </div>
  );
}
