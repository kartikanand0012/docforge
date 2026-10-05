"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import Chat from "@/components/Chat";
import { api, type DocumentRow, type KnowledgeBase, type KnowledgeBaseMember } from "@/lib/api";
import { STAGE_LABELS } from "@/lib/stages";

/** Every document of the organisation, a page at a time (at most 20 pages of 50). */
async function everyDocument(): Promise<DocumentRow[]> {
  const all: DocumentRow[] = [];
  let before: string | null = null;
  for (let n = 0; n < 20; n += 1) {
    const page = await api.documents({ before });
    all.push(...page.items);
    if (!page.next_before) break;
    before = page.next_before;
  }
  return all;
}

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
  const [loaded, setLoaded] = useState(false);
  const [said, setSaid] = useState("");
  const mounted = useRef(true);

  const show = useCallback(
    ([all, inside, documents]: [KnowledgeBase[], KnowledgeBaseMember[], DocumentRow[]]) => {
      if (!mounted.current) return;
      setBase(all.find((b) => b.id === id) ?? null);
      setMembers(inside);
      const taken = new Set(inside.map((m) => m.id));
      const left = documents.filter((d) => !taken.has(d.id));
      setAvailable(left);
      setChosen((current) => current.filter((c) => left.some((d) => d.id === c)));
      setLoaded(true);
    },
    [id],
  );
  const fail = useCallback((e: Error) => mounted.current && setProblem(e.message), []);
  const fetchAll = useCallback(
    () => Promise.all([api.collections(), api.collectionDocuments(id), everyDocument()] as const),
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

  async function act(work: () => Promise<unknown>, done?: string) {
    if (busy) return;
    setBusy(true);
    setProblem(null);
    try {
      await work();
      if (done) setSaid(done);
      await load();
    } catch (e) {
      setProblem((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (problem && !base) return <p className="error" role="alert">{problem}</p>;
  if (!loaded) return <p className="muted">Loading…</p>;
  if (!base) {
    return (
      <div className="card">
        <h1>Knowledge base not found</h1>
        <p>
          It may have been deleted. <Link href="/collections">All knowledge bases</Link>
        </p>
      </div>
    );
  }
  return (
    <>
      <p>
        <Link href="/collections">Knowledge bases</Link>
      </p>
      <h1>{base.name}</h1>
      {base.description && <p className="muted">{base.description}</p>}
      {problem && <p className="error" role="alert">{problem}</p>}
      <p className="sr-only" aria-live="polite">
        {said}
      </p>
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
                  <button type="button" className="link" onClick={() => void act(() => api.removeFromCollection(id, m.id), `${m.filename} removed.`)} aria-label={`Remove ${m.filename}`}>
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          <h3>Add documents</h3>
          {available.length === 0 ? (
            <p className="muted">Every document is already in it.</p>
          ) : (
            <>
              <label htmlFor="add-documents">Documents to add</label>
              <p id="add-hint" className="muted">
                Choose several with Ctrl or Cmd held down.
              </p>
              <select
                id="add-documents"
                aria-describedby="add-hint"
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
                  onClick={() =>
                    chosen.length &&
                    void act(async () => {
                      await api.addToCollection(id, chosen);
                      setChosen([]);
                    }, `${chosen.length} ${chosen.length === 1 ? "document" : "documents"} added.`)
                  }
                >
                  {chosen.length ? `Add ${chosen.length} ${chosen.length === 1 ? "document" : "documents"}` : "Add documents"}
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
                  setBusy(true);
                  api.deleteCollection(id).then(
                    () => router.push("/collections"), // nothing to reload: it is gone
                    (e: Error) => {
                      setProblem(e.message);
                      setBusy(false);
                    },
                  );
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
