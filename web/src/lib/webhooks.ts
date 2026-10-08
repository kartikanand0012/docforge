/** The webhooks screen's wording, kept apart from the page so it is tested. */

/** A URL as it is safe to show: the scheme and host, with the path and query hidden (either
 * can carry a receiver's token, as Slack's and Teams' URLs do) and no user name or password. */
export function maskedUrl(url: string): string {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return url;
  }
  const hidden = parsed.pathname.length > 1 || parsed.search !== "";
  return `${parsed.protocol}//${parsed.host}/${hidden ? "…" : ""}`;
}

/** When the next attempt is due, roughly: the queue decides the exact moment. */
export function nextAttemptText(at: string | null, now: Date = new Date()): string {
  if (!at) return "";
  const seconds = (new Date(at).getTime() - now.getTime()) / 1000;
  if (seconds <= 0) return "due now";
  if (seconds < 60) return "in under a minute";
  const minutes = Math.round(seconds / 60);
  return `about ${minutes} minute${minutes === 1 ? "" : "s"}`;
}

export const DELIVERY_TEXT: Record<string, string> = {
  pending: "Waiting",
  delivered: "Delivered",
  failed: "Failed",
};
