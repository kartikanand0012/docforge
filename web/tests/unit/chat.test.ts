import { describe, expect, it } from "vitest";
import { citationMarks, statusNote } from "@/lib/chat";

describe("statusNote", () => {
  it("says nothing extra for a fully supported answer", () => {
    expect(statusNote("supported", 0)).toBeNull();
  });

  it("says how many quotes were left out of a partly supported answer", () => {
    expect(statusNote("partly_supported", 2)).toBe("2 quotes could not be found in the documents and were left out.");
    expect(statusNote("partly_supported", 1)).toBe("1 quote could not be found in the documents and was left out.");
  });

  it("explains a withheld answer and one the documents do not hold", () => {
    expect(statusNote("unsupported", 3)).toMatch(/not shown/);
    expect(statusNote("not_found", 0)).toMatch(/not in the documents/);
  });
});

describe("citationMarks", () => {
  const box = { page: 1, x0: 0, y0: 742, x1: 306, y1: 792, page_width: 612, page_height: 792 };

  it("places each box of a citation on its page, from the top-left", () => {
    const marks = citationMarks([box], 1);
    expect(marks).toEqual([{ left: "0.000%", top: "0.000%", width: "50.000%", height: "6.313%" }]);
  });

  it("leaves out boxes of other pages and boxes without a page size", () => {
    expect(citationMarks([{ ...box, page: 2 }], 1)).toEqual([]);
    expect(citationMarks([{ ...box, page_width: 0 }], 1)).toEqual([]);
  });
});
