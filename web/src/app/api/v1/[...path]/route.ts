import { cookies } from "next/headers";
import { NextResponse, type NextRequest } from "next/server";
import { MAX_BODY_BYTES, SESSION_COOKIE, apiBase, declaredTooLarge, forwardedFor, sameOrigin } from "@/lib/server";

/** The review screen's only way to the API: same origin, with the session token added here,
 * so the browser holds nothing but an HttpOnly cookie. */
async function forward(request: NextRequest, ctx: RouteContext<"/api/v1/[...path]">) {
  if (!sameOrigin(request)) return NextResponse.json({ detail: "Cross-site request refused." }, { status: 403 });
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) return NextResponse.json({ detail: "Sign in first." }, { status: 401 });
  const { path } = await ctx.params;
  if (path.some((part) => part === ".." || part === "." || part.includes("/"))) {
    return NextResponse.json({ detail: "Bad path." }, { status: 400 });
  }
  const target = `${apiBase()}/v1/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`;
  if (declaredTooLarge(request)) return NextResponse.json({ detail: "The file is too large." }, { status: 413 });
  const headers = new Headers({ Authorization: `Bearer ${token}`, ...forwardedFor(request) });
  const type = request.headers.get("content-type");
  if (type) headers.set("Content-Type", type);
  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  const body = hasBody ? await request.arrayBuffer() : undefined;
  // An undeclared length is checked once read; the front proxy caps it before that.
  if (body && body.byteLength > MAX_BODY_BYTES) {
    return NextResponse.json({ detail: "The file is too large." }, { status: 413 });
  }
  const upstream = await fetch(target, {
    method: request.method,
    headers,
    body,
    cache: "no-store",
  });
  const out = new Headers();
  for (const name of ["content-type", "cache-control", "retry-after"]) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  if (upstream.status === 401) (await cookies()).delete(SESSION_COOKIE);
  return new NextResponse(upstream.status === 204 ? null : upstream.body, { status: upstream.status, headers: out });
}

export const GET = forward;
export const POST = forward;
export const DELETE = forward;
export const PATCH = forward;
