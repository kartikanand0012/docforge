import { describe, expect, it } from "vitest";
import { mergeFresh } from "@/lib/rows";

const row = (id: string, stage: string) => ({ id, stage });

describe("mergeFresh", () => {
  it("updates the rows it has fresh copies of and keeps the older pages loaded", () => {
    const shown = [row("c", "parsing"), row("b", "ready"), row("a", "ready")];
    const fresh = [row("d", "stored"), row("c", "ready")];

    expect(mergeFresh(shown, fresh)).toEqual([row("d", "stored"), row("c", "ready"), row("b", "ready"), row("a", "ready")]);
  });

  it("never lists a row twice", () => {
    const merged = mergeFresh([row("a", "ready")], [row("a", "ready"), row("a", "ready")]);
    expect(merged).toEqual([row("a", "ready")]);
  });
});
