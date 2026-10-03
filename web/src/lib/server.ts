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

/** The client address the front proxy (Caddy) set, passed on to the API so its sign-in limit
 * counts each visitor, not this server. Only where DOCFORGE_TRUST_PROXY=1, which a deployment
 * sets when the proxy is the only way in and overwrites the header; a browser that reaches
 * this server directly could put anything there. */
export function forwardedFor(
  request: Request,
  env: Record<string, string | undefined> = process.env,
): Record<string, string> {
  const value = request.headers.get("x-forwarded-for");
  return env.DOCFORGE_TRUST_PROXY === "1" && value ? { "X-Forwarded-For": value } : {};
}

/** Uploads are refused above the API's own limit before they are read into memory. */
export const MAX_BODY_BYTES = 11 * 1024 * 1024;

export function declaredTooLarge(request: Request, limit: number = MAX_BODY_BYTES): boolean {
  const declared = Number(request.headers.get("content-length") ?? "0");
  return Number.isFinite(declared) && declared > limit;
}
