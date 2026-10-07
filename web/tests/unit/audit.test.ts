import { describe, expect, it } from "vitest";
import { chainNote, detailsText, filtersFromQuery, filtersToQuery, localDayRange } from "@/lib/audit";

describe("audit log filters", () => {
  it("go to the URL and back unchanged, empty ones left out", () => {
    const filters = { action: "review.signed", actor: "reviewer:1", from: "2026-10-01", to: "2026-10-08" };
    const query = filtersToQuery(filters);
    expect(query).toBe("action=review.signed&actor=reviewer%3A1&from=2026-10-01&to=2026-10-08");
    expect(filtersFromQuery(new URLSearchParams(query))).toEqual(filters);
    expect(filtersToQuery({ action: "", actor: "" })).toBe("");
  });

  it("turn local days into instants: from the start of the first, to the start of the day after the last", () => {
    const { from, to } = localDayRange("2026-10-01", "2026-10-08");
    expect(new Date(from!).getTime()).toBe(new Date(2026, 9, 1).getTime());
    expect(new Date(to!).getTime()).toBe(new Date(2026, 9, 9).getTime());
    expect(localDayRange("", "")).toEqual({ from: undefined, to: undefined });
  });
});

describe("the chain check", () => {
  it("says verified, verified against an outside anchor, or broken at an entry", () => {
    expect(chainNote({ consistent: true, entries: 12345, first_bad_id: null, reason: null, anchors_checked: 0 })).toMatch(
      /^Verified: 12,345 entries, chain intact\. No outside anchor yet/,
    );
    expect(chainNote({ consistent: true, entries: 3, first_bad_id: null, reason: null, anchors_checked: 1 })).toBe(
      "Verified: 3 entries, chain intact, and it still holds the entry anchored outside the database.",
    );
    expect(chainNote({ consistent: false, entries: 9, first_bad_id: 7, reason: "hash mismatch", anchors_checked: 0 })).toBe(
      "Broken at entry 7: hash mismatch.",
    );
  });
});

describe("details", () => {
  it("read as key: value, with how many were held back", () => {
    expect(detailsText({ outcome: "approved", version_no: 2 }, 1)).toBe("outcome: approved · version_no: 2 · 1 detail hidden");
    expect(detailsText({}, 2)).toBe("2 details hidden");
    expect(detailsText({}, 0)).toBe("");
  });
});
