"use client";

import { useCallback, useEffect, useState } from "react";
import Chat from "@/components/Chat";
import { api, type ConversationSummary } from "@/lib/api";

/** Questions across every document of the organisation, and one's earlier conversations. */
export default function ChatPage() {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [open, setOpen] = useState<string | undefined>(undefined);
  const [problem, setProblem] = useState<string | null>(null);

  const load = useCallback(() => {
    api.conversations().then(setConversations, (e: Error) => setProblem(e.message));
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <h1>Chat</h1>
      <p className="muted">Answers come only from your organisation&apos;s documents, with the quotes they rest on. When the documents do not say, it says so.</p>
      {problem && <p className="error" role="alert">{problem}</p>}
      <div className="chat-layout">
        <nav aria-label="Earlier conversations" className="card">
          <h2>Conversations</h2>
          <p>
            <button type="button" onClick={() => setOpen(undefined)} aria-pressed={open === undefined}>
              New conversation
            </button>
          </p>
          <ul>
            {conversations.map((c) => (
              <li key={c.id}>
                <button type="button" className="link" onClick={() => setOpen(c.id)} aria-pressed={open === c.id}>
                  {c.title}
                </button>
              </li>
            ))}
          </ul>
        </nav>
        <Chat key={open ?? "new"} conversationId={open} onConversation={load} />
      </div>
    </>
  );
}
