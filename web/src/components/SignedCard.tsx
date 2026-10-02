"use client";

import type { Signed } from "@/lib/api";

/** The signed decision, whether the record still matches its signature, and the draft it produced. */
export default function SignedCard({ review, valid }: { review: Signed; valid: boolean | null }) {
  const download = () => {
    const blob = new Blob([JSON.stringify(review.draft, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `payment-approval-${String(review.draft?.invoice_no ?? "draft").replace(/[^\w.-]+/g, "_")}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="card" aria-label="Signed review">
      <h2>
        <span className={`chip ${review.outcome}`}>{review.outcome}</span> by {review.reviewer_name}
      </h2>
      <p>
        “{review.meaning}” · {new Date(review.signed_at).toLocaleString()}
      </p>
      <p>Reason: {review.reason}</p>
      {review.override_reason && <p className="notice warn">Approved despite open checks: {review.override_reason}</p>}
      <p className={valid ? "notice ok" : "notice bad"}>
        {valid ? "The record matches what was signed." : "The record no longer matches its signature."}
        <br />
        <span className="value">SHA-256 {review.record_sha256}</span>
      </p>
      {review.draft && (
        <>
          <h2>Payment approval draft</h2>
          <p className="muted">Nothing is paid here: this draft is for a person or a payment system to act on.</p>
          <table>
            <tbody>
              {[
                ["Payee", `${(review.draft.payee as { name?: string })?.name ?? ""} (${(review.draft.payee as { gstin?: string })?.gstin ?? ""})`],
                ["Invoice", String(review.draft.invoice_no ?? "")],
                ["Invoice date", String(review.draft.invoice_date ?? "")],
                ["Amount", `${String(review.draft.amount ?? "")} ${String(review.draft.currency ?? "")}`],
                ["Purchase order", String(review.draft.po_no ?? "")],
                ["Status", String(review.draft.status ?? "")],
              ].map(([name, value]) => (
                <tr key={name}>
                  <th scope="row">{name}</th>
                  <td className="value">{value}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p>
            <button type="button" onClick={download}>
              Download draft (JSON)
            </button>
          </p>
        </>
      )}
    </div>
  );
}
