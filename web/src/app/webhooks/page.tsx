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
  const [secret, setSecret] = useState<{ id: string; secret: string } | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [deliveries, setDeliveries] = useState<Delivery[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [said, setSaid] = useState("");
  const [forbidden, setForbidden] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const loads = useRef(0);
  const mounted = useRef(true);
  const heading = useRef<HTMLHeadingElement>(null);

  const explain = (e: Error) => {
    if (e instanceof ApiError && e.status === 403) setForbidden(true);
    else setProblem(e.message);
  };

  const load = async () => {
    const mine = ++loads.current;
    const found = await api.webhooks();
    if (mounted.current && mine === loads.current) setHooks(found);
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

  /** Run one action on a webhook: one at a time, the list reloaded, what happened said. */
  async function act(id: string, what: () => Promise<unknown>, done: string) {
    if (busy) return;
    setBusy(id);
    setProblem(null);
    try {
      await what();
      setSaid(done);
      await load();
    } catch (e) {
      explain(e as Error);
    } finally {
      setBusy(null);
    }
  }

  async function make(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy("new");
    setProblem(null);
    try {
      const made = await api.makeWebhook(url.trim(), chosen);
      setSecret(made);
      setUrl("");
      setChosen([]);
      setSaid("Webhook made. Copy its secret below: it is shown only once.");
      await load();
    } catch (e) {
      explain(e as Error);
    } finally {
      setBusy(null);
    }
  }

  async function rotate(hook: Webhook) {
    const sure = window.confirm("Receivers using the old secret will refuse deliveries until they have the new one. Rotate now?");
    if (!sure) return;
    await act(hook.id, async () => setSecret({ id: hook.id, ...(await api.rotateWebhook(hook.id)) }), "Secret rotated. Copy the new one below.");
  }

  async function remove(hook: Webhook) {
    if (!window.confirm(`Delete the webhook to ${maskedUrl(hook.url)}? It cannot be undone here.`)) return;
    await act(hook.id, () => api.deleteWebhook(hook.id), "Webhook deleted.");
    if (open === hook.id) setOpen(null);
    heading.current?.focus();
  }

  async function show(hook: Webhook) {
    setOpen(hook.id);
    setDeliveries(null);
    try {
      setDeliveries(await api.deliveries(hook.id));
    } catch (e) {
      explain(e as Error);
    }
  }

  async function resend(hook: Webhook, delivery: Delivery) {
    await act(hook.id, () => api.resendDelivery(hook.id, delivery.id), "Sent again.");
    await show(hook);
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
          <input value={url} onChange={(e) => setUrl(e.target.value)} required maxLength={2000} placeholder="https://erp.example.com/hooks" />
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
        <button type="submit" disabled={busy !== null || chosen.length === 0}>
          {busy === "new" ? "Making…" : "Make webhook"}
        </button>
      </form>
      {secret && (
        <div className="card" tabIndex={-1} ref={(card) => card?.focus()} aria-label="Webhook secret">
          <p>
            <strong>Copy this secret now: it is shown only once.</strong> Verify each delivery&rsquo;s
            <code> DocForge-Signature</code> with it.
          </p>
          <pre className="command" tabIndex={0} aria-label="Webhook secret">
            {secret.secret}
          </pre>
          <button type="button" onClick={() => setSecret(null)}>
            Done, I have copied it
          </button>
        </div>
      )}

      <h2 ref={heading} tabIndex={-1}>
        Webhooks
      </h2>
      {hooks === null && !problem && (
        <p className="muted" role="status">
          Loading…
        </p>
      )}
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
              {hooks.map((hook) => (
                <tr key={hook.id}>
                  <td>
                    <code className="wrap">{maskedUrl(hook.url)}</code>
                  </td>
                  <td>{hook.events.join(", ")}</td>
                  <td>{hook.active ? "Active" : "Disabled"}</td>
                  <td>
                    {hook.last_delivery
                      ? `${DELIVERY_TEXT[hook.last_delivery.status] ?? hook.last_delivery.status}${hook.last_delivery.last_status ? ` (${hook.last_delivery.last_status})` : ""}`
                      : "None yet"}
                  </td>
                  <td className="row">
                    <button type="button" disabled={busy !== null} onClick={() => show(hook)}>
                      Deliveries
                    </button>
                    <button type="button" disabled={busy !== null || !hook.active} onClick={() => act(hook.id, () => api.testWebhook(hook.id), "Test queued.")}>
                      Send test
                    </button>
                    <button type="button" disabled={busy !== null} onClick={() => rotate(hook)}>
                      Rotate secret
                    </button>
                    <button
                      type="button"
                      disabled={busy !== null}
                      onClick={() => act(hook.id, () => api.setWebhookActive(hook.id, !hook.active), hook.active ? "Webhook disabled." : "Webhook enabled.")}
                    >
                      {hook.active ? "Disable" : "Enable"}
                    </button>
                    <button type="button" disabled={busy !== null} onClick={() => remove(hook)} aria-label={`Delete the webhook to ${maskedUrl(hook.url)}`}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open && (
        <section aria-label="Deliveries">
          <h2>Deliveries</h2>
          {deliveries === null && (
            <p className="muted" role="status">
              Loading…
            </p>
          )}
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
                            onClick={() => {
                              const hook = hooks?.find((h) => h.id === open);
                              if (hook) void resend(hook, d);
                            }}
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
