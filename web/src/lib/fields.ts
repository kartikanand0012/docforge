/** Turning field paths such as `lines[0].batch_no` into labels and reading their values. */

export type Extracted = { value: unknown; raw: string | null; block_ids: string[] };

const WORDS: Record<string, string> = {
  po_no: "PO no.",
  po_date: "PO date",
  gstin: "GSTIN",
  hsn: "HSN",
  mrp: "MRP",
  ptr: "PTR",
  qty: "Quantity",
  free_qty: "Free quantity",
  discount_pct: "Discount %",
  gst_rate: "GST %",
  cgst: "CGST",
  sgst: "SGST",
  igst: "IGST",
  mfg: "Manufactured",
  no: "no.",
};

function word(part: string): string {
  if (WORDS[part]) return WORDS[part];
  const text = part.replaceAll("_", " ").replace(/\bno\b/, "no.");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** `lines[2].batch_no` -> `Line 3 · Batch no.`; `seller.drug_licence_nos[0]` -> `Seller · Drug licence nos 1`. */
export function label(path: string): string {
  return path
    .split(".")
    .map((segment) => {
      const match = /^([a-z_]+)\[(\d+)\]$/.exec(segment);
      if (!match) return word(segment);
      const name = match[1] === "lines" ? "Line" : word(match[1]);
      return `${name} ${Number(match[2]) + 1}`;
    })
    .join(" · ");
}

/** The section a path belongs to, for grouping the field list. */
export function section(path: string): string {
  if (path.startsWith("lines[")) return `Line ${Number(/^lines\[(\d+)\]/.exec(path)?.[1] ?? 0) + 1}`;
  const first = path.split(".")[0];
  if (["seller", "buyer", "totals"].includes(first)) return word(first);
  return "Document";
}

/** The field at `path` in a record, or undefined if the path does not lead to one. */
export function fieldAt(record: unknown, path: string): Extracted | undefined {
  let node: unknown = record;
  for (const segment of path.split(".")) {
    const match = /^([A-Za-z_]+)(?:\[(\d+)\])?$/.exec(segment);
    if (!match || typeof node !== "object" || node === null) return undefined;
    node = (node as Record<string, unknown>)[match[1]];
    if (match[2] !== undefined) {
      if (!Array.isArray(node)) return undefined;
      node = node[Number(match[2])];
    }
  }
  if (typeof node === "object" && node !== null && "raw" in node && "block_ids" in node) {
    return node as Extracted;
  }
  return undefined;
}
