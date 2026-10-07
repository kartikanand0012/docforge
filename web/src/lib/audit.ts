/** The audit-log screen's wording and filters, kept apart from the page so they are tested. */

export type AuditFilters = { action?: string; actor?: string; target_type?: string; target_id?: string; from?: string; to?: string };

export type ChainReport = { consistent: boolean; entries: number; first_bad_id: number | null; reason: string | null; anchors_checked: number };

const KEYS = ["action", "actor", "target_type", "target_id", "from", "to"] as const;

/** Filters as a query string: shareable between administrators. Empty ones are left out. */
export function filtersToQuery(filters: AuditFilters): string {
  const query = new URLSearchParams();
  for (const key of KEYS) {
    const value = filters[key];
    if (value) query.set(key, value);
  }
  return query.toString();
}

export function filtersFromQuery(query: URLSearchParams): AuditFilters {
  const filters: AuditFilters = {};
  for (const key of KEYS) {
    const value = query.get(key);
    if (value) filters[key] = value;
  }
  return filters;
}

/** Local days (YYYY-MM-DD) as instants: from the start of the first, to the start of the day
 * after the last, so the last day is included whole, in the browser's own time zone. */
export function localDayRange(fromDay: string, toDay: string): { from?: string; to?: string } {
  const start = (day: string, plus = 0) => {
    const [y, m, d] = day.split("-").map(Number);
    return new Date(y, m - 1, d + plus).toISOString();
  };
  return { from: fromDay ? start(fromDay) : undefined, to: toDay ? start(toDay, 1) : undefined };
}

/** The local days a range in the URL stands for: the reverse of `localDayRange`. */
export function localDaysFromRange(from?: string, to?: string): { fromDay: string; toDay: string } {
  const day = (instant: string, minus = 0) => {
    const d = new Date(instant);
    d.setDate(d.getDate() - minus);
    const pad = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  };
  return { fromDay: from ? day(from) : "", toDay: to ? day(to, 1) : "" };
}

/** What the chain check found, in words, with the limits `audit.py` itself states. */
export function chainNote(report: ChainReport): string {
  if (!report.consistent) return `Broken at entry ${report.first_bad_id}: ${report.reason ?? "it does not match"}.`;
  const count = report.entries.toLocaleString("en-US");
  if (report.anchors_checked > 0) {
    return `Verified: ${count} entries, chain intact, and it still holds the entry anchored outside the database.`;
  }
  return (
    `Verified: ${count} entries, chain intact. No outside anchor yet: a rewrite with every hash ` +
    "recomputed, or entries removed from the end, would not be detected."
  );
}

/** An entry's shown details as text, and how many were held back. */
export function detailsText(details: Record<string, unknown>, hidden: number): string {
  const parts = Object.entries(details).map(
    ([key, value]) => `${key}: ${value !== null && typeof value === "object" ? JSON.stringify(value) : String(value)}`,
  );
  if (hidden) parts.push(hidden === 1 ? "1 detail hidden" : `${hidden} details hidden`);
  return parts.join(" · ");
}
