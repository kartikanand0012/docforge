"use client";

import { useEffect, useRef, useState, useSyncExternalStore, type FormEvent } from "react";
import { ApiError, api, type AgentCall, type ApiKey, type MadeKey } from "@/lib/api";
import { OUTCOME_TEXT, claudeCodeCommand, mcpEndpoint } from "@/lib/agents";

const unchanging = () => () => {};
const FORBIDDEN = "Only administrators can see this page.";

/** Connect an AI agent (Claude Code, or any MCP client over HTTP) to the organisation's
 * documents with a read-only key. For administrators. */
export default function AgentsPage() {
  // The site's origin, known only in the browser: empty when rendered on the server.
  const origin = useSyncExternalStore(unchanging, () => window.location.origin, () => "");
  const endpoint = origin ? mcpEndpoint(origin) : "";
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [calls, setCalls] = useState<AgentCall[] | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [name, setName] = useState("");
  const [made, setMade] = useState<MadeKey | null>(null);
  const [copy, setCopy] = useState<"idle" | "copied" | "refused">("idle");
  const [busy, setBusy] = useState(false);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [said, setSaid] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  // Only the newest load is shown: an older one finishing late never overwrites it.
  const loads = useRef(0);
  const mounted = useRef(true);
  const heading = useRef<HTMLHeadingElement>(null);

  const explain = (e: Error) => {
    if (e instanceof ApiError && e.status === 403) setForbidden(true);
    else setProblem(e.message);
  };

  const load = async () => {
    const mine = ++loads.current;
    const [found, recent] = await Promise.all([api.apiKeys(), api.agentCalls()]);
    if (!mounted.current || mine !== loads.current) return;
    setKeys(found.filter((k) => k.role === "reader"));
    setCalls(recent);
  };

  useEffect(() => {
    mounted.current = true;
    const mine = ++loads.current;
    Promise.all([api.apiKeys(), api.agentCalls()]).then(
      ([found, recent]) => {
        if (!mounted.current || mine !== loads.current) return;
        setKeys(found.filter((k) => k.role === "reader"));
        setCalls(recent);
      },
      (e: Error) => {
        if (!mounted.current) return;
        if (e instanceof ApiError && e.status === 403) setForbidden(true);
        else setProblem(e.message);
      },
    );
    return () => {
      mounted.current = false;
    };
  }, []);

  async function make(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setProblem(null);
    setCopy("idle");
    try {
      setMade(await api.makeKey(name.trim()));
      setName("");
      setSaid("Key made. Copy the command below: it is shown only once.");
    } catch (e) {
      explain(e as Error);
      return;
    } finally {
      setBusy(false);
    }
    load().catch(explain); // the key exists whatever the list does
  }

  async function revoke(key: ApiKey) {
    if (revoking) return;
    setRevoking(key.prefix);
    setProblem(null);
    try {
      await api.revokeKey(key.prefix);
      if (made?.prefix === key.prefix) setMade(null);
      setSaid(`${key.name} revoked: an agent using it is refused from now on.`);
      await load();
    } catch (e) {
      explain(e as Error);
    } finally {
      setRevoking(null);
      heading.current?.focus(); // its button is gone: keep the reader in the list
    }
  }

  async function copyCommand(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopy("copied");
      setTimeout(() => mounted.current && setCopy((now) => (now === "copied" ? "idle" : now)), 2000);
    } catch {
      setCopy("refused");
    }
  }

  const command = made ? claudeCodeCommand(endpoint, made.token) : null;

  return (
    <>
      <h1>Connect an AI agent</h1>
      <p className="muted">
        An AI agent such as Claude Code can search the organisation&rsquo;s documents, read them, and ask questions with
        checked, quoted answers, through the Model Context Protocol (MCP). It uses a read-only key: it can never upload,
        review or change anything.
      </p>
      <p role="status" aria-live="polite" className="visually-hidden">
        {said}
      </p>
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}
      {forbidden ? (
        <p className="card">{FORBIDDEN}</p>
      ) : (
        <>
          <p className="card">
            <strong>A key reads every document of the organisation.</strong> Give it only to agents you trust, and
            revoke it when it is no longer needed. Text from documents is sent as data, not instructions; still, allow
            an agent that can also act elsewhere (send mail, change files) to do so only with your approval.
          </p>

          <h2>Endpoint</h2>
          <p>
            <code className="wrap">{endpoint || "…"}</code> (Streamable HTTP, with the key as{" "}
            <code>Authorization: Bearer</code>)
          </p>

          <h2>Make a read-only key</h2>
          <form onSubmit={make} className="row" aria-label="Make a read-only key">
            <label>
              What it is for{" "}
              <input value={name} onChange={(e) => setName(e.target.value)} maxLength={100} required placeholder="Claude Code on my laptop" />
            </label>
            <button type="submit" disabled={busy}>
              {busy ? "Making…" : "Make key"}
            </button>
          </form>
          {made && command && (
            <div className="card" tabIndex={-1} ref={(card) => card?.focus()} aria-label={`New key ${made.name}`}>
              <p>
                <strong>Copy this now: the key is shown only once.</strong> Run it where Claude Code is installed:
              </p>
              <pre className="command" tabIndex={0} aria-label="Command that adds DocForge to Claude Code">
                {command}
              </pre>
              <div className="row">
                <button type="button" onClick={() => copyCommand(command)}>
                  {copy === "copied" ? "Copied" : "Copy command"}
                </button>
                <button type="button" onClick={() => setMade(null)}>
                  Done, I have copied it
                </button>
              </div>
              {copy === "refused" && (
                <p className="error" role="status">
                  The browser refused to copy: select the command and copy it.
                </p>
              )}
              <p className="muted">Other MCP clients: the endpoint above, with the same Authorization header as the command.</p>
              <p className="muted">
                Claude Code keeps the key in its own settings, and the command stays in your shell history: run it only on a
                computer you trust, and revoke the key if that computer is lost.
              </p>
            </div>
          )}

          <h2 ref={heading} tabIndex={-1}>
            Keys
          </h2>
          {keys === null && !problem && (
            <p className="muted" role="status">
              Loading…
            </p>
          )}
          {keys && keys.length === 0 && <p className="muted">No keys for agents yet.</p>}
          {keys && keys.length > 0 && (
            <div className="table-scroll">
              <table className="documents">
                <thead>
                  <tr>
                    <th scope="col">Name</th>
                    <th scope="col">Made</th>
                    <th scope="col">Last used</th>
                    <th scope="col">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {keys.map((key) => (
                    <tr key={key.prefix}>
                      <td>
                        {key.name} <span className="muted">({key.prefix})</span>
                      </td>
                      <td>
                        <time dateTime={key.created_at}>{new Date(key.created_at).toLocaleDateString()}</time>
                      </td>
                      <td>
                        {key.last_used_at ? <time dateTime={key.last_used_at}>{new Date(key.last_used_at).toLocaleString()}</time> : "Never"}
                      </td>
                      <td>
                        {key.revoked_at ? (
                          <span className="muted">Revoked</span>
                        ) : (
                          <button
                            type="button"
                            onClick={() => revoke(key)}
                            disabled={revoking !== null}
                            aria-label={`Revoke ${key.name} (${key.prefix})`}
                          >
                            {revoking === key.prefix ? "Revoking…" : "Revoke"}
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h2>Latest calls</h2>
          {calls === null && !problem && (
            <p className="muted" role="status">
              Loading…
            </p>
          )}
          {calls && calls.length === 0 && <p className="muted">No agent has called yet.</p>}
          {calls && calls.length > 0 && (
            <div className="table-scroll">
              <table className="documents">
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Key</th>
                    <th scope="col">Tool</th>
                    <th scope="col">Outcome</th>
                    <th scope="col">Results</th>
                  </tr>
                </thead>
                <tbody>
                  {calls.map((c, i) => (
                    <tr key={`${c.created_at}-${i}`}>
                      <td>
                        <time dateTime={c.created_at}>{new Date(c.created_at).toLocaleString()}</time>
                      </td>
                      <td>{c.key_name}</td>
                      <td>
                        <code>{c.tool}</code>
                      </td>
                      <td>{OUTCOME_TEXT[c.outcome] ?? c.outcome}</td>
                      <td>{c.results}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </>
  );
}
