/** The DocForge API, as the review screen uses it. */

import { readEvents, type Answer, type ChatStatus, type Citation } from "./chat";
import type { Box } from "./geometry";

/** Every call goes to this site's own `/api/v1/...`, which adds the session token server-side. */
import type { Step } from "@/lib/stages";

export const API_URL = "/api";

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
  record_sha256: string;
  meanings: { approved: string; rejected: string };
  superseded: boolean;
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
  document: {
    id: string;
    doc_type: string;
    media_type: string;
    filename: string;
    status: string;
    stage: string;
    ready_for_chat: boolean;
    page_count: number | null;
  };
  versions: { version_no: number; status: string; error: string | null }[];
};

export type DocumentRow = {
  id: string;
  doc_type: string;
  media_type: string;
  filename: string;
  status: string;
  stage: string;
  ready_for_chat: boolean;
  page_count: number | null;
  created_at: string;
};

export type ChatScope = { document_id?: string; collection_id?: string; conversation_id?: string };

export type KnowledgeBase = { id: string; name: string; description: string; documents: number; created_by: string; created_at: string };

export type KnowledgeBaseMember = { id: string; filename: string; doc_type: string; stage: string; added_at: string };

export type UnansweredReport = {
  questions: { message_id: string; question: string; reason: string; missing: string; documents: string[]; owner: string; conversation_id: string; created_at: string }[];
  by_reason: Record<string, number>;
};

export type ConversationSummary = { id: string; title: string; document_id: string | null; created_at: string };

export type StoredMessage = { id: string; question: string; answer: string; status: ChatStatus; citations: Citation[]; created_at: string };

export type DocumentPage = { items: DocumentRow[]; next_before: string | null };

export type Credentials = { email: string; pin: string };

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

/** The error a failed response means; a lost session sends the page to sign in. */
async function failure(response: Response): Promise<ApiError> {
  if (response.status === 401 && typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
    // A full load on purpose: the session is gone, so nothing of the old page should stay.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
  }
  let message = `Request failed (${response.status}).`;
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") message = body.detail;
    else if (Array.isArray(body.detail)) message = "Some of what was entered is not valid.";
  } catch {
    /* not JSON: keep the generic message */
  }
  return new ApiError(response.status, message);
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, { cache: "no-store", ...init });
  if (!response.ok) throw await failure(response);
  if (response.status === 204) return null as T; // deleted: nothing to read
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
    body: {
      outcome: "approved" | "rejected";
      meaning: string;
      reason: string;
      override_reason: string | null;
      expected_record_sha256: string;
    },
    who: Credentials,
  ) => call<Signed>(`/v1/documents/${encodeURIComponent(id)}/review`, json({ ...body, ...who })),
  upload: (file: File, docType: string) => {
    const form = new FormData();
    form.append("file", file);
    form.append("doc_type", docType);
    return call<{ document: { id: string }; created: boolean }>("/v1/documents", { method: "POST", body: form });
  },
  evals: () => call<Record<string, unknown>>("/v1/evals"),
  documents: (filters: { before?: string | null; doc_type?: string; stage?: string } = {}) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) if (value) query.set(key, value);
    const qs = query.toString();
    return call<DocumentPage>(`/v1/documents${qs ? `?${qs}` : ""}`);
  },
  timeline: (id: string) => call<Step[]>(`/v1/documents/${encodeURIComponent(id)}/timeline`),
  ask: (question: string, scope: ChatScope = {}) => call<Answer>("/v1/chat", json({ question, ...scope })),
  /** As `ask`, telling `onStage` each stage as it begins; the answer comes whole, checked. */
  askStreamed: async (
    question: string,
    scope: ChatScope,
    onStage: (stage: string, data: Record<string, unknown>) => void,
    signal?: AbortSignal,
  ): Promise<Answer> => {
    const response = await fetch(`${API_URL}/v1/chat/stream`, { ...json({ question, ...scope }), cache: "no-store", signal });
    if (!response.ok) throw await failure(response);
    if (!response.body) throw new ApiError(502, "The answer did not arrive. Try again.");
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        // At the end, a last event without its blank line is still an event.
        const { events, rest } = readEvents(buffer + (value ?? ""), { final: done });
        buffer = rest;
        for (const event of events) {
          if (event.name === "stage") onStage(String(event.data.stage), event.data);
          if (event.name === "answer") return event.data as unknown as Answer;
          if (event.name === "error") throw new ApiError(Number(event.data.status) || 500, String(event.data.detail));
        }
        if (done) break;
      }
    } finally {
      reader.cancel().catch(() => undefined); // nothing more is read: let the connection go
    }
    throw new ApiError(502, "The answer did not arrive. Try again.");
  },
  collections: () => call<KnowledgeBase[]>("/v1/collections"),
  createCollection: (name: string, description: string) => call<KnowledgeBase>("/v1/collections", json({ name, description })),
  renameCollection: (id: string, name: string, description: string) =>
    call<KnowledgeBase>(`/v1/collections/${encodeURIComponent(id)}`, { ...json({ name, description }), method: "PATCH" }),
  deleteCollection: (id: string) => call<null>(`/v1/collections/${encodeURIComponent(id)}`, { method: "DELETE" }),
  collectionDocuments: (id: string) => call<KnowledgeBaseMember[]>(`/v1/collections/${encodeURIComponent(id)}/documents`),
  addToCollection: (id: string, documentIds: string[]) =>
    call<{ added: number }>(`/v1/collections/${encodeURIComponent(id)}/documents`, json({ document_ids: documentIds })),
  removeFromCollection: (id: string, documentId: string) =>
    call<null>(`/v1/collections/${encodeURIComponent(id)}/documents/${encodeURIComponent(documentId)}`, { method: "DELETE" }),
  conversations: () => call<ConversationSummary[]>("/v1/conversations"),
  unanswered: () => call<UnansweredReport>("/v1/questions/unanswered"),
  conversation: (id: string) => call<{ id: string; messages: StoredMessage[] }>(`/v1/conversations/${encodeURIComponent(id)}`),
  pageUrl: (id: string, page: number) => `${API_URL}/v1/documents/${encodeURIComponent(id)}/pages/${page}`,
};
