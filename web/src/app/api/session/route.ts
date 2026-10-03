import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { SESSION_COOKIE, apiBase, cookieOptions, sameOrigin } from "@/lib/server";

/** Sign in: the token goes into an HttpOnly cookie; the page never sees it. */
export async function POST(request: Request) {
  if (!sameOrigin(request)) return NextResponse.json({ detail: "Cross-site request refused." }, { status: 403 });
  const body = await request.text();
  const response = await fetch(`${apiBase()}/v1/sessions`, {
    method: "POST",
    // The client's own X-Forwarded-For is not passed on: it could be anything. In deployment
    // the front proxy, not the browser, says where a request came from.
    headers: { "Content-Type": "application/json" },
    body,
    cache: "no-store",
  });
  const payload = (await response.json().catch(() => ({}))) as { token?: string; detail?: string };
  if (!response.ok || !payload.token) {
    const headers = response.headers.get("retry-after") ? { "Retry-After": response.headers.get("retry-after")! } : undefined;
    return NextResponse.json({ detail: payload.detail ?? "Sign-in failed." }, { status: response.status, headers });
  }
  (await cookies()).set(SESSION_COOKIE, payload.token, cookieOptions(request));
  return NextResponse.json({ signedIn: true }, { status: 201 });
}

/** Sign out: end the session at the API and forget the cookie. */
export async function DELETE(request: Request) {
  if (!sameOrigin(request)) return NextResponse.json({ detail: "Cross-site request refused." }, { status: 403 });
  const jar = await cookies();
  const token = jar.get(SESSION_COOKIE)?.value;
  if (token) {
    await fetch(`${apiBase()}/v1/sessions/current`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
      cache: "no-store",
    }).catch(() => undefined);
  }
  jar.delete(SESSION_COOKIE);
  return new NextResponse(null, { status: 204 });
}
