"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type DocumentRow } from "@/lib/api";
import { mergeFresh } from "@/lib/rows";
import { STAGE_LABELS } from "@/lib/stages";

const FINAL = new Set(["ready", "processed", "failed"]);
const TYPES: Record<string, string> = {
  invoice: "Invoice",
  purchase_order: "Purchase order",
  coa: "Certificate of analysis",
  general: "General document",
};

const newestFirst = (a: DocumentRow, b: DocumentRow) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id);

function stageState(stage: string): string {
  if (stage === "failed") return "failed";
  return FINAL.has(stage) ? "done" : "current";
}

/** Every document of the organisation, newest first, with where each one is now. Refreshes
 * while any of them is still being processed. */
export default function DocumentsPage() {
  const [rows, setRows] = useState<DocumentRow[] | null>(null);
  const [next, setNext] = useState<string | null>(null);
  const [docType, setDocType] = useState("");
  const [stage, setStage] = useState("");
  const [problem, setProblem] = useState<string | null>(null);

  // Bumped whenever the filters change: an answer for older filters is dropped, not shown.
  const filters = useRef(0);
  const [busy, setBusy] = useState(false);

  /** The first page: replaces the list when the filters changed, else is merged into it so
   * pages already loaded below stay. True once nothing on it is still being processed. */
  const load = useCallback(
    async (replace: boolean) => {
      const ticket = filters.current;
      try {
        const page = await api.documents({ doc_type: docType, stage });
        if (ticket !== filters.current) return true;
        if (replace) {
          setRows(page.items);
          setNext(page.next_before);
        } else {
          setRows((current) => mergeFresh(current ?? [], page.items));
        }
        setProblem(null);
        return page.items.every((row) => FINAL.has(row.stage));
      } catch (e) {
        if (ticket === filters.current) setProblem((e as Error).message);
        return false;
      }
    },
    [docType, stage],
  );

  useEffect(() => {
    filters.current += 1;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async (replace: boolean) => {
      const settled = await load(replace);
      if (!stopped && !settled) timer = setTimeout(() => void tick(false), 3000);
    };
    void tick(true);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [load]);

  async function more() {
    if (!next || busy) return;
    const ticket = filters.current;
    setBusy(true);
    try {
      const page = await api.documents({ before: next, doc_type: docType, stage });
      if (ticket !== filters.current) return;
      setRows((current) => mergeFresh(page.items, current ?? []).sort(newestFirst));
      setNext(page.next_before);
    } catch (e) {
      if (ticket === filters.current) setProblem((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Documents</h1>
      <form className="filters" aria-label="Filter documents" onSubmit={(e) => e.preventDefault()}>
        <label>
          Type{" "}
          <select value={docType} onChange={(e) => setDocType(e.target.value)}>
            <option value="">All</option>
            {Object.entries(TYPES).map(([value, label]) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </label>
        <label>
          Stage{" "}
          <select value={stage} onChange={(e) => setStage(e.target.value)}>
            <option value="">All</option>
            {["stored", "converting", "parsing", "extracting", "checking", "indexing", "ready", "processed", "failed"].map((s) => (
              <option key={s} value={s}>{STAGE_LABELS[s]}</option>
            ))}
          </select>
        </label>
      </form>
      {problem && <p className="error" role="alert">{problem}</p>}
      <p className="sr-only" aria-live="polite">
        {rows === null ? "" : rows.length === 0 ? "No documents match." : `${rows.length} documents shown.`}
      </p>
      {rows === null ? (
        <p className="muted">Loading…</p>
      ) : rows.length === 0 ? (
        <div className="card">
          <p>No documents{docType || stage ? " match these filters" : " yet"}. <Link href="/upload">Upload one</Link>.</p>
        </div>
      ) : (
        <table className="documents">
          <thead>
            <tr>
              <th scope="col">Document</th>
              <th scope="col">Type</th>
              <th scope="col">Stage</th>
              <th scope="col">Uploaded</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td><Link href={`/documents/${row.id}`}>{row.filename}</Link></td>
                <td>{TYPES[row.doc_type] ?? row.doc_type}</td>
                <td><span className={`chip stage-${stageState(row.stage)}`}>{STAGE_LABELS[row.stage] ?? row.stage}</span></td>
                <td><time dateTime={row.created_at}>{new Date(row.created_at).toLocaleString()}</time></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {next && (
        <p>
          <button type="button" onClick={() => void more()} disabled={busy}>
            {busy ? "Loading…" : "Load more"}
          </button>
        </p>
      )}
    </>
  );
}
