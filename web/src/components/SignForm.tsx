"use client";

import { useState, type FormEvent } from "react";
import { api, type ReviewDetail } from "@/lib/api";
import { rememberEmail, savedEmail } from "@/lib/reviewer";

type Props = { detail: ReviewDetail; onSigned: () => void; onFailed: () => void };

/** Approve or reject under the reviewer's signature. The signature binds to the record shown
 * here (its hash is sent), and its meaning is the fixed sentence the server gives. */
export default function SignForm({ detail, onSigned, onFailed }: Props) {
  const [outcome, setOutcome] = useState<"approved" | "rejected">("approved");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const needsOverride = outcome === "approved" && detail.blockers.length > 0;
  const meaning = detail.meanings[outcome];

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const email = String(form.get("email")).trim();
    setBusy(true);
    setError(null);
    try {
      await api.sign(
        detail.document_id,
        {
          outcome,
          meaning,
          reason: String(form.get("reason")),
          override_reason: needsOverride ? String(form.get("override_reason")) : null,
          expected_record_sha256: detail.record_sha256,
        },
        { email, pin: String(form.get("pin")) },
      );
      rememberEmail(email);
      onSigned();
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
      onFailed(); // the record may have changed or been signed elsewhere: show the latest
    }
  }

  return (
    <form className="card" onSubmit={submit} aria-label="Sign the review">
      <h2>Sign</h2>
      <fieldset>
        <legend>Decision</legend>
        <label>
          <input type="radio" name="outcome" className="inline" checked={outcome === "approved"} onChange={() => setOutcome("approved")} /> Approve
        </label>
        <label>
          <input type="radio" name="outcome" className="inline" checked={outcome === "rejected"} onChange={() => setOutcome("rejected")} /> Reject
        </label>
      </fieldset>
      <p>
        Your signature will mean: <strong>“{meaning}”</strong>
      </p>
      <label htmlFor="sign-reason">Reason</label>
      <input id="sign-reason" name="reason" required placeholder={outcome === "approved" ? "e.g. matches the order and the goods received" : "e.g. duplicate of an earlier invoice"} />
      {needsOverride && (
        <>
          <label htmlFor="override_reason">Why approve although: {detail.blockers.join("; ")}</label>
          <textarea id="override_reason" name="override_reason" required rows={2} />
        </>
      )}
      <label htmlFor="sign-email">Your email</label>
      <input id="sign-email" name="email" type="email" required defaultValue={savedEmail()} autoComplete="email" />
      <label htmlFor="sign-pin">Your PIN</label>
      <input id="sign-pin" name="pin" type="password" inputMode="numeric" required autoComplete="one-time-code" />
      {error && <p className="error" role="alert">{error}</p>}
      <p>
        <button className="primary" type="submit" disabled={busy}>
          {busy ? "Signing…" : outcome === "approved" ? "Sign and approve" : "Sign and reject"}
        </button>
      </p>
    </form>
  );
}
