"use client";

import { useCallback, useRef, useState } from "react";
import Chat from "@/components/Chat";
import Timeline from "@/components/Timeline";
import { api, type DocumentSummary } from "@/lib/api";
import { citationMarks, type Citation } from "@/lib/chat";
import { formatName } from "@/lib/formats";

/** A general document: nothing is extracted, so nothing to review. Where it is, and its pages
 * once read (from the PDF made of it, if it was not one). */
export default function GeneralDocument({ summary }: { summary: DocumentSummary }) {
  const [current, setCurrent] = useState(summary);
  const d = current.document;
  const failed = current.versions.at(-1)?.error ?? null;

  const [problem, setProblem] = useState<string | null>(null);
  const [cited, setCited] = useState<Citation | null>(null);
  const latest = useRef(0);

  // A source chosen in the chat: its page is brought into view with the quote outlined.
  const cite = useCallback((citation: Citation) => {
    setCited(citation);
    document.getElementById(`page-${citation.page}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  // Only the newest answer is shown: an older one arriving late must not undo it.
  const reload = useCallback(() => {
    const ticket = ++latest.current;
    api.document(d.id).then(
      (next) => {
        if (ticket !== latest.current) return;
        setCurrent(next);
        setProblem(null);
      },
      (e: Error) => ticket === latest.current && setProblem(e.message),
    );
  }, [d.id]);

  return (
    <>
      <h1>
        {d.filename} <span className="muted">· general document · {formatName(d.media_type)}</span>
      </h1>
      <div className="card">
        <Timeline id={d.id} docType="general" onChange={reload} />
        {d.stage === "failed" && failed && (
          <p className="error" role="alert">
            {failed}
          </p>
        )}
        {problem && (
          <p className="error" role="alert">
            {problem}
          </p>
        )}
        {d.ready_for_chat && <p className="notice ok">Read and indexed: it can be searched and asked about.</p>}
      </div>
      {d.ready_for_chat && <Chat documentId={d.id} onCite={cite} />}
      {cited && (
        <p className="sr-only" aria-live="polite">
          The quote is outlined on page {cited.page}.
        </p>
      )}
      {(d.page_count ?? 0) > 0 && (
        <section aria-label="Pages of the document">
          {Array.from({ length: d.page_count ?? 0 }, (_, n) => n + 1).map((page) => (
            <figure key={page} id={`page-${page}`} className="page">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={api.pageUrl(d.id, page)} alt={`Page ${page} of ${d.filename}`} loading="lazy" />
              {cited &&
                citationMarks(cited.boxes, page).map((style, i) => (
                  <span key={i} className="mark selected cited" style={style} data-quote={cited.quote} aria-hidden="true" />
                ))}
            </figure>
          ))}
        </section>
      )}
    </>
  );
}
