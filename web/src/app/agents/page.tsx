"use client";

import { useEffect, useState, useSyncExternalStore, type FormEvent } from "react";
import { ApiError, api, type AgentCall, type ApiKey, type MadeKey } from "@/lib/api";
import { OUTCOME_TEXT, claudeCodeCommand, mcpEndpoint } from "@/lib/agents";

/** Connect an AI agent (Claude Code, or any MCP client over HTTP) to the organisation's
 * documents with a read-only key. For administrators. */
const unchanging = () => () => {};

export default function AgentsPage() {
  // The site's origin, known only in the browser: empty when rendered on the server.
  const origin = useSyncExternalStore(unchanging, () => window.location.origin, () => "");
  const endpoint = origin ? mcpEndpoint(origin) : "";
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [calls, setCalls] = useState<AgentCall[]>([]);
  const [name, setName] = useState("");
  const [made, setMade] = useState<MadeKey | null>(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const explain = (e: Error) =>
    setProblem(e instanceof ApiError && e.status === 403 ? "Only administrators can see this page." : e.message);

  const fetchAll = () => Promise.all([api.apiKeys(), api.agentCalls()]);
  const show = ([found, recent]: [ApiKey[], AgentCall[]]) => {
    setKeys(found.filter((k) => k.role === "reader"));
    setCalls(recent);
  };
  const load = async () => show(await fetchAll());

  useEffect(() => {
    let stopped = false;
    Promise.all([api.apiKeys(), api.agentCalls()]).then(
      ([found, recent]) => {
        if (stopped) return;
        setKeys(found.filter((k) => k.role === "reader"));
        setCalls(recent);
      },
      (e: Error) => !stopped && setProblem(e instanceof ApiError && e.status === 403 ? "Only administrators can see this page." : e.message),
    );
    return () => {
      stopped = true;
    };
  }, []);

  async function make(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setProblem(null);
    setCopied(false);
    try {
      setMade(await api.makeKey(name.trim()));
      setName("");
      await load();
    } catch (e) {
      explain(e as Error);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(key: ApiKey) {
    setProblem(null);
    try {
      await api.revokeKey(key.prefix);
      if (made?.prefix === key.prefix) setMade(null);
      await load();
    } catch (e) {
      explain(e as Error);
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setProblem("Copying was refused by the browser: select the command and copy it.");
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
      <p className="card">
        <strong>A key reads every document of the organisation.</strong> Give it only to agents you trust, and revoke it
        when it is no longer needed. Text from documents is sent as data, not instructions; still, allow an agent that
        can also act elsewhere (send mail, change files) to do so only with your approval.
      </p>
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}

      <h2>Endpoint</h2>
      <p>
        <code>{endpoint || "…"}</code> (Streamable HTTP, with the key as <code>Authorization: Bearer</code>)
      </p>

      <h2>Make a read-only key</h2>
      <form onSubmit={make} className="row">
        <label>
          What it is for{" "}
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={100} required placeholder="Claude Code on my laptop" />
        </label>
        <button type="submit" disabled={busy || !name.trim()}>
          {busy ? "Making…" : "Make key"}
        </button>
      </form>
      {made && command && (
        <div className="card" role="status">
          <p>
            <strong>Copy this now: the key is shown only once.</strong> Run it where Claude Code is installed:
          </p>
          <pre className="command">{command}</pre>
          <button type="button" onClick={() => copy(command)}>
            {copied ? "Copied" : "Copy command"}
          </button>
          <p className="muted">Other MCP clients: the endpoint above, with the header <code>Authorization: Bearer {made.token}</code>.</p>
        </div>
      )}

      <h2>Keys</h2>
      {keys === null && !problem && (
        <p className="muted" role="status">
          Loading…
        </p>
      )}
      {keys && keys.length === 0 && <p className="muted">No keys for agents yet.</p>}
      {keys && keys.length > 0 && (
        <table className="documents">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Made</th>
              <th scope="col">Last used</th>
              <th scope="col">
                <span className="visually-hidden">Revoke</span>
              </th>
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
                <td>{key.last_used_at ? <time dateTime={key.last_used_at}>{new Date(key.last_used_at).toLocaleString()}</time> : "Never"}</td>
                <td>
                  {key.revoked_at ? (
                    <span className="muted">Revoked</span>
                  ) : (
                    <button type="button" onClick={() => revoke(key)} aria-label={`Revoke ${key.name}`}>
                      Revoke
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h2>Latest calls</h2>
      {calls.length === 0 ? (
        <p className="muted">No agent has called yet.</p>
      ) : (
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
      )}
    </>
  );
}
