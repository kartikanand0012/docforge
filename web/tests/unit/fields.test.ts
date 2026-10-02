import { describe, expect, it } from "vitest";
import { fieldAt, label, section } from "@/lib/fields";

describe("label", () => {
  it.each([
    ["invoice_no", "Invoice no."],
    ["lines[2].batch_no", "Line 3 · Batch no."],
    ["lines[0].ptr", "Line 1 · PTR"],
    ["seller.gstin", "Seller · GSTIN"],
    ["seller.drug_licence_nos[0]", "Seller · Drug licence nos 1"],
    ["totals.grand_total", "Totals · Grand total"],
  ])("%s reads as %s", (path, expected) => {
    expect(label(path)).toBe(expected);
  });
});

describe("section", () => {
  it("groups by line, party, totals or the document", () => {
    expect(section("lines[11].qty")).toBe("Line 12");
    expect(section("buyer.name")).toBe("Buyer");
    expect(section("totals.cgst")).toBe("Totals");
    expect(section("po_no")).toBe("Document");
  });
});

describe("fieldAt", () => {
  const record = {
    invoice_no: { value: "A-1", raw: "A-1", block_ids: ["b2"] },
    lines: [{ qty: { value: 20, raw: "20", block_ids: ["b9"] } }],
    seller: { drug_licence_nos: [{ value: "L1", raw: "L1", block_ids: [] }] },
  };

  it("finds nested fields", () => {
    expect(fieldAt(record, "invoice_no")?.raw).toBe("A-1");
    expect(fieldAt(record, "lines[0].qty")?.raw).toBe("20");
    expect(fieldAt(record, "seller.drug_licence_nos[0]")?.raw).toBe("L1");
  });

  it("returns undefined for paths that lead nowhere", () => {
    expect(fieldAt(record, "lines[5].qty")).toBeUndefined();
    expect(fieldAt(record, "lines")).toBeUndefined();
    expect(fieldAt(record, "nope.deeper")).toBeUndefined();
    expect(fieldAt(record, "lines[0].qty.value")).toBeUndefined();
  });
});
