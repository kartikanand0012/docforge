/** The DocForge API, as the review screen uses it. */

import type { Box } from "./geometry";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");

export type FieldStatus = "verified" | "confirmed" | "not_in_cited_blocks" | "no_citation";

export type FieldAssessment = {
  path: string;
  status: FieldStatus;
  needs_review: boolean;
  reasons: string[];
  boxes: Box[];
  found_in: string[];
};

export type RuleResult = {
  rule_id: string;
  severity: "error" | "warning";
  outcome: "passed" | "failed" | "not_evaluated";
  message: string;
  paths: string[];
};

export type Discrepancy = {
  code: string;
  severity: "error" | "warning";
  message: string;
  invoice_value: string | null;
  order_value: string | null;
};

export type CorrectionEntry = {
  path: string;
  old_text: string | null;
  new_text: string | null;
  reason: string;
  reviewer_name: string;
  created_at: string;
};

export type Signed = {
  outcome: "approved" | "rejected";
  meaning: string;
  reason: string;
  override_reason: string | null;
  reviewer_name: string;
  signed_at: string;
  record_sha256: string;
  draft: Record<string, unknown> | null;
};

export type ReviewDetail = {
  document_id: string;
  doc_type: string;
  filename: string;
  version_no: number;
  page_count: number;
  pages: { number: number; width: number; height: number }[];
  decision: "accept" | "review";
  blockers: string[];
  record: Record<string, unknown>;
  assessment: { fields: FieldAssessment[]; rules: RuleResult[]; reasons: string[] };
  editable_paths: string[];
  corrections: CorrectionEntry[];
  match_status: "match" | "mismatch" | "no_counterpart";
  discrepancies: Discrepancy[];
  counterpart_document_id: string | null;
  review: Signed | null;
  signature_valid: boolean | null;
};

export type QueueItem = {
  document_id: string;
  doc_type: string;
  filename: string;
  version_no: number;
  created_at: string;
  reasons: string[];
  match_status: string;
};

export type DocumentSummary = {
  document: { id: string; doc_type: string; filename: string; status: string; page_count: number | null };
  versions: { version_no: number; status: string; error: string | null }[];
};

export type Credentials = { email: string; pin: string };

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, { cache: "no-store", ...init });
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = "Some of what was entered is not valid.";
    } catch {
      /* not JSON: keep the generic message */
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  queue: () => call<QueueItem[]>("/v1/review/queue"),
  review: (id: string) => call<ReviewDetail>(`/v1/documents/${encodeURIComponent(id)}/review`),
  document: (id: string) => call<DocumentSummary>(`/v1/documents/${encodeURIComponent(id)}`),
  correct: (id: string, path: string, text: string | null, reason: string, who: Credentials) =>
    call<ReviewDetail>(`/v1/documents/${encodeURIComponent(id)}/corrections`, json({ path, text, reason, ...who })),
  sign: (
    id: string,
    body: { outcome: "approved" | "rejected"; meaning: string; reason: string; override_reason: string | null },
    who: Credentials,
  ) => call<Signed>(`/v1/documents/${encodeURIComponent(id)}/review`, json({ ...body, ...who })),
  upload: (file: File, docType: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("doc_type", docType);
    return call<{ document: { id: string }; created: boolean }>("/v1/documents", { method: "POST", body: form });
  },
  evals: () => call<Record<string, unknown>>("/v1/evals"),
  pageUrl: (id: string, page: number) => `${API_URL}/v1/documents/${encodeURIComponent(id)}/pages/${page}`,
};
