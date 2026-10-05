import { toOverlay, type Overlay } from "@/lib/geometry";

export type CitationBox = { page: number; x0: number; y0: number; x1: number; y1: number; page_width?: number; page_height?: number };

export type Citation = { document_id: string; filename: string; doc_type: string; page: number; quote: string; boxes: CitationBox[] };

export type ChatStatus = "supported" | "partly_supported" | "unsupported" | "not_found";

export type Answer = {
  conversation_id: string;
  message_id: string;
  status: ChatStatus;
  text: string;
  citations: Citation[];
  dropped_citations: number;
  words_only: boolean;
};

export type Turn = { question: string; answer: Answer | null; error: string | null };

/** What a reader should know about an answer beyond its text, or nothing. */
export function statusNote(status: ChatStatus, dropped: number): string | null {
  if (status === "partly_supported") {
    return dropped === 1
      ? "1 quote could not be found in the documents and was left out."
      : `${dropped} quotes could not be found in the documents and were left out.`;
  }
  if (status === "unsupported") return "The answer drafted could not be checked against the documents, so it is not shown.";
  if (status === "not_found") return "The answer is not in the documents searched.";
  return null;
}

/** Where a citation's boxes sit on page `page`, as overlays on its image. */
export function citationMarks(boxes: CitationBox[], page: number): Overlay[] {
  return boxes
    .filter((box) => box.page === page && (box.page_width ?? 0) > 0 && (box.page_height ?? 0) > 0)
    .map((box) => toOverlay(box, { width: box.page_width ?? 0, height: box.page_height ?? 0 }));
}

/** Enter sends; Shift+Enter is a new line; Enter that ends an input method's composition
 * (Hindi, Japanese, Chinese keyboards) only finishes the word. */
export function sendsOnEnter(event: { key: string; shiftKey: boolean; isComposing: boolean; keyCode: number }): boolean {
  return event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229;
}

/** What a question is asked within: its conversation's scope for a follow-up, else one
 * document, one knowledge base, or (neither) the whole organisation. */
export function chatScope(where: { documentId?: string; collectionId?: string; conversationId?: string }): {
  document_id?: string;
  collection_id?: string;
  conversation_id?: string;
} {
  if (where.conversationId) return { conversation_id: where.conversationId };
  if (where.documentId) return { document_id: where.documentId };
  if (where.collectionId) return { collection_id: where.collectionId };
  return {};
}

export type StreamEvent = { name: string; data: Record<string, unknown> };

/** The complete events in `buffer` (server-sent events, blank-line separated), and what is
 * left of an event still arriving. */
export function readEvents(buffer: string, options: { final?: boolean } = {}): { events: StreamEvent[]; rest: string } {
  const blocks = buffer.split("\n\n");
  const rest = options.final ? "" : (blocks.pop() ?? "");
  const events: StreamEvent[] = [];
  for (const block of blocks) {
    let name = "message";
    let data = "";
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trimStart();
    }
    if (!data) continue;
    try {
      events.push({ name, data: JSON.parse(data) as Record<string, unknown> });
    } catch {
      /* not an event this page understands */
    }
  }
  return { events, rest };
}

export const STAGE_TEXT: Record<string, (data: Record<string, unknown>) => string> = {
  searching: () => "Searching the documents…",
  reading: (d) => (d.passages ? `Reading ${String(d.passages)} passages…` : "Nothing found to read."),
  checking: () => "Checking every quote against its source…",
};
