import { NextResponse, type NextRequest } from "next/server";
import { SESSION_COOKIE } from "@/lib/server";

/** Pages need a session: without the cookie, go to sign-in first. The API routes check
 * their own credentials, and the sign-in page and its route are open. */
export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  if (pathname === "/login" || pathname.startsWith("/api/") || request.cookies.has(SESSION_COOKIE)) {
    return NextResponse.next();
  }
  const login = new URL("/login", request.url);
  login.searchParams.set("next", `${pathname}${search}`);
  return NextResponse.redirect(login);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
