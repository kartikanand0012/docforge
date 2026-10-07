/** The webhooks screen's wording, kept apart from the page so it is tested. */

/** A URL with its query hidden: a query can carry a receiver's own token. */
export function maskedUrl(url: string): string {
  const at = url.indexOf("?");
  return at < 0 ? url : `${url.slice(0, at)}?…`;
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
