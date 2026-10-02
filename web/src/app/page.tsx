"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, type QueueItem } from "@/lib/api";

export default function QueuePage() {
  const [items, setItems] = useState<QueueItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.queue().then(setItems, (e: Error) => setError(e.message));
  }, []);

  return (
    <>
      <h1>Review queue</h1>
      {error && <p className="error" role="alert">{error}</p>}
      {items === null && !error && <p className="muted">Loading…</p>}
      {items?.length === 0 && <p className="card">Nothing is waiting for review.</p>}
      {items && items.length > 0 && (
        <div className="card">
          <table>
            <thead>
              <tr>
                <th scope="col">Document</th>
                <th scope="col">Type</th>
                <th scope="col">Order match</th>
                <th scope="col">Why it needs a person</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.document_id}>
                  <td>
                    <Link href={`/documents/${item.document_id}`}>{item.filename}</Link>
                    <div className="muted">version {item.version_no}</div>
                  </td>
                  <td>{item.doc_type.replace("_", " ")}</td>
                  <td>
                    <span className={`chip ${item.match_status}`}>{item.match_status.replace("_", " ")}</span>
                  </td>
                  <td>
                    <ul>
                      {item.reasons.map((reason) => (
                        <li key={reason}>{reason}</li>
                      ))}
                    </ul>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
