"use client";

import { useState, type FormEvent } from "react";
import { api, type ReviewDetail } from "@/lib/api";
import { label } from "@/lib/fields";
import { rememberEmail, savedEmail } from "@/lib/reviewer";

type Props = {
  documentId: string;
  path: string;
  current: string | null;
  onDone: (next: ReviewDetail | null) => void;
};

/** Change or confirm one value. Every correction is signed with the reviewer's PIN. */
export default function CorrectionForm({ documentId, path, current, onDone }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const notPrinted = form.get("not_printed") === "on";
    const email = String(form.get("email")).trim();
    setBusy(true);
    setError(null);
    try {
      const next = await api.correct(
        documentId,
        path,
        notPrinted ? null : String(form.get("text")),
        String(form.get("reason")),
        { email, pin: String(form.get("pin")) },
      );
      rememberEmail(email);
      onDone(next);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <form className="card" style={{ marginTop: 12 }} onSubmit={submit} aria-label={`Correct ${label(path)}`}>
      <h2>Correct {label(path)}</h2>
      <p className="muted">
        Currently: <span className="value">{current ?? "not printed"}</span>. Entering the same value confirms it.
      </p>
      <label htmlFor="text">Value as printed</label>
      <input id="text" name="text" defaultValue={current ?? ""} autoFocus />
      <label>
        <input type="checkbox" name="not_printed" className="inline" /> Not printed on the document
      </label>
      <label htmlFor="reason">Reason</label>
      <input id="reason" name="reason" required placeholder="e.g. checked against the paper copy" />
      <label htmlFor="email">Your email</label>
      <input id="email" name="email" type="email" required defaultValue={savedEmail()} autoComplete="email" />
      <label htmlFor="pin">Your PIN</label>
      <input id="pin" name="pin" type="password" inputMode="numeric" required autoComplete="one-time-code" />
      {error && <p className="error" role="alert">{error}</p>}
      <p className="row">
        <button className="primary" type="submit" disabled={busy}>
          {busy ? "Saving…" : "Save correction"}
        </button>
        <button type="button" onClick={() => onDone(null)} disabled={busy}>
          Cancel
        </button>
      </p>
    </form>
  );
}
