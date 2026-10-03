/** Server-side only: where the API is, and the session cookie that carries the token. */

export const SESSION_COOKIE = "df_session";

export function apiBase(): string {
  return (process.env.DOCFORGE_API_URL ?? "http://127.0.0.1:8000").replace(/\/$/, "");
}

/** Secure whenever the browser reached us over HTTPS (directly or through a proxy that says
 * so); a Secure cookie over plain HTTP would be dropped, which only happens locally. */
export function cookieOptions(request: Request) {
  const https =
    new URL(request.url).protocol === "https:" || request.headers.get("x-forwarded-proto") === "https";
  return {
    httpOnly: true, // the token is never readable by page scripts
    sameSite: "strict" as const, // and never sent with a request started by another site
    secure: https,
    path: "/",
    maxAge: 8 * 60 * 60,
  };
}

/** A state-changing request must come from this site's own pages. */
export function sameOrigin(request: Request): boolean {
  const origin = request.headers.get("origin");
  if (origin === null) return request.method === "GET" || request.method === "HEAD";
  // Compared with the Host the browser addressed: the server's own idea of its URL can differ
  // (for example localhost against 127.0.0.1). Behind a proxy, it must pass Host through.
  const host = request.headers.get("host");
  try {
    return host !== null && new URL(origin).host === host;
  } catch {
    return false;
  }
}
