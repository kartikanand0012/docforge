"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, api, type Delivery, type Webhook } from "@/lib/api";
import { DELIVERY_TEXT, maskedUrl, nextAttemptText } from "@/lib/webhooks";

const FORBIDDEN = "Only administrators can see this page.";

/** Webhooks: the organisation's systems told when something happens. For administrators. */
export default function WebhooksPage() {
  const [hooks, setHooks] = useState<Webhook[] | null>(null);
  const [events, setEvents] = useState<string[]>([]);
  const [url, setUrl] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [secret, setSecret] = useState<{ id: string; url: string; secret: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [open, setOpen] = useState<Webhook | null>(null);
  const [deliveries, setDeliveries] = useState<Delivery[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [said, setSaid] = useState("");
  const [forbidden, setForbidden] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const loads = useRef(0);
  const shown = useRef(0);
  const mounted = useRef(true);
  const heading = useRef<HTMLHeadingElement>(null);
  const card = useRef<HTMLDivElement>(null);

  const explain = (e: Error) => {
    if (e instanceof ApiError && e.status === 403) setForbidden(true);
    else setProblem(e.message);
  };

  const load = async () => {
    const mine = ++loads.current;
    const found = await api.webhooks();
    if (mounted.current && mine === loads.current) setHooks(found);
  };

  /** The deliveries of `hook`: only the newest request is shown, so a slow answer for
   * another webhook can never be listed (or re-sent) under this one. */
  const showDeliveries = async (hook: Webhook) => {
    const mine = ++shown.current;
    if (open?.id !== hook.id) setDeliveries(null);
    setOpen(hook);
    try {
      const found = await api.deliveries(hook.id);
      if (mounted.current && mine === shown.current) setDeliveries(found);
    } catch (e) {
      if (mounted.current && mine === shown.current) {
        setDeliveries([]);
        explain(e as Error);
      }
    }
  };

  useEffect(() => {
    mounted.current = true;
    const mine = ++loads.current;
    Promise.all([api.webhooks(), api.webhookEvents()]).then(
      ([found, names]) => {
        if (!mounted.current || mine !== loads.current) return;
        setHooks(found);
        setEvents(names.filter((name) => name !== "webhook.test"));
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

  // A new secret takes focus once, when it appears: not on every later render.
  useEffect(() => {
    if (secret) card.current?.focus();
  }, [secret]);

  /** One action at a time; the list (and an open deliveries panel) reloaded; what happened
   * said. True if it worked. */
  async function act(id: string, what: () => Promise<unknown>, done: string): Promise<boolean> {
    if (busy) return false;
    setBusy(id);
    setProblem(null);
    setSaid("");
    try {
      await what();
      setSaid(done);
      await load();
      if (open) await showDeliveries(open);
      return true;
    } catch (e) {
      explain(e as Error);
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function make(event: FormEvent) {
    event.preventDefault();
    const target = url.trim();
    await act(
      "new",
      async () => {
        const made = await api.makeWebhook(target, chosen);
        setSecret({ ...made, url: target });
        setCopied(false);
        setUrl("");
        setChosen([]);
      },
      "Webhook made. Copy its secret below: it is shown only once.",
    );
  }

  async function rotate(hook: Webhook) {
    if (!window.confirm("Receivers using the old secret will refuse deliveries until they have the new one. Rotate now?")) return;
    await act(
      hook.id,
      async () => {
        setSecret({ id: hook.id, url: hook.url, ...(await api.rotateWebhook(hook.id)) });
        setCopied(false);
      },
      "Secret rotated. Copy the new one below.",
    );
  }

  async function remove(hook: Webhook) {
    if (!window.confirm(`Delete the webhook to ${maskedUrl(hook.url)}? It cannot be undone here.`)) return;
    if (await act(hook.id, () => api.deleteWebhook(hook.id), "Webhook deleted.")) {
      if (open?.id === hook.id) setOpen(null);
      heading.current?.focus(); // its row, and the focused button, are gone
    }
  }

  async function copySecret() {
    if (!secret) return;
    try {
      await navigator.clipboard.writeText(secret.secret);
      setCopied(true);
    } catch {
      setSaid("The browser refused to copy: select the secret and copy it.");
    }
  }

  function done() {
    setSecret(null);
    heading.current?.focus(); // the card, and its focused button, are gone
  }

  if (forbidden) {
    return (
      <>
        <h1>Webhooks</h1>
        <p className="card">{FORBIDDEN}</p>
      </>
    );
  }

  return (
    <>
      <h1>Webhooks</h1>
      <p className="muted">
        DocForge tells your systems when something happens. Each delivery is signed with the webhook&rsquo;s secret and
        carries an event id: a receiver should ignore an id it has already processed.
      </p>
      <p role="status" aria-live="polite" className="visually-hidden">
        {said}
      </p>
      {problem && (
        <p className="error" role="alert">
          {problem}
        </p>
      )}

      <h2>Make a webhook</h2>
      <form onSubmit={make} aria-label="Make a webhook">
        <label>
          URL (https){" "}
          <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} required maxLength={2000} placeholder="https://erp.example.com/hooks" />
        </label>
        <fieldset>
          <legend>Events</legend>
          {events.map((name) => (
            <label key={name}>
              <input
                type="checkbox"
                checked={chosen.includes(name)}
                onChange={(e) => setChosen(e.target.checked ? [...chosen, name] : chosen.filter((c) => c !== name))}
              />{" "}
              {name}
            </label>
          ))}
        </fieldset>
        <p id="events-needed" className="muted">
          Choose at least one event.
        </p>
        <button type="submit" disabled={busy !== null || chosen.length === 0} aria-describedby="events-needed">
          {busy === "new" ? "Making…" : "Make webhook"}
        </button>
      </form>
      {secret && (
        <div className="card" role="group" tabIndex={-1} ref={card} aria-label={`Secret of the webhook to ${maskedUrl(secret.url)}`}>
          <p>
            <strong>Copy this secret now: it is shown only once.</strong> Verify each delivery&rsquo;s
            <code> DocForge-Signature</code> with it.
          </p>
          <pre className="command" tabIndex={0} aria-label="The secret">
            {secret.secret}
          </pre>
          <div className="row">
            <button type="button" onClick={copySecret}>
              {copied ? "Copied" : "Copy secret"}
            </button>
            <button type="button" onClick={done}>
              Done, I have copied it
            </button>
          </div>
        </div>
      )}

      <h2 ref={heading} tabIndex={-1}>
        Webhooks
      </h2>
      {hooks === null && !problem && <p className="muted">Loading…</p>}
      {hooks && hooks.length === 0 && <p className="muted">No webhooks yet.</p>}
      {hooks && hooks.length > 0 && (
        <div className="table-scroll">
          <table className="documents">
            <thead>
              <tr>
                <th scope="col">URL</th>
                <th scope="col">Events</th>
                <th scope="col">Status</th>
                <th scope="col">Last delivery</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {hooks.map((hook) => {
                const to = maskedUrl(hook.url);
                return (
                  <tr key={hook.id}>
                    <td>
                      <code className="wrap">{to}</code>
                    </td>
                    <td>{hook.events.join(", ")}</td>
                    <td>{hook.active ? "Active" : "Disabled"}</td>
                    <td>
                      {hook.last_delivery
                        ? `${DELIVERY_TEXT[hook.last_delivery.status] ?? hook.last_delivery.status}${hook.last_delivery.last_status ? ` (${hook.last_delivery.last_status})` : ""}`
                        : "None yet"}
                    </td>
                    <td className="row">
                      <button type="button" onClick={() => void showDeliveries(hook)} aria-label={`Deliveries to ${to}`}>
                        Deliveries
                      </button>
                      <button
                        type="button"
                        disabled={busy !== null || !hook.active}
                        onClick={() => void act(hook.id, () => api.testWebhook(hook.id), "Test queued.")}
                        aria-label={`Send a test to ${to}`}
                      >
                        Send test
                      </button>
                      <button type="button" disabled={busy !== null} onClick={() => void rotate(hook)} aria-label={`Rotate the secret of ${to}`}>
                        Rotate secret
                      </button>
                      <button
                        type="button"
                        disabled={busy !== null}
                        onClick={() =>
                          void act(hook.id, () => api.setWebhookActive(hook.id, !hook.active), hook.active ? "Webhook disabled." : "Webhook enabled.")
                        }
                        aria-label={`${hook.active ? "Disable" : "Enable"} the webhook to ${to}`}
                      >
                        {hook.active ? "Disable" : "Enable"}
                      </button>
                      <button type="button" disabled={busy !== null} onClick={() => void remove(hook)} aria-label={`Delete the webhook to ${to}`}>
                        Delete
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {open && (
        <section aria-label={`Deliveries to ${maskedUrl(open.url)}`}>
          <h2>Deliveries to {maskedUrl(open.url)}</h2>
          {deliveries === null && <p className="muted">Loading…</p>}
          {deliveries && deliveries.length === 0 && <p className="muted">Nothing sent yet.</p>}
          {deliveries && deliveries.length > 0 && (
            <div className="table-scroll">
              <table className="documents">
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Event</th>
                    <th scope="col">Status</th>
                    <th scope="col">Attempts</th>
                    <th scope="col">Answer</th>
                    <th scope="col">Next try</th>
                    <th scope="col">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {deliveries.map((d) => (
                    <tr key={d.id}>
                      <td>
                        <time dateTime={d.created_at}>{new Date(d.created_at).toLocaleString()}</time>
                      </td>
                      <td>{d.event_type}</td>
                      <td>{DELIVERY_TEXT[d.status] ?? d.status}</td>
                      <td>{d.attempts}</td>
                      <td>{d.last_status ?? d.last_error ?? ""}</td>
                      <td>{nextAttemptText(d.next_attempt_at)}</td>
                      <td>
                        {d.status === "failed" && (
                          <button
                            type="button"
                            disabled={busy !== null}
                            onClick={() => void act(open.id, () => api.resendDelivery(open.id, d.id), "Sent again.")}
                          >
                            Send again
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}
    </>
  );
}
