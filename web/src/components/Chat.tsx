"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { statusNote, type Citation, type Turn } from "@/lib/chat";

/** Questions about one document (`documentId`) or every document of the organisation.
 * Each answer shows the quotes it rests on; choosing one shows it on its page (`onCite`),
 * or opens its document. Answers whose quotes are not in the documents are not shown. */
export default function Chat({
  documentId,
  conversationId: initial,
  onCite,
  onConversation,
}: {
  documentId?: string;
  conversationId?: string;
  onCite?: (citation: Citation) => void;
  onConversation?: (id: string) => void;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<string | undefined>(initial);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const end = useRef<HTMLDivElement | null>(null);

  // An earlier conversation, when opened from the list.
  useEffect(() => {
    if (!initial) return;
    let stopped = false;
    api.conversation(initial).then(
      (found) => {
        if (stopped) return;
        setTurns(
          found.messages.map((m) => ({
            question: m.question,
            answer: {
              conversation_id: found.id, message_id: m.id, status: m.status, text: m.answer,
              citations: m.citations, dropped_citations: 0, words_only: false,
            },
            error: null,
          })),
        );
      },
      (e: Error) => !stopped && setProblem(e.message),
    );
    return () => {
      stopped = true;
    };
  }, [initial]);

  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "nearest" });
  }, [turns.length]);

  async function ask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const asked = question.trim();
    if (!asked || busy) return;
    setBusy(true);
    setProblem(null);
    setQuestion("");
    const index = turns.length;
    setTurns((current) => [...current, { question: asked, answer: null, error: null }]);
    try {
      const answer = await api.ask(asked, conversationId ? { conversation_id: conversationId } : documentId ? { document_id: documentId } : {});
      setConversationId(answer.conversation_id);
      if (!conversationId) onConversation?.(answer.conversation_id);
      setTurns((current) => current.map((t, i) => (i === index ? { ...t, answer } : t)));
    } catch (e) {
      setTurns((current) => current.map((t, i) => (i === index ? { ...t, error: (e as Error).message } : t)));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card chat" aria-label={documentId ? "Ask about this document" : "Ask about your documents"}>
      <h2>{documentId ? "Ask about this document" : "Ask about your documents"}</h2>
      {problem && <p className="error" role="alert">{problem}</p>}
      <div className="turns" aria-live="polite">
        {turns.map((turn, i) => (
          <article key={i} className="turn">
            <p className="asked"><strong>You:</strong> {turn.question}</p>
            {turn.error ? (
              <p className="error" role="alert">{turn.error}</p>
            ) : turn.answer === null ? (
              <p className="muted">Reading the documents…</p>
            ) : (
              <div className={`answer ${turn.answer.status}`}>
                <p>{turn.answer.text}</p>
                {statusNote(turn.answer.status, turn.answer.dropped_citations) && (
                  <p className="muted">{statusNote(turn.answer.status, turn.answer.dropped_citations)}</p>
                )}
                {turn.answer.citations.length > 0 && (
                  <ol className="citations" aria-label="Sources">
                    {turn.answer.citations.map((c, n) => (
                      <li key={n}>
                        {onCite ? (
                          <button type="button" className="citation" onClick={() => onCite(c)}>
                            {c.filename}, page {c.page}
                          </button>
                        ) : (
                          <Link className="citation" href={`/documents/${c.document_id}`}>
                            {c.filename}, page {c.page}
                          </Link>
                        )}{" "}
                        <q>{c.quote}</q>
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            )}
          </article>
        ))}
        <div ref={end} />
      </div>
      <form onSubmit={ask} className="ask">
        <label htmlFor={documentId ? `question-${documentId}` : "question"}>Question</label>
        <textarea
          id={documentId ? `question-${documentId}` : "question"}
          value={question}
          maxLength={2000}
          rows={2}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <button className="primary" type="submit" disabled={busy || !question.trim()}>
          {busy ? "Asking…" : "Ask"}
        </button>
      </form>
    </section>
  );
}
