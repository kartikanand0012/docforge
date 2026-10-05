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

  it("says both when statements and quotes were left out", () => {
    expect(statusNote("partly_supported", 2, 3)).toBe(
      "3 statements could not be checked against the documents and were left out. 2 quotes could not be found in the documents and were left out.",
    );
  });

  it("says how many statements were left out for a figure no quote holds", () => {
    expect(statusNote("partly_supported", 0, 1)).toBe("1 statement could not be checked against the documents and was left out.");
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

import { sendsOnEnter } from "@/lib/chat";

describe("sendsOnEnter", () => {
  it("sends on Enter, not on Shift+Enter", () => {
    expect(sendsOnEnter({ key: "Enter", shiftKey: false, isComposing: false, keyCode: 13 })).toBe(true);
    expect(sendsOnEnter({ key: "Enter", shiftKey: true, isComposing: false, keyCode: 13 })).toBe(false);
  });

  it("does not send while an input method is composing a word", () => {
    expect(sendsOnEnter({ key: "Enter", shiftKey: false, isComposing: true, keyCode: 13 })).toBe(false);
    expect(sendsOnEnter({ key: "Enter", shiftKey: false, isComposing: false, keyCode: 229 })).toBe(false);
  });
});

import { reasonNote } from "@/lib/chat";

describe("reasonNote", () => {
  it("says why a question went unanswered, in words", () => {
    expect(reasonNote("no_passages", { scope: "collection" })).toBe("Nothing in this knowledge base matched the question.");
    expect(reasonNote("not_in_passages", { missing: "the bank account number", documents: ["a.pdf", "b.pdf"] })).toBe(
      "The documents read (a.pdf, b.pdf) do not give the bank account number.",
    );
    expect(reasonNote("figures_not_in_quotes", {})).toMatch(/figures/);
    expect(reasonNote(null, {})).toBeNull();
  });

  it("says when passages were held back for reading like instructions", () => {
    expect(reasonNote(null, { held_back: 1 })).toBe("1 passage was held back because it reads like instructions to the AI, not document content.");
  });
});
