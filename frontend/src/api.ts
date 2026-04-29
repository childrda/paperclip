// Thin fetch wrapper. The Vite dev server proxies /api, /health, /stats
// to the FastAPI backend, so we always speak relative URLs here.

import type {
  AiFlag,
  AiFlagStatus,
  AuditEvent,
  Case,
  CaseDetail,
  CaseStatus,
  CurrentUser,
  EmailDetail,
  EmailSummary,
  ExemptionCode,
  ExportListItem,
  ExportManifest,
  ImportSubmitted,
  Page,
  PersonSummary,
  PipelineJob,
  Redaction,
  RedactionStatus,
  SearchHit,
  Stats,
} from "./types";

class ApiError extends Error {
  constructor(public status: number, public body: string) {
    super(`HTTP ${status}: ${body.slice(0, 200)}`);
  }
}

function reviewerHeader(): Record<string, string> {
  // Auth identity flows through the HttpOnly session cookie now; this
  // helper survives only as a hook for legacy callers that don't have
  // a session and want to attribute via X-FOIA-Reviewer (e.g. tests).
  return {};
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
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
    case_id?: number;
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

  // ---- Auth

  async login(username: string, password: string): Promise<CurrentUser> {
    return request("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  },

  async logout(): Promise<void> {
    await request("/api/v1/auth/logout", { method: "POST" });
  },

  async getMe(): Promise<CurrentUser | null> {
    try {
      return await request<CurrentUser>("/api/v1/auth/me");
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) return null;
      throw e;
    }
  },

  // ---- Cases

  async listCases(args: {
    status?: CaseStatus;
    limit?: number;
    offset?: number;
  } = {}): Promise<{ items: Case[]; total: number; limit: number; offset: number }> {
    return request(`/api/v1/cases${qs(args as Record<string, unknown>)}`);
  },

  async getCase(id: number): Promise<CaseDetail> {
    return request(`/api/v1/cases/${id}`);
  },

  async setCaseStatus(id: number, status: CaseStatus): Promise<Case> {
    return request(`/api/v1/cases/${id}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    });
  },

  async proposeCaseRedactions(id: number): Promise<{
    detections_seen: number;
    proposed: number;
    skipped_existing: number;
    skipped_no_exemption: number;
    skipped_invalid: number;
    by_entity: Record<string, number>;
  }> {
    return request(`/api/v1/cases/${id}/propose-redactions`, {
      method: "POST",
    });
  },

  // ---- Imports (background job, SSE-streamed)

  async submitImport(args: {
    file: File;
    name: string;
    bates_prefix?: string;
    label?: string;
    propose_redactions?: boolean;
  }): Promise<ImportSubmitted> {
    const fd = new FormData();
    fd.append("file", args.file);
    fd.append("name", args.name);
    if (args.bates_prefix) fd.append("bates_prefix", args.bates_prefix);
    if (args.label) fd.append("label", args.label);
    fd.append(
      "propose_redactions",
      args.propose_redactions === false ? "false" : "true",
    );
    const res = await fetch("/api/v1/imports", {
      method: "POST",
      body: fd,
      headers: reviewerHeader(),
      credentials: "include",
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new ApiError(res.status, text);
    }
    return (await res.json()) as ImportSubmitted;
  },

  async getImport(jobId: number): Promise<{ job: PipelineJob; events: unknown[] }> {
    return request(`/api/v1/imports/${jobId}`);
  },

  async listImports(): Promise<PipelineJob[]> {
    return request("/api/v1/imports");
  },

  async retryImport(jobId: number): Promise<{ job_id: number; status: string }> {
    return request(`/api/v1/imports/${jobId}/retry`, { method: "POST" });
  },

  /** Returns an EventSource subscribed to the job's progress stream. */
  importEventSource(jobId: number): EventSource {
    // Vite proxies /api → backend; SSE works through the proxy.
    return new EventSource(`/api/v1/imports/${jobId}/events`, {
      withCredentials: true,
    });
  },

  async search(q: string, scope?: "emails" | "attachments"): Promise<Page<SearchHit>> {
    return request(`/api/v1/search${qs({ q, scope, limit: 50 })}`);
  },

  async listPersons(args: {
    is_internal?: boolean;
    name_contains?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<Page<PersonSummary>> {
    return request(`/api/v1/persons${qs(args as Record<string, unknown>)}`);
  },

  async listExports(): Promise<ExportListItem[]> {
    return request("/api/v1/exports");
  },

  async listAudit(args: {
    event_type?: string;
    actor?: string;
    origin?: string;
    limit?: number;
    offset?: number;
  } = {}): Promise<Page<AuditEvent>> {
    return request(`/api/v1/audit${qs(args as Record<string, unknown>)}`);
  },
};

// Re-export so the multipart upload can use the same header helper.
export { reviewerHeader };

export { ApiError };
