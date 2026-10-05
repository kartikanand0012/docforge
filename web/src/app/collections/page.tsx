"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { api, type KnowledgeBase } from "@/lib/api";

/** The organisation's knowledge bases: named sets of documents to ask within. */
export default function KnowledgeBasesPage() {
  const [bases, setBases] = useState<KnowledgeBase[] | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);

  const load = useCallback(() => {
    api.collections().then(
      (found) => mounted.current && setBases(found),
      (e: Error) => mounted.current && setProblem(e.message),
    );
  }, []);
  useEffect(() => {
    mounted.current = true;
    load();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!name.trim() || busy) return;
    setBusy(true);
    setProblem(null);
    try {
      await api.createCollection(name.trim(), description.trim());
      setName("");
      setDescription("");
      load();
    } catch (e) {
      setProblem((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Knowledge bases</h1>
      <p className="muted">Group documents (procedures, contracts, a supplier&apos;s papers) and ask questions within the group: answers come only from its documents.</p>
      {problem && <p className="error" role="alert">{problem}</p>}
      <form className="card" onSubmit={create} aria-label="New knowledge base" style={{ maxWidth: 560 }}>
        <h2>New knowledge base</h2>
        <label htmlFor="kb-name">Name</label>
        <input id="kb-name" value={name} maxLength={100} onChange={(e) => setName(e.target.value)} />
        <label htmlFor="kb-description">Description (optional)</label>
        <input id="kb-description" value={description} maxLength={1000} onChange={(e) => setDescription(e.target.value)} />
        <p>
          <button className="primary" type="submit" aria-disabled={busy || !name.trim()}>
            {busy ? "Creating…" : "Create"}
          </button>
        </p>
      </form>
      {bases === null ? (
        <p className="muted">Loading…</p>
      ) : bases.length === 0 ? (
        <p className="card">No knowledge bases yet.</p>
      ) : (
        <table className="documents">
          <thead>
            <tr>
              <th scope="col">Knowledge base</th>
              <th scope="col">Documents</th>
              <th scope="col">Description</th>
            </tr>
          </thead>
          <tbody>
            {bases.map((b) => (
              <tr key={b.id}>
                <td><Link href={`/collections/${b.id}`}>{b.name}</Link></td>
                <td>{b.documents}</td>
                <td className="muted">{b.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}
