import { describe, expect, it } from "vitest";
import { snippet } from "@/lib/snippet";

const long = `${"Purpose and scope of the procedure. ".repeat(12)}Record the temperature of cold-chain goods; anything above 8 °C is rejected. ${"Retention rules follow. ".repeat(10)}`;

describe("snippet", () => {
  it("returns a short text as it is", () => {
    expect(snippet("Grand total 1,200.00", "total", 300)).toBe("Grand total 1,200.00");
  });

  it("shows the part of a long text where the question's words are", () => {
    const shown = snippet(long, "What happens to goods above 8 °C?", 300);
    expect(shown).toContain("above 8 °C is rejected");
    expect(shown.length).toBeLessThanOrEqual(302);
    expect(shown.startsWith("…")).toBe(true);
  });

  it("starts from the beginning when no word of the question is in the text", () => {
    const shown = snippet(long, "invoice number", 300);
    expect(shown.startsWith("Purpose")).toBe(true);
    expect(shown.endsWith("…")).toBe(true);
  });

  it("ignores short and common words", () => {
    expect(snippet(long, "what is the", 300).startsWith("Purpose")).toBe(true);
  });
});
