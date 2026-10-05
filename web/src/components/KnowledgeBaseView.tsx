"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import Chat from "@/components/Chat";
import { api, type DocumentRow, type KnowledgeBase, type KnowledgeBaseMember } from "@/lib/api";
import { STAGE_LABELS } from "@/lib/stages";

/** One knowledge base: its documents (added from the organisation's, or removed), and
 * questions asked within it. */
export default function KnowledgeBaseView({ id }: { id: string }) {
  const router = useRouter();
  const [base, setBase] = useState<KnowledgeBase | null>(null);
  const [members, setMembers] = useState<KnowledgeBaseMember[]>([]);
  const [available, setAvailable] = useState<DocumentRow[]>([]);
  const [chosen, setChosen] = useState<string[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);

  const show = useCallback(
    ([all, inside, page]: [KnowledgeBase[], KnowledgeBaseMember[], { items: DocumentRow[] }]) => {
      if (!mounted.current) return;
      setBase(all.find((b) => b.id === id) ?? null);
      setMembers(inside);
      const taken = new Set(inside.map((m) => m.id));
      setAvailable(page.items.filter((d) => !taken.has(d.id)));
    },
    [id],
  );
  const fail = useCallback((e: Error) => mounted.current && setProblem(e.message), []);
  const fetchAll = useCallback(
    () => Promise.all([api.collections(), api.collectionDocuments(id), api.documents()] as const),
    [id],
  );
  const load = useCallback(() => fetchAll().then(show, fail), [fetchAll, show, fail]);
  useEffect(() => {
    mounted.current = true;
    fetchAll().then(show, fail);
    return () => {
      mounted.current = false;
    };
  }, [fetchAll, show, fail]);

  async function act(work: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    try {
      await work();
      await load();
    } catch (e) {
      setProblem((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (problem && !base) return <p className="error" role="alert">{problem}</p>;
  if (!base) return <p className="muted">Loading…</p>;
  return (
    <>
      <p>
        <Link href="/collections">Knowledge bases</Link>
      </p>
      <h1>{base.name}</h1>
      {base.description && <p className="muted">{base.description}</p>}
      {problem && <p className="error" role="alert">{problem}</p>}
      <div className="review">
        <section className="card" aria-label="Documents in this knowledge base">
          <h2>Documents ({members.length})</h2>
          {members.length === 0 ? (
            <p className="muted">None yet: add some below.</p>
          ) : (
            <ul className="members">
              {members.map((m) => (
                <li key={m.id}>
                  <Link href={`/documents/${m.id}`}>{m.filename}</Link> <span className="muted">· {STAGE_LABELS[m.stage] ?? m.stage}</span>{" "}
                  <button type="button" className="link" onClick={() => void act(() => api.removeFromCollection(id, m.id))} aria-label={`Remove ${m.filename}`}>
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          <h3>Add documents</h3>
          {available.length === 0 ? (
            <p className="muted">Every recent document is already in it.</p>
          ) : (
            <>
              <label htmlFor="add-documents">Documents to add</label>
              <select
                id="add-documents"
                multiple
                size={Math.min(8, available.length)}
                value={chosen}
                onChange={(e) => setChosen(Array.from(e.target.selectedOptions, (o) => o.value))}
              >
                {available.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.filename}
                  </option>
                ))}
              </select>
              <p>
                <button
                  type="button"
                  className="primary"
                  aria-disabled={busy || chosen.length === 0}
                  onClick={() => chosen.length && void act(async () => {
                    await api.addToCollection(id, chosen);
                    setChosen([]);
                  })}
                >
                  Add {chosen.length || ""} {chosen.length === 1 ? "document" : "documents"}
                </button>
              </p>
            </>
          )}
          <h3>Knowledge base</h3>
          <p>
            <button
              type="button"
              onClick={() => {
                if (window.confirm(`Delete the knowledge base "${base.name}"? Its documents are kept.`)) {
                  void act(async () => {
                    await api.deleteCollection(id);
                    router.push("/collections");
                  });
                }
              }}
            >
              Delete knowledge base
            </button>
          </p>
        </section>
        <Chat key={id} collectionId={id} collectionName={base.name} />
      </div>
    </>
  );
}
