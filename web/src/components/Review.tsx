"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, api, type FieldAssessment, type ReviewDetail } from "@/lib/api";
import { fieldAt, label, section } from "@/lib/fields";
import PageView from "./PageView";
import CorrectionForm from "./CorrectionForm";
import SignForm from "./SignForm";
import SignedCard from "./SignedCard";

const STATUS_TEXT: Record<FieldAssessment["status"], string> = {
  verified: "found in source",
  confirmed: "confirmed by reviewer",
  not_in_cited_blocks: "not found in cited text",
  no_citation: "no source cited",
};

type Load = { kind: "loading" } | { kind: "processing"; status: string; error: string | null } | { kind: "error"; message: string } | { kind: "ready"; detail: ReviewDetail };

export default function Review({ id }: { id: string }) {
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [selected, setSelected] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");

  const refresh = useCallback(async () => {
    try {
      setLoad({ kind: "ready", detail: await api.review(id) });
      return true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // Not extracted yet: show where processing is.
        const summary = await api.document(id);
        const latest = summary.versions.at(-1);
        setLoad({ kind: "processing", status: latest?.status ?? summary.document.status, error: latest?.error ?? null });
        return latest?.status === "failed";
      }
      setLoad({ kind: "error", message: (e as Error).message });
      return true;
    }
  }, [id]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      const done = await refresh();
      if (!done && !stopped) timer = setTimeout(tick, 2000);
    };
    void tick();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [refresh]);

  const detail = load.kind === "ready" ? load.detail : null;
  const groups = useMemo(() => {
    const result = new Map<string, FieldAssessment[]>();
    for (const field of detail?.assessment.fields ?? []) {
      const name = section(field.path);
      result.set(name, [...(result.get(name) ?? []), field]);
    }
    return [...result.entries()];
  }, [detail]);

  if (load.kind === "loading") return <p className="muted">Loading…</p>;
  if (load.kind === "error") return <p className="error" role="alert">{load.message}</p>;
  if (load.kind === "processing") {
    return (
      <div className="card" aria-live="polite">
        <h1>Processing</h1>
        <p>
          Status: <strong>{load.status}</strong>
        </p>
        {load.error ? <p className="error">{load.error}</p> : <p className="muted">This page updates when the extraction is ready.</p>}
      </div>
    );
  }

  const d = load.detail;
  const signed = d.review !== null;
  const selectedField = d.assessment.fields.find((f) => f.path === selected) ?? null;
  const needs = d.assessment.fields.filter((f) => f.needs_review).length;

  return (
    <>
      <h1>
        {d.filename} <span className="muted">· {d.doc_type.replace("_", " ")} · version {d.version_no}</span>
      </h1>
      <p className="sr-only" aria-live="polite">{announcement}</p>

      {signed ? null : d.blockers.length === 0 ? (
        <p className="notice ok">Every value was found in its source or confirmed, the checks pass{d.doc_type === "invoice" ? " and it matches its order" : ""}. It can be approved.</p>
      ) : (
        <div className="notice warn">
          <strong>Needs a person:</strong>
          <ul>
            {d.blockers.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="review">
        <section aria-label="Pages of the original document">
          <div className="sticky">
            {d.pages.map((page) => (
              <PageView
                key={page.number}
                src={api.pageUrl(d.document_id, page.number)}
                page={page}
                fields={d.assessment.fields}
                selected={selected}
              />
            ))}
          </div>
        </section>

        <section aria-label="Extracted values">
          <div className="card">
            <div className="row">
              <span className={`chip ${d.match_status}`}>order: {d.match_status.replace("_", " ")}</span>
              <span className="muted">
                {d.assessment.fields.length} values, {needs} need attention
              </span>
            </div>
            {d.discrepancies.length > 0 && (
              <>
                <h2>Differences from the purchase order</h2>
                <ul>
                  {d.discrepancies.map((x, i) => (
                    <li key={i}>
                      <span className={`chip ${x.severity === "error" ? "failed" : "review"}`}>{x.severity}</span> {x.message}
                    </li>
                  ))}
                </ul>
              </>
            )}
            <h2>Values</h2>
            <div className="fields">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Field</th>
                    <th scope="col">Value</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                {groups.map(([name, fields]) => (
                  <tbody key={name}>
                    <tr>
                      <th scope="rowgroup" colSpan={3}>{name}</th>
                    </tr>
                    {fields.map((field) => {
                      const value = fieldAt(d.record, field.path);
                      const isSelected = field.path === selected;
                      return (
                        <tr key={field.path} className={`field-row${isSelected ? " selected" : ""}`}>
                          <td>
                            <button
                              type="button"
                              aria-pressed={isSelected}
                              onClick={() => {
                                setSelected(isSelected ? null : field.path);
                                setAnnouncement(isSelected ? "" : `${label(field.path)} highlighted on the page`);
                              }}
                            >
                              {label(field.path)}
                            </button>
                          </td>
                          <td className="value">{value?.raw ?? <span className="muted">not printed</span>}</td>
                          <td>
                            <span className={`chip ${field.status}`}>{STATUS_TEXT[field.status]}</span>
                            {field.reasons
                              .filter((r) => r.startsWith("failed"))
                              .map((r) => (
                                <div key={r} className="chip failed">{r}</div>
                              ))}
                            {!signed && d.editable_paths.includes(field.path) && (
                              <div>
                                <button type="button" onClick={() => setEditing(field.path)} aria-label={`Correct ${label(field.path)}`}>
                                  Correct
                                </button>
                              </div>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                ))}
              </table>
            </div>
          </div>

          {editing && (
            <CorrectionForm
              documentId={d.document_id}
              path={editing}
              current={fieldAt(d.record, editing)?.raw ?? null}
              onDone={(next) => {
                setEditing(null);
                setSelected(editing);
                if (next) setLoad({ kind: "ready", detail: next });
                setAnnouncement(next ? `${label(editing)} corrected` : "");
              }}
            />
          )}

          {d.corrections.length > 0 && (
            <div className="card" style={{ marginTop: 12 }}>
              <h2>Corrections</h2>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Field</th>
                    <th scope="col">From</th>
                    <th scope="col">To</th>
                    <th scope="col">Why, by whom</th>
                  </tr>
                </thead>
                <tbody>
                  {d.corrections.map((c, i) => (
                    <tr key={i}>
                      <td>{label(c.path)}</td>
                      <td className="value">{c.old_text ?? "—"}</td>
                      <td className="value">{c.new_text ?? "—"}</td>
                      <td>
                        {c.reason}
                        <div className="muted">
                          {c.reviewer_name}, {new Date(c.created_at).toLocaleString()}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div style={{ marginTop: 12 }}>
            {signed && d.review ? (
              <SignedCard review={d.review} valid={d.signature_valid} />
            ) : (
              <SignForm detail={d} onSigned={() => void refresh()} />
            )}
          </div>
          {selectedField === null ? null : <p className="sr-only">{label(selectedField.path)} selected</p>}
        </section>
      </div>
    </>
  );
}
