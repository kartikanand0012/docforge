"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, type DocumentRow } from "@/lib/api";
import { STAGE_LABELS } from "@/lib/stages";

const FINAL = new Set(["ready", "processed", "failed"]);
const TYPES: Record<string, string> = { invoice: "Invoice", purchase_order: "Purchase order", coa: "Certificate of analysis" };

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

  const load = useCallback(async () => {
    try {
      const page = await api.documents({ doc_type: docType, stage });
      setRows(page.items);
      setNext(page.next_before);
      setProblem(null);
      return page.items.every((row) => FINAL.has(row.stage));
    } catch (e) {
      setProblem((e as Error).message);
      return false;
    }
  }, [docType, stage]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      const settled = await load();
      if (!stopped && !settled) timer = setTimeout(tick, 3000);
    };
    void tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [load]);

  async function more() {
    if (!next) return;
    try {
      const page = await api.documents({ before: next, doc_type: docType, stage });
      setRows((current) => [...(current ?? []), ...page.items]);
      setNext(page.next_before);
    } catch (e) {
      setProblem((e as Error).message);
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
            {["stored", "parsing", "extracting", "checking", "indexing", "ready", "processed", "failed"].map((s) => (
              <option key={s} value={s}>{STAGE_LABELS[s]}</option>
            ))}
          </select>
        </label>
      </form>
      {problem && <p className="error" role="alert">{problem}</p>}
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
          <button type="button" onClick={() => void more()}>Load more</button>
        </p>
      )}
    </>
  );
}
