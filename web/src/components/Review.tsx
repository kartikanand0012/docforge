"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Timeline from "@/components/Timeline";
import { ApiError, api, type FieldAssessment, type ReviewDetail } from "@/lib/api";
import { fieldAt, label, section } from "@/lib/fields";
import PageView from "./PageView";
import CorrectionForm from "./CorrectionForm";
import SignForm from "./SignForm";
import SignedCard from "./SignedCard";

const STATUS_TEXT: Record<string, string> = {
  verified: "found in source",
  confirmed: "confirmed by reviewer",
  not_in_cited_blocks: "not found in cited text",
  no_citation: "no source cited",
};

type Waiting = { status: string; error: string | null };

export default function Review({ id }: { id: string }) {
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [waiting, setWaiting] = useState<Waiting | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [attempt, setAttempt] = useState(0);
  const latest = useRef(0);

  /** Load the review; true when there is nothing more to wait for. Keeps what was shown last
   * if the API cannot be reached, and says so. */
  const refresh = useCallback(async (): Promise<boolean> => {
    const ticket = ++latest.current;
    try {
      const next = await api.review(id);
      if (ticket !== latest.current) return true;
      setDetail(next);
      setWaiting(null);
      setProblem(null);
      return true;
    } catch (e) {
      if (ticket !== latest.current) return true;
      if (e instanceof ApiError && e.status === 409) {
        try {
          const summary = await api.document(id);
          const version = summary.versions.at(-1);
          const status = version?.status ?? summary.document.status;
          setWaiting({ status, error: version?.error ?? null });
          setProblem(null);
          return status === "failed";
        } catch (inner) {
          setProblem((inner as Error).message);
          return false;
        }
      }
      if (e instanceof ApiError && e.status < 500) {
        setProblem(e.message);
        return true; // e.g. no such document: retrying will not help
      }
      setProblem(`${(e as Error).message} Retrying…`);
      return false;
    }
  }, [id]);

  // A stage moved on (matched with its order, indexed): show what it changed.
  const reload = useCallback(() => void refresh(), [refresh]);

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
  }, [refresh, attempt]);

  const groups = useMemo(() => {
    const result = new Map<string, FieldAssessment[]>();
    for (const field of detail?.assessment.fields ?? []) {
      const name = section(field.path);
      result.set(name, [...(result.get(name) ?? []), field]);
    }
    return [...result.entries()];
  }, [detail]);

  if (!detail) {
    if (waiting) {
      const failed = waiting.status === "failed";
      return (
        <div className="card">
          <h1>{failed ? "Processing failed" : "Processing"}</h1>
          <Timeline id={id} />
          {failed ? (
            <p className="error" role="alert">{waiting.error ?? "The document could not be processed."}</p>
          ) : (
            <p className="muted">This page updates as each stage finishes.</p>
          )}
        </div>
      );
    }
    return (
      <div className="card">
        <h1>Document</h1>
        {problem ? (
          <>
            <p className="error" role="alert">{problem}</p>
            <button type="button" onClick={() => setAttempt((n) => n + 1)}>
              Try again
            </button>
          </>
        ) : (
          <p className="muted">Loading…</p>
        )}
      </div>
    );
  }

  const d = detail;
  const signed = d.review !== null;
  const needs = d.assessment.fields.filter((f) => f.needs_review).length;
  const select = (path: string) => {
    const off = path === selected;
    setSelected(off ? null : path);
    const field = d.assessment.fields.find((f) => f.path === path);
    const pages = [...new Set(field?.boxes.map((b) => b.page) ?? [])];
    setAnnouncement(
      off ? "" : pages.length ? `${label(path)} is outlined on page ${pages.join(" and ")}` : `${label(path)} has no place on the page`,
    );
  };

  return (
    <>
      <h1>
        {d.filename} <span className="muted">· {d.doc_type.replace("_", " ")} · version {d.version_no}</span>
      </h1>
      <Timeline id={id} compact onChange={reload} />
      <p className="sr-only" aria-live="polite">{announcement}</p>
      {problem && <p className="error" role="alert">{problem}</p>}
      {d.superseded && <p className="notice warn">A newer version of this document is being processed. This version can be read but not corrected or signed.</p>}

      {signed ? null : d.blockers.length === 0 ? (
        <p className="notice ok">
          Every value was found in its source or confirmed, the checks pass{d.doc_type === "invoice" ? " and it matches its order" : ""}. It can be approved.
        </p>
      ) : (
        <div className="notice warn">
          <strong>Needs a person:</strong>
          <ul>
            {d.blockers.map((b, i) => (
              <li key={`${i}-${b}`}>{b}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="review">
        <section aria-label="Pages of the original document">
          <p className="legend muted">
            <span className="swatch needs" aria-hidden="true" /> needs attention <span className="swatch selected" aria-hidden="true" /> selected value
          </p>
          {d.pages.map((page) => (
            <PageView key={page.number} src={api.pageUrl(d.document_id, page.number)} page={page} fields={d.assessment.fields} selected={selected} />
          ))}
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
                            <button type="button" className="link" aria-pressed={isSelected} aria-label={`Show ${label(field.path)} on the page`} onClick={() => select(field.path)}>
                              {label(field.path)}
                            </button>
                          </td>
                          <td className="value">{value?.raw ?? <span className="muted">not printed</span>}</td>
                          <td>
                            <span className={`chip ${field.status}`}>{STATUS_TEXT[field.status] ?? field.status}</span>
                            {field.reasons
                              .filter((r) => r.startsWith("failed"))
                              .map((r, i) => (
                                <div key={`${i}-${r}`} className="chip failed">
                                  {r}
                                </div>
                              ))}
                            {!signed && !d.superseded && d.editable_paths.includes(field.path) && (
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

          {editing && !signed && (
            <CorrectionForm
              key={editing}
              documentId={d.document_id}
              path={editing}
              current={fieldAt(d.record, editing)?.raw ?? null}
              onDone={(next) => {
                const path = editing;
                setEditing(null);
                setSelected(path);
                if (next) setDetail(next);
                setAnnouncement(next ? `${label(path)} saved` : "");
              }}
            />
          )}

          {d.corrections.length > 0 && (
            <div className="card spaced">
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

          <div className="spaced">
            {signed && d.review ? (
              <SignedCard review={d.review} valid={d.signature_valid} />
            ) : d.superseded ? null : (
              <SignForm detail={d} onSigned={() => void refresh()} onFailed={() => void refresh()} />
            )}
          </div>
        </section>
      </div>
    </>
  );
}
