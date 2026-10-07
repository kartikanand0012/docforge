"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, api, type AuditChoices, type AuditEntry } from "@/lib/api";
import { chainNote, detailsText, filtersFromQuery, filtersToQuery, localDayRange, type AuditFilters } from "@/lib/audit";

const FORBIDDEN = "Only administrators can see this page.";

/** Who did what, and whether the hash chain still holds. For administrators. */
function AuditLog() {
  const router = useRouter();
  const params = useSearchParams();
  const applied = filtersFromQuery(new URLSearchParams(params.toString()));
  const [draft, setDraft] = useState<AuditFilters & { fromDay?: string; toDay?: string }>({
    action: applied.action,
    actor: applied.actor,
    target_id: applied.target_id,
  });
  const [choices, setChoices] = useState<AuditChoices | null>(null);
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [next, setNext] = useState<number | null>(null);
  const [chain, setChain] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const loads = useRef(0);
  const query = filtersToQuery(applied);

  useEffect(() => {
    let stopped = false;
    api.auditChoices().then(
      (found) => !stopped && setChoices(found),
      () => undefined, // the list below says what is wrong
    );
    return () => {
      stopped = true;
    };
  }, []);

  useEffect(() => {
    const mine = ++loads.current;
    api.audit(query).then(
      (page) => {
        if (mine !== loads.current) return; // a newer filter has been chosen
        setEntries(page.items);
        setNext(page.next_before);
        setProblem(null);
      },
      (e: Error) => {
        if (mine !== loads.current) return;
        if (e instanceof ApiError && e.status === 403) setForbidden(true);
        else setProblem(e.message);
      },
    );
  }, [query]);

  function apply(event: FormEvent) {
    event.preventDefault();
    const days = localDayRange(draft.fromDay ?? "", draft.toDay ?? "");
    const chosen: AuditFilters = { action: draft.action, actor: draft.actor, target_id: draft.target_id, ...days };
    if (chosen.target_id) chosen.target_type = chosen.target_id.length === 12 ? "api_key" : "document";
    router.push(`/audit${filtersToQuery(chosen) ? `?${filtersToQuery(chosen)}` : ""}`);
  }

  async function more() {
    if (next === null || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.audit(filtersToQuery({ ...applied }) + `${query ? "&" : ""}before=${next}`);
      setEntries((now) => [...(now ?? []), ...page.items]);
      setNext(page.next_before);
    } catch (e) {
      setProblem((e as Error).message);
    } finally {
      setLoadingMore(false);
    }
  }

  async function check() {
    setChecking(true);
    setChain(null);
    try {
      setChain(chainNote(await api.verifyChain()));
    } catch (e) {
      setChain((e as Error).message);
    } finally {
      setChecking(false);
    }
  }

  if (forbidden) {
    return (
      <>
        <h1>Audit log</h1>
        <p className="card">{FORBIDDEN}</p>
      </>
    );
  }

  return (
    <>
      <h1>Audit log</h1>
      <p className="muted">Every action, newest first. Each entry is chained to the one before it, so a change to the log shows.</p>
      <div className="card">
        <button type="button" onClick={check} disabled={checking}>
          {checking ? "Checking…" : "Check the chain"}
        </button>{" "}
        <span role="status" aria-live="polite">
          {chain}
        </span>
      </div>

      <form onSubmit={apply} className="row" aria-label="Filter the audit log">
        <label>
          Action{" "}
          <select value={draft.action ?? ""} onChange={(e) => setDraft({ ...draft, action: e.target.value || undefined })}>
            <option value="">Any</option>
            {choices?.actions.map((a) => (
              <option key={a.value} value={a.value}>
                {a.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Who{" "}
          <select value={draft.actor ?? ""} onChange={(e) => setDraft({ ...draft, actor: e.target.value || undefined })}>
            <option value="">Anyone</option>
            {choices?.actors.map((a) => (
              <option key={a.value} value={a.value}>
                {a.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Document id or key prefix{" "}
          <input value={draft.target_id ?? ""} onChange={(e) => setDraft({ ...draft, target_id: e.target.value.trim() || undefined })} />
        </label>
        <label>
          From <input type="date" value={draft.fromDay ?? ""} onChange={(e) => setDraft({ ...draft, fromDay: e.target.value })} />
        </label>
        <label>
          To <input type="date" value={draft.toDay ?? ""} onChange={(e) => setDraft({ ...draft, toDay: e.target.value })} />
        </label>
        <button type="submit">Filter</button>
        <a className="button" href={api.auditExportUrl(query)} download>
          Export CSV
        </a>
      </form>

      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      {entries === null && !problem && (
        <p className="muted" role="status">
          Loading…
        </p>
      )}
      {entries && entries.length === 0 && <p className="muted">No entries match these filters.</p>}
      {entries && entries.length > 0 && (
        <div className="table-scroll">
          <table className="documents">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Who</th>
                <th scope="col">Action</th>
                <th scope="col">On</th>
                <th scope="col">Details</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id}>
                  <td>
                    <time dateTime={e.occurred_at}>{new Date(e.occurred_at).toLocaleString()}</time>
                    <div className="muted">#{e.id}</div>
                  </td>
                  <td>{e.actor_name}</td>
                  <td>{e.action_label}</td>
                  <td>
                    {e.target_type === "document" ? (
                      <a href={`/documents/${encodeURIComponent(e.target_id)}`}>{e.target_label ?? "a document"}</a>
                    ) : (
                      <code className="wrap">
                        {e.target_type} {e.target_id}
                      </code>
                    )}
                  </td>
                  <td>{detailsText(e.details, e.hidden_details)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {next !== null && (
        <button type="button" onClick={more} disabled={loadingMore}>
          {loadingMore ? "Loading…" : "Load more"}
        </button>
      )}
    </>
  );
}

export default function AuditPage() {
  return (
    <Suspense fallback={<p className="muted">Loading…</p>}>
      <AuditLog />
    </Suspense>
  );
}
