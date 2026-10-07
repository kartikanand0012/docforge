"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, api, type AuditChoices, type AuditEntry } from "@/lib/api";
import {
  chainNote,
  detailsText,
  filtersFromQuery,
  filtersToQuery,
  localDayRange,
  localDaysFromRange,
  type AuditFilters,
} from "@/lib/audit";

const FORBIDDEN = "Only administrators can see this page.";
const TARGETS = [
  { value: "", label: "Anything" },
  { value: "document", label: "A document" },
  { value: "api_key", label: "An API key (prefix)" },
  { value: "webhook", label: "A webhook" },
];

/** The filter form, made from the URL's filters: a new URL (back, forward, a shared link)
 * makes a new form, so the form always says what the table shows. */
function FilterForm({ applied, choices, onApply }: { applied: AuditFilters; choices: AuditChoices | null; onApply: (f: AuditFilters) => void }) {
  const days = localDaysFromRange(applied.from, applied.to);
  const [draft, setDraft] = useState({ ...applied, fromDay: days.fromDay, toDay: days.toDay });

  function apply(event: FormEvent) {
    event.preventDefault();
    const range = localDayRange(draft.fromDay, draft.toDay);
    onApply({
      action: draft.action || undefined,
      actor: draft.actor || undefined,
      target_type: draft.target_id ? draft.target_type || undefined : undefined,
      target_id: draft.target_id || undefined,
      ...range,
    });
  }

  return (
    <form onSubmit={apply} className="row" aria-label="Filter the audit log">
      <label>
        Action{" "}
        <select value={draft.action ?? ""} onChange={(e) => setDraft({ ...draft, action: e.target.value })}>
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
        <select value={draft.actor ?? ""} onChange={(e) => setDraft({ ...draft, actor: e.target.value })}>
          <option value="">Anyone</option>
          {choices?.actors.map((a) => (
            <option key={a.value} value={a.value}>
              {a.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        On{" "}
        <select value={draft.target_type ?? ""} onChange={(e) => setDraft({ ...draft, target_type: e.target.value })}>
          {TARGETS.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
      </label>
      <label>
        Its id{" "}
        <input value={draft.target_id ?? ""} onChange={(e) => setDraft({ ...draft, target_id: e.target.value.trim() })} />
      </label>
      <label>
        From <input type="date" value={draft.fromDay} onChange={(e) => setDraft({ ...draft, fromDay: e.target.value })} />
      </label>
      <label>
        To <input type="date" value={draft.toDay} onChange={(e) => setDraft({ ...draft, toDay: e.target.value })} />
      </label>
      <button type="submit">Filter</button>
    </form>
  );
}

/** The entries for one set of filters. Made anew when the filters change, so nothing of an
 * older query (its rows, its cursor, a "Load more" in flight) can mix into a newer one. */
function Entries({ query, onForbidden }: { query: string; onForbidden: () => void }) {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [next, setNext] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const live = useRef(true);

  useEffect(() => {
    live.current = true;
    api.audit(query).then(
      (page) => {
        if (!live.current) return;
        setEntries(page.items);
        setNext(page.next_before);
      },
      (e: Error) => {
        if (!live.current) return;
        if (e instanceof ApiError && e.status === 403) onForbidden();
        else setProblem(e.message);
      },
    );
    return () => {
      live.current = false;
    };
  }, [query, onForbidden]);

  async function more() {
    if (next === null || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await api.audit(`${query}${query ? "&" : ""}before=${next}`);
      if (!live.current) return;
      setEntries((now) => [...(now ?? []), ...page.items]);
      setNext(page.next_before);
    } catch (e) {
      if (live.current) setProblem((e as Error).message);
    } finally {
      if (live.current) setLoadingMore(false);
    }
  }

  return (
    <>
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      {entries === null && !problem && <p className="muted">Loading…</p>}
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
                      <Link href={`/documents/${encodeURIComponent(e.target_id)}`}>{e.target_label ?? "a document"}</Link>
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

/** Who did what, and whether the hash chain still holds. For administrators. */
function AuditLog() {
  const router = useRouter();
  const params = useSearchParams();
  const query = params.toString();
  const applied = filtersFromQuery(new URLSearchParams(query));
  const [choices, setChoices] = useState<AuditChoices | null>(null);
  const [chain, setChain] = useState("");
  const [checking, setChecking] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exported, setExported] = useState("");
  const [forbidden, setForbidden] = useState(false);
  const forbid = useCallback(() => setForbidden(true), []);

  useEffect(() => {
    let stopped = false;
    api.auditChoices().then(
      (found) => !stopped && setChoices(found),
      () => undefined, // the entries below say what is wrong
    );
    return () => {
      stopped = true;
    };
  }, []);

  async function check() {
    setChecking(true);
    setChain("");
    try {
      setChain(chainNote(await api.verifyChain()));
    } catch (e) {
      setChain((e as Error).message);
    } finally {
      setChecking(false);
    }
  }

  /** The export is fetched, not linked: whether it was cut, or failed, can then be said. */
  async function exportCsv() {
    setExporting(true);
    setExported("");
    try {
      const { blob, truncated } = await api.auditExport(filtersToQuery(applied));
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      link.download = "docforge-audit-log.csv";
      link.click();
      URL.revokeObjectURL(href);
      setExported(truncated ? "Exported the newest 10,000 entries only: narrow the dates for the rest." : "Exported.");
    } catch (e) {
      setExported((e as Error).message);
    } finally {
      setExporting(false);
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
      <FilterForm
        key={query}
        applied={applied}
        choices={choices}
        onApply={(f) => {
          const next = filtersToQuery(f);
          router.push(`/audit${next ? `?${next}` : ""}`);
          if (next === query) router.refresh();
        }}
      />
      <p>
        <button type="button" onClick={exportCsv} disabled={exporting}>
          {exporting ? "Exporting…" : "Export these entries as CSV"}
        </button>{" "}
        <span role="status" aria-live="polite">
          {exported}
        </span>
      </p>
      <Entries key={query} query={query} onForbidden={forbid} />
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
