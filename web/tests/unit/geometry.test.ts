import { describe, expect, it } from "vitest";
import { toOverlay } from "@/lib/geometry";

const page = { width: 600, height: 800 };

describe("toOverlay", () => {
  it("turns a bottom-left box in points into top-left percentages", () => {
    // 60 pt from the left, 40 pt tall, its top 100 pt below the top edge.
    const overlay = toOverlay({ page: 1, x0: 60, y0: 660, x1: 180, y1: 700 }, page);

    expect(overlay).toEqual({ left: "10.000%", top: "12.500%", width: "20.000%", height: "5.000%" });
  });

  it("keeps a box touching the bottom-left corner at the bottom-left", () => {
    const overlay = toOverlay({ page: 1, x0: 0, y0: 0, x1: 6, y1: 8 }, page);

    expect(overlay.left).toBe("0.000%");
    expect(overlay.top).toBe("99.000%");
  });

  it("clamps a box that runs off the page", () => {
    const overlay = toOverlay({ page: 1, x0: -10, y0: 790, x1: 700, y1: 820 }, page);

    expect(overlay.left).toBe("0.000%");
    expect(overlay.top).toBe("0.000%");
    expect(overlay.width).toBe("100.000%");
  });

  it("refuses a page without a size", () => {
    expect(() => toOverlay({ page: 1, x0: 0, y0: 0, x1: 1, y1: 1 }, { width: 0, height: 10 })).toThrow();
  });
});
