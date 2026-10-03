"use client";

import { useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";
import { rememberEmail, savedEmail } from "@/lib/reviewer";

function LoginForm() {
  const next = useSearchParams().get("next");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    const email = String(form.get("email")).trim();
    const response = await fetch("/api/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tenant: String(form.get("tenant")).trim(), email, pin: String(form.get("pin")) }),
    });
    if (response.ok) {
      rememberEmail(email);
      // Only a path on this site: never an address taken from the query string. A full load,
      // because pages prefetched before sign-in were cached as redirects to this page.
      const target = next && next.startsWith("/") && !next.startsWith("//") ? next : "/";
      window.location.assign(target);
      return;
    }
    const body = (await response.json().catch(() => ({}))) as { detail?: string };
    setError(body.detail ?? "Sign-in failed.");
    setBusy(false);
  }

  return (
    <form className="card" onSubmit={submit} style={{ maxWidth: 420 }} aria-label="Sign in">
      <label htmlFor="tenant">Organisation</label>
      <input id="tenant" name="tenant" required defaultValue="default" autoComplete="organization" />
      <label htmlFor="email">Email</label>
      <input id="email" name="email" type="email" required defaultValue={savedEmail()} autoComplete="username" />
      <label htmlFor="pin">PIN</label>
      <input id="pin" name="pin" type="password" inputMode="numeric" required autoComplete="current-password" />
      {error && <p className="error" role="alert">{error}</p>}
      <p>
        <button className="primary" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </p>
    </form>
  );
}

export default function LoginPage() {
  return (
    <>
      <h1>Sign in</h1>
      <Suspense fallback={<p className="muted">Loading…</p>}>
        <LoginForm />
      </Suspense>
    </>
  );
}
