// Thin fetch wrapper. The Vite dev server proxies /api, /health, /stats
// to the FastAPI backend, so we always speak relative URLs here.

import type {
  AiFlag,
  AiFlagStatus,
  EmailDetail,
  EmailSummary,
  ExemptionCode,
  ExportManifest,
  Page,
  Redaction,
  RedactionStatus,
  Stats,
} from "./types";

class ApiError extends Error {
  constructor(public status: number, public body: string) {
    super(`HTTP ${status}: ${body.slice(0, 200)}`);
  }
}

function reviewerHeader(): Record<string, string> {
  // Pull the reviewer name set in the Phase 7 header input. Sent on every
  // request so the backend audit log can attribute reads as well as writes.
  const v = (localStorage.getItem("foia.reviewer") ?? "").trim();
  return v ? { "X-FOIA-Reviewer": v } : {};
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...reviewerHeader(),
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new ApiError(res.status, text);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function qs(params: Record<string, unknown>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export const api = {
  async getStats(): Promise<Stats> {
    return request("/stats");
  },

  async listEmails(args: {
    limit?: number;
    offset?: number;
    subject_contains?: string;
    from_contains?: string;
    has_pii?: boolean;
  }): Promise<Page<EmailSummary>> {
    return request(`/api/v1/emails${qs(args)}`);
  },

  async getEmail(id: number): Promise<EmailDetail> {
    return request(`/api/v1/emails/${id}`);
  },

  async getEmailRedactions(id: number): Promise<Redaction[]> {
    return request(`/api/v1/emails/${id}/redactions`);
  },

  async patchRedaction(
    id: number,
    payload: Partial<{
      status: RedactionStatus;
      reviewer_id: string;
      exemption_code: string;
      notes: string;
    }>,
  ): Promise<Redaction> {
    return request(`/api/v1/redactions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  async listExemptionCodes(): Promise<ExemptionCode[]> {
    return request("/api/v1/exemption-codes");
  },

  async createExport(args: {
    email_ids?: number[];
    include_attachments?: boolean;
  } = {}): Promise<ExportManifest> {
    return request("/api/v1/exports", {
      method: "POST",
      body: JSON.stringify(args),
    });
  },

  async listAiFlags(args: {
    status?: AiFlagStatus;
    source_id?: number;
    source_type?: string;
    limit?: number;
  } = {}): Promise<Page<AiFlag>> {
    return request(`/api/v1/ai-flags${qs(args as Record<string, unknown>)}`);
  },

  async runAiQa(args: {
    email_id?: number;
    attachment_id?: number;
    provider?: string;
    model?: string;
  } = {}): Promise<{ qa_run_id: string; flags_written: number; sources_scanned: number; sources_failed: number; flags_skipped_existing: number; by_entity: Record<string, number> }> {
    return request("/api/v1/ai-flags/run", {
      method: "POST",
      body: JSON.stringify(args),
    });
  },

  async dismissAiFlag(id: number, note?: string): Promise<AiFlag> {
    return request(`/api/v1/ai-flags/${id}/dismiss`, {
      method: "PATCH",
      body: JSON.stringify({ note: note ?? null }),
    });
  },

  async promoteAiFlag(
    id: number,
    args: { exemption_code?: string; note?: string } = {},
  ): Promise<{ flag: AiFlag; redaction: Redaction }> {
    return request(`/api/v1/ai-flags/${id}/promote`, {
      method: "POST",
      body: JSON.stringify(args),
    });
  },
};

export { ApiError };
