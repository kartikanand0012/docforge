"use client";

import Link from "next/link";
import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { STAGE_TEXT, chatScope, sendsOnEnter, statusNote, type Citation, type Turn } from "@/lib/chat";

/** Questions about one document (`documentId`) or every document of the organisation.
 * Each answer shows the quotes it rests on; choosing one shows it on its page (`onCite`),
 * or opens its document. Answers whose quotes are not in the documents are not shown. */
export default function Chat({
  documentId,
  collectionId,
  collectionName,
  conversationId: initial,
  onCite,
  onConversation,
}: {
  documentId?: string;
  /** Ask within one knowledge base. */
  collectionId?: string;
  collectionName?: string;
  conversationId?: string;
  onCite?: (citation: Citation) => void;
  onConversation?: (id: string) => void;
}) {
  const headingId = useId();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [conversationId, setConversationId] = useState<string | undefined>(initial);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [historyFailed, setHistoryFailed] = useState(false);
  const end = useRef<HTMLDivElement | null>(null);
  const box = useRef<HTMLTextAreaElement | null>(null);
  const sending = useRef(false); // set at once, unlike `busy`, so a double Enter sends once
  const asked = useRef(false); // scroll to new answers only once the person has asked
  const leaving = useRef<AbortController | null>(null);

  // Leaving the page (or another document's chat) stops waiting for an answer.
  useEffect(
    () => () => {
      leaving.current?.abort();
    },
    [],
  );

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
      (e: Error) => {
        if (stopped) return;
        setProblem(`This conversation could not be loaded: ${e.message}`);
        setHistoryFailed(true);
      },
    );
    return () => {
      stopped = true;
    };
  }, [initial]);

  useEffect(() => {
    if (!asked.current) return; // never on opening the page or an earlier conversation
    end.current?.scrollIntoView?.({ block: "nearest" });
  }, [turns]);

  async function ask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = question.trim();
    if (!text || sending.current || historyFailed) return;
    sending.current = true;
    asked.current = true;
    setBusy(true);
    setProblem(null);
    setQuestion("");
    const index = turns.length;
    setTurns((current) => [...current, { question: text, answer: null, error: null }]);
    try {
      const scope = chatScope({ documentId, collectionId, conversationId });
      const controller = new AbortController();
      leaving.current = controller;
      const answer = await api.askStreamed(text, scope, (name, data) => setStage(STAGE_TEXT[name]?.(data) ?? null), controller.signal);
      if (controller.signal.aborted) return;
      setConversationId(answer.conversation_id);
      if (!conversationId) onConversation?.(answer.conversation_id);
      setTurns((current) => current.map((t, i) => (i === index ? { ...t, answer } : t)));
    } catch (e) {
      if (leaving.current?.signal.aborted) return; // the page is gone: nothing to show
      setTurns((current) => current.map((t, i) => (i === index ? { ...t, error: (e as Error).message } : t)));
      setQuestion((current) => current || text); // the question is not lost: ask it again
    } finally {
      sending.current = false;
      setBusy(false);
      setStage(null);
      box.current?.focus();
    }
  }

  return (
    <section className="card chat" aria-labelledby={headingId}>
      <h2 id={headingId}>
        {documentId ? "Ask about this document" : collectionId ? `Ask within ${collectionName ?? "this knowledge base"}` : "Ask about your documents"}
      </h2>
      {problem && <p className="error" role="alert">{problem}</p>}
      <p className="sr-only" aria-live="polite">
        {stage ?? ""}
      </p>
      <div className="turns" role="log">
        {turns.map((turn, i) => (
          <article key={i} className="turn">
            <p className="asked"><strong>You:</strong> {turn.question}</p>
            {turn.error ? (
              <p className="error">{turn.error}</p>
            ) : turn.answer === null ? (
              <p className="muted">{stage ?? "Asking…"}</p>
            ) : (
              <div className={`answer ${turn.answer.status}`}>
                <p>{turn.answer.text}</p>
                {statusNote(turn.answer.status, turn.answer.dropped_citations) && (
                  <p className="muted">{statusNote(turn.answer.status, turn.answer.dropped_citations)}</p>
                )}
                {turn.answer.citations.length > 0 && (
                  <ol className="citations" aria-label="Sources">
                    {turn.answer.citations.map((c, n) => {
                      const quoteId = `${headingId}-q${i}-${n}`;
                      return (
                        <li key={n}>
                          {onCite ? (
                            <button type="button" className="citation" onClick={() => onCite(c)} aria-describedby={quoteId} title="Show it on the page">
                              {c.filename}, page {c.page}
                            </button>
                          ) : (
                            <Link className="citation" href={`/documents/${c.document_id}`} aria-describedby={quoteId}>
                              {c.filename}, page {c.page}
                            </Link>
                          )}{" "}
                          <q id={quoteId}>{c.quote}</q>
                        </li>
                      );
                    })}
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
          ref={box}
          id={documentId ? `question-${documentId}` : "question"}
          value={question}
          disabled={historyFailed}
          maxLength={2000}
          rows={2}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (sendsOnEnter({ key: e.key, shiftKey: e.shiftKey, isComposing: e.nativeEvent.isComposing, keyCode: e.keyCode })) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <button className="primary" type="submit" aria-disabled={busy || !question.trim() || historyFailed}>
          {busy ? "Asking…" : "Ask"}
        </button>
      </form>
    </section>
  );
}
