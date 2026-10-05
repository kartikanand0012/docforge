import { describe, expect, it } from "vitest";
import { safeNext } from "@/lib/next";

describe("safeNext", () => {
  const origin = "https://review.example.com";

  it.each([
    ["/documents/1", "/documents/1"],
    ["/evals?x=1", "/evals?x=1"],
    [null, "/"],
    ["", "/"],
    ["//evil.example", "/"],
    ["/\\evil.example", "/"],
    ["https://evil.example/", "/"],
    ["javascript:alert(1)", "/"],
    ["/%5C%5Cevil.example", "/%5C%5Cevil.example"],
  ])("%s goes to %s", (next, expected) => {
    expect(safeNext(next, origin)).toBe(expected);
  });
});
