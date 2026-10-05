"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Chat from "@/components/Chat";
import { api, type ConversationSummary } from "@/lib/api";

/** Questions across every document of the organisation, and one's earlier conversations. */
export default function ChatPage() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [open, setOpen] = useState<string | undefined>(undefined);
  const [current, setCurrent] = useState<string | undefined>(undefined);
  const [problem, setProblem] = useState<string | null>(null);
  const latest = useRef(0);
  const mounted = useRef(true);

  // Only the newest answer is shown, and none after leaving the page.
  const load = useCallback(() => {
    const ticket = ++latest.current;
    api.conversations().then(
      (found) => mounted.current && ticket === latest.current && setConversations(found),
      (e: Error) => mounted.current && ticket === latest.current && setProblem(e.message),
    );
  }, []);
  useEffect(() => {
    mounted.current = true;
    load();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  const choose = (id: string | undefined) => {
    setOpen(id);
    setCurrent(id);
  };

  return (
    <>
      <h1>Chat</h1>
      <p className="muted">Answers come only from your organisation&apos;s documents, with the quotes they rest on. When the documents do not say, it says so.</p>
      {problem && <p className="error" role="alert">{problem}</p>}
      <div className="chat-layout">
        <nav aria-label="Earlier conversations" className="card">
          <h2>Conversations</h2>
          <p>
            <button type="button" onClick={() => choose(undefined)} aria-pressed={current === undefined}>
              New conversation
            </button>
          </p>
          <ul>
            {conversations.map((c) => (
              <li key={c.id}>
                <button type="button" className="link" onClick={() => choose(c.id)} aria-pressed={current === c.id}>
                  {c.title}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <Chat
          key={open ?? "new"}
          conversationId={open}
          onConversation={(id) => {
            setCurrent(id); // the new conversation is the one shown, without reloading it
            load();
          }}
        />
      </div>
    </>
  );
}
