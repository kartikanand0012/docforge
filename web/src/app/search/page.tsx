"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";
import { snippet } from "@/lib/snippet";
import { API_URL } from "@/lib/api";

type Hit = {
  document_id: string;
  doc_type: string;
  filename: string;
  kind: string;
  page: number;
  text: string;
  score: number;
};

export default function SearchPage() {
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [asked, setAsked] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const params = new URLSearchParams({ q: String(form.get("q")), mode: String(form.get("mode")), k: "10" });
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${API_URL}/v1/search?${params}`, { cache: "no-store" });
      const body = (await response.json()) as { results?: Hit[]; detail?: unknown };
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Search failed.");
      setHits(body.results ?? []);
      setAsked(String(form.get("q")));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Search documents</h1>
      <form className="card row" onSubmit={submit} role="search" aria-label="Search documents">
        <label htmlFor="q" className="sr-only">
          Question or words
        </label>
        <input id="q" name="q" required maxLength={500} placeholder="e.g. which invoice billed batch XGX944068" style={{ flex: 1, minWidth: 220 }} />
        <label htmlFor="mode" className="sr-only">
          How to search
        </label>
        <select id="mode" name="mode" defaultValue="hybrid" style={{ width: "auto" }}>
          <option value="hybrid">Words and meaning</option>
          <option value="keyword">Words only</option>
          <option value="vector">Meaning only</option>
        </select>
        <button className="primary" type="submit" disabled={busy}>
          {busy ? "Searching…" : "Search"}
        </button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      {hits && (
        <div className="spaced" aria-live="polite">
          {hits.length === 0 ? (
            <p className="card">Nothing found.</p>
          ) : (
            <ol className="card" style={{ paddingLeft: 24 }}>
              {hits.map((hit, i) => (
                <li key={`${hit.document_id}-${i}`} style={{ marginBottom: 10 }}>
                  <Link href={`/documents/${hit.document_id}`}>{hit.filename}</Link>{" "}
                  <span className="muted">
                    · {hit.doc_type.replace("_", " ")} · page {hit.page} · {hit.kind.replace("_", " ")}
                  </span>
                  <div className="value">{snippet(hit.text, asked, 300)}</div>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </>
  );
}
