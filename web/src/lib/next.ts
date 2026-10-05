/** Where to go after sign-in: a path on this site only, never another address. */
export function safeNext(next: string | null, origin: string): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.includes("\\")) return "/";
  try {
    const target = new URL(next, origin);
    return target.origin === origin ? `${target.pathname}${target.search}${target.hash}` : "/";
  } catch {
    return "/";
  }
}
