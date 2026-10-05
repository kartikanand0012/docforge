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
