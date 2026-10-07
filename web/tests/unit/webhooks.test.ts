import { describe, expect, it } from "vitest";
import { DELIVERY_TEXT, maskedUrl, nextAttemptText } from "@/lib/webhooks";

describe("webhooks on screen", () => {
  it("hide a URL's path and query, either of which can hold a receiver's token", () => {
    expect(maskedUrl("https://erp.example.com/hooks?token=abc")).toBe("https://erp.example.com/…");
    expect(maskedUrl("https://erp.example.com/hooks")).toBe("https://erp.example.com/…");
  });

  it("say when the next attempt is due, roughly", () => {
    const now = new Date("2026-10-08T10:00:00Z");
    expect(nextAttemptText("2026-10-08T10:03:00Z", now)).toBe("about 3 minutes");
    expect(nextAttemptText("2026-10-08T10:00:20Z", now)).toBe("in under a minute");
    expect(nextAttemptText("2026-10-08T09:59:00Z", now)).toBe("due now");
    expect(nextAttemptText(null, now)).toBe("");
  });

  it("name every delivery status", () => {
    expect(Object.keys(DELIVERY_TEXT).sort()).toEqual(["delivered", "failed", "pending"]);
  });
});

describe("review findings", () => {
  it("hides a path that can be a token, and any user name and password", () => {
    expect(maskedUrl("https://hooks.slack.com/services/T00/B00/XXXX")).toBe("https://hooks.slack.com/…");
    expect(maskedUrl("https://user:pass@erp.example.com/hooks?x=1")).toBe("https://erp.example.com/…");
    expect(maskedUrl("https://erp.example.com/")).toBe("https://erp.example.com/");
    expect(maskedUrl("not a url")).toBe("not a url");
  });
});
