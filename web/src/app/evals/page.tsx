"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

type Evals = {
  extraction?: {
    model: string; documents: number; documents_fully_correct: number; fields_correct: number; fields_total: number;
    citation_accuracy: number; latency_ms_p50: number; latency_ms_p95: number; input_tokens_per_document: number; output_tokens_per_document: number;
  };
  cost?: { per_document_usd: number | null; price_input_per_million_usd: number | null; price_output_per_million_usd: number | null };
  trust?: { seeded_cases: number; seeded_cases_caught: number; seeded_findings_expected: number; seeded_findings_caught: number; clean_pairs: number; clean_pairs_accepted: number };
  scans?: Record<string, { source: string; fields_correct: number; fields_total: number; documents_with_errors: number; silent_errors: number; silent_errors_after_order_match: number | null }>;
  multipage?: { documents: number; documents_fully_correct: number; fields_correct: number; fields_total: number; pages: number };
};

const pct = (a: number, b: number) => (b ? `${((a / b) * 100).toFixed(2)}%` : "—");

export default function EvalsPage() {
  const [data, setData] = useState<Evals | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.evals().then((d) => setData(d as Evals), (e: Error) => setError(e.message));
  }, []);

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  const { extraction, cost, trust, scans, multipage } = data;

  return (
    <>
      <h1>Evals and cost</h1>
      <p className="muted">
        Measured on synthetic documents with known answers and known defects. They show what the pipeline does on these documents; they are not a claim about every real document.
      </p>
      <div className="grid2">
        {extraction && (
          <div className="card">
            <h2>Extraction</h2>
            <div className="stat">{pct(extraction.fields_correct, extraction.fields_total)}</div>
            <p>
              {extraction.fields_correct} of {extraction.fields_total} printed values correct on {extraction.documents} invoices ({extraction.model}); citations {(extraction.citation_accuracy * 100).toFixed(2)}%.
            </p>
            <p className="muted">
              Model time per invoice: p50 {(extraction.latency_ms_p50 / 1000).toFixed(1)} s, p95 {(extraction.latency_ms_p95 / 1000).toFixed(1)} s.
            </p>
          </div>
        )}
        {trust && (
          <div className="card">
            <h2>Seeded defects</h2>
            <div className="stat">
              {trust.seeded_cases_caught} / {trust.seeded_cases}
            </div>
            <p>
              cases caught ({trust.seeded_findings_caught} of {trust.seeded_findings_expected} expected findings). Correct pairs accepted without review: {trust.clean_pairs_accepted} of {trust.clean_pairs}.
            </p>
          </div>
        )}
        {extraction && cost && (
          <div className="card">
            <h2>Cost per invoice</h2>
            <div className="stat">{cost.per_document_usd === null ? "—" : `$${cost.per_document_usd.toFixed(4)}`}</div>
            <p>
              {extraction.input_tokens_per_document.toLocaleString()} tokens in, {extraction.output_tokens_per_document.toLocaleString()} out per invoice.
            </p>
            <p className="muted">
              {cost.per_document_usd === null
                ? "No model prices are configured, so no cost is shown. Set the current per-token prices to see one."
                : `At $${cost.price_input_per_million_usd} in and $${cost.price_output_per_million_usd} out per million tokens, as configured.`}
            </p>
          </div>
        )}
      </div>

      {scans && (
        <div className="card" style={{ marginTop: 16 }}>
          <h2>Scans</h2>
          <table>
            <thead>
              <tr>
                <th scope="col">Variant</th>
                <th scope="col">Values correct</th>
                <th scope="col">Invoices with a wrong value</th>
                <th scope="col">…accepted by their own checks</th>
                <th scope="col">…and agreeing with their order</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(scans).map(([name, v]) => (
                <tr key={name}>
                  <th scope="row">{name.replace("_", " ")} <span className="muted">({v.source.replace("_", " ")})</span></th>
                  <td>{pct(v.fields_correct, v.fields_total)}</td>
                  <td>{v.documents_with_errors}</td>
                  <td>{v.silent_errors}</td>
                  <td>{v.silent_errors_after_order_match ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {multipage && (
        <div className="card" style={{ marginTop: 16 }}>
          <h2>Long invoices</h2>
          <p>
            {multipage.documents} invoices over {multipage.pages} pages: {multipage.documents_fully_correct} fully correct, {pct(multipage.fields_correct, multipage.fields_total)} of values correct.
          </p>
        </div>
      )}
    </>
  );
}
