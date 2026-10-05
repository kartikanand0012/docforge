"use client";

import { useEffect, useState } from "react";
import GeneralDocument from "@/components/GeneralDocument";
import Review from "@/components/Review";
import { api, type DocumentSummary } from "@/lib/api";

/** A document's page: the review for the checked types, the plain view for general ones. */
export default function DocumentView({ id }: { id: string }) {
  const [summary, setSummary] = useState<DocumentSummary | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let stopped = false;
    api.document(id).then(
      (found) => !stopped && setSummary(found),
      (e: Error) => !stopped && setProblem(e.message),
    );
    return () => {
      stopped = true;
    };
  }, [id]);

  if (summary?.document.doc_type === "general") return <GeneralDocument key={summary.document.id} summary={summary} />;
  if (summary || problem) return <Review id={id} />; // the review says what went wrong, and retries
  return (
    <div className="card">
      <h1>Document</h1>
      <p className="muted">Loading…</p>
    </div>
  );
}
