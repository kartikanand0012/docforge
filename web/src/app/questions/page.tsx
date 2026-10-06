"use client";

import { useEffect, useState } from "react";
import { ApiError, api, type UnansweredReport } from "@/lib/api";
import { reasonNote } from "@/lib/chat";

const REASONS: Record<string, string> = {
  not_in_passages: "Not in the documents",
  no_passages: "Nothing matched",
  quotes_not_found: "Quotes not found",
  figures_not_in_quotes: "Figures not in quotes",
  wording_not_in_passages: "Not what the documents say",
  model_error: "Model failed",
  not_recorded: "Reason not recorded",
};

/** What the organisation asked that its documents could not answer, and why: the documents
 * worth adding. For administrators. */
export default function UnansweredPage() {
  const [report, setReport] = useState<UnansweredReport | null>(null);
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    let stopped = false;
    api.unanswered().then(
      (found) => !stopped && setReport(found),
      (e: Error) => !stopped && setProblem(e instanceof ApiError && e.status === 403 ? "Only administrators can see this page." : e.message),
    );
    return () => {
      stopped = true;
    };
  }, []);

  return (
    <>
      <h1>Unanswered questions</h1>
      <p className="muted">Questions your documents could not answer, and why. &ldquo;Not in the documents&rdquo; says what was missing: often a document worth adding.</p>
      {problem && <p className="error" role="alert">{problem}</p>}
      {!report && !problem && <p className="muted" role="status">Loading…</p>}
      {report && (
        <>
          {Object.keys(report.by_reason).length > 0 && (
          <ul className="row" role="list" aria-label="Unanswered questions by reason, last 30 days">
            {Object.entries(report.by_reason).map(([reason, n]) => (
              <li key={reason} className="chip">
                {REASONS[reason] ?? reason}: {n}
              </li>
            ))}
          </ul>
          )}
          {report.questions.length === 0 ? (
            <p className="card">Every question has been answered.</p>
          ) : (
            <table className="documents">
              <thead>
                <tr>
                  <th scope="col">Question</th>
                  <th scope="col">Why</th>
                  <th scope="col">Asked</th>
                </tr>
              </thead>
              <tbody>
                {report.questions.map((q) => (
                  <tr key={q.message_id}>
                    <td>{q.question}</td>
                    <td>
                      <span className="chip">{REASONS[q.reason] ?? q.reason}</span>{" "}
                      {reasonNote(q.reason, { missing: q.missing, documents: q.documents })}
                    </td>
                    <td>
                      <time dateTime={q.created_at}>{new Date(q.created_at).toLocaleString()}</time>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </>
  );
}
