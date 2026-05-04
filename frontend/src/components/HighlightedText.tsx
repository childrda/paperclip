import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
  /** Remove a redaction entirely (used when an auto-proposal was wrong). */
  onDelete?: (redactionId: number) => Promise<void>;
  /** Create a manual redaction over an arbitrary text range. */
  onCreate?: (range: {
    start: number;
    end: number;
    exemption_code: string;
  }) => Promise<void>;
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

/**
 * Compute the offset of a (node, offsetInNode) pair within the
 * concatenated text content of ``container``. Walks every text node
 * inside the container in DOM order and counts characters until the
 * target node is hit.
 *
 * Returns -1 if the target isn't a descendant of the container — in
 * practice that means the user's selection straddled outside our
 * highlighted block, and we should ignore it.
 */
function getTextOffset(
  container: Node,
  target: Node,
  offsetInTarget: number,
): number {
  if (target === container) {
    // Selection anchored on the container itself: count all text up to
    // the Nth child.
    let total = 0;
    for (let i = 0; i < offsetInTarget && i < container.childNodes.length; i++) {
      total += (container.childNodes[i].textContent ?? "").length;
    }
    return total;
  }
  let offset = 0;
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  let node: Node | null;
  while ((node = walker.nextNode())) {
    if (node === target) return offset + offsetInTarget;
    offset += (node as Text).data.length;
  }
  return -1;
}

export function HighlightedText({
  text,
  redactions,
  sourceType,
  sourceId,
  exemptionCodes,
  onPatch,
  onDelete,
  onCreate,
}: Props) {
  const filtered = redactions.filter(
    (r) => r.source_type === sourceType && r.source_id === sourceId,
  );
  const segments = buildSegments(text, filtered);

  const [openId, setOpenId] = useState<number | null>(null);
  const [popoverPos, setPopoverPos] = useState<{ top: number; left: number } | null>(null);
  const [pendingSelection, setPendingSelection] = useState<{
    start: number;
    end: number;
    top: number;
    left: number;
  } | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  // Click outside / Escape to dismiss the popover.
  useEffect(() => {
    if (openId === null && pendingSelection === null) return;
    function handler(e: MouseEvent) {
      const target = e.target as HTMLElement | null;
      if (
        !target?.closest(".span-actions") &&
        !target?.closest(".span-marker") &&
        !target?.closest(".selection-toolbar")
      ) {
        setOpenId(null);
        setPendingSelection(null);
      }
    }
    function key(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setOpenId(null);
        setPendingSelection(null);
      }
    }
    document.addEventListener("mousedown", handler);
    document.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", handler);
      document.removeEventListener("keydown", key);
    };
  }, [openId, pendingSelection]);

  function handleMouseUp() {
    if (!onCreate) return;
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) {
      return;
    }
    const range = sel.getRangeAt(0);
    const container = containerRef.current;
    if (!container) return;
    if (!container.contains(range.commonAncestorContainer)) return;
    const a = getTextOffset(container, range.startContainer, range.startOffset);
    const b = getTextOffset(container, range.endContainer, range.endOffset);
    if (a < 0 || b < 0 || a === b) return;
    const start = Math.min(a, b);
    const end = Math.max(a, b);
    const rect = range.getBoundingClientRect();
    setPendingSelection({
      start,
      end,
      // ``position: fixed`` on the toolbar — viewport coords, NO scroll
      // offset.
      top: rect.bottom + 4,
      left: rect.left,
    });
    // Don't close the existing span popover if one is open; the user
    // might have made a selection inside the popover input.
  }

  return (
    <div
      className="body-text"
      ref={containerRef}
      onMouseUp={handleMouseUp}
    >
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
              // ``position: fixed`` — viewport coords, no scroll offset.
              setPopoverPos({
                top: rect.bottom + 4,
                left: rect.left,
              });
              setOpenId(r.id);
              setPendingSelection(null);
            }}
          >
            {slice || "·"}
          </span>
        );
      })}

      {openId !== null && popoverPos
        ? createPortal(
            <SpanActions
              redaction={filtered.find((r) => r.id === openId)!}
              exemptionCodes={exemptionCodes}
              position={popoverPos}
              onPatch={async (payload) => {
                await onPatch(openId, payload);
                setOpenId(null);
              }}
              onDelete={
                onDelete
                  ? async () => {
                      await onDelete(openId);
                      setOpenId(null);
                    }
                  : undefined
              }
              onCancel={() => setOpenId(null)}
            />,
            document.body,
          )
        : null}

      {pendingSelection && onCreate
        ? createPortal(
            <SelectionToolbar
              start={pendingSelection.start}
              end={pendingSelection.end}
              position={{
                top: pendingSelection.top,
                left: pendingSelection.left,
              }}
              exemptionCodes={exemptionCodes}
              previewText={text.slice(
                pendingSelection.start,
                pendingSelection.end,
              )}
              onCreate={async (payload) => {
                await onCreate(payload);
                setPendingSelection(null);
                window.getSelection()?.removeAllRanges();
              }}
              onCancel={() => setPendingSelection(null)}
            />,
            document.body,
          )
        : null}
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
  onDelete?: () => Promise<void>;
  onCancel: () => void;
}

function SpanActions({
  redaction,
  exemptionCodes,
  position,
  onPatch,
  onDelete,
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

  async function handleDelete() {
    if (!onDelete) return;
    if (!confirm(
      `Delete redaction #${redaction.id} entirely? This is irreversible.`,
    )) {
      return;
    }
    setBusy(true);
    try {
      await onDelete();
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
        {onDelete ? (
          <button
            disabled={busy}
            onClick={handleDelete}
            title="Remove this redaction entirely (e.g. an auto-proposal that's wrong)"
          >
            Delete
          </button>
        ) : null}
        <button onClick={onCancel} disabled={busy}>
          Close
        </button>
      </div>
    </div>
  );
}

interface SelectionToolbarProps {
  start: number;
  end: number;
  position: { top: number; left: number };
  exemptionCodes: ExemptionCode[];
  previewText: string;
  onCreate: (payload: {
    start: number;
    end: number;
    exemption_code: string;
  }) => Promise<void>;
  onCancel: () => void;
}

function SelectionToolbar({
  start,
  end,
  position,
  exemptionCodes,
  previewText,
  onCreate,
  onCancel,
}: SelectionToolbarProps) {
  const [exemption, setExemption] = useState(
    exemptionCodes[0]?.code ?? "FERPA",
  );
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    try {
      await onCreate({ start, end, exemption_code: exemption });
    } finally {
      setBusy(false);
    }
  }

  const preview =
    previewText.length > 60
      ? previewText.slice(0, 57) + "…"
      : previewText;

  return (
    <div
      className="span-actions selection-toolbar"
      style={{ top: position.top, left: position.left }}
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
    >
      <h4>Redact selection</h4>
      <p className="muted small">
        <code>{preview || "(empty)"}</code> ({end - start} chars)
      </p>
      <p>
        <label>
          Exemption:{" "}
          <select
            value={exemption}
            onChange={(e) => setExemption(e.target.value)}
            disabled={busy}
          >
            {exemptionCodes.map((c) => (
              <option key={c.code} value={c.code}>
                {c.code}
              </option>
            ))}
          </select>
        </label>
      </p>
      <div className="row">
        <button
          className="accept"
          disabled={busy || end - start <= 0}
          onClick={submit}
        >
          {busy ? "Adding…" : "Add as proposed"}
        </button>
        <button onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </div>
  );
}
