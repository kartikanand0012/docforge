"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { ACCEPT } from "@/lib/formats";

export default function UploadPage() {
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const file = form.get("file");
    if (!(file instanceof File) || file.size === 0) {
      setError("Choose a file first.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await api.upload(file, String(form.get("doc_type")));
      router.push(`/documents/${result.document.id}`);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <>
      <h1>Upload a document</h1>
      <form className="card" onSubmit={submit} style={{ maxWidth: 520 }}>
        <label htmlFor="doc_type">Document type</label>
        <select id="doc_type" name="doc_type" defaultValue="invoice">
          <option value="invoice">Invoice</option>
          <option value="purchase_order">Purchase order</option>
          <option value="coa">Certificate of analysis</option>
          <option value="general">General document (read and indexed for search and chat, nothing extracted)</option>
        </select>
        <label htmlFor="file">PDF (born-digital or scanned), Word, PowerPoint, Excel, or a photo or scan as PNG, JPEG or TIFF (up to 10 MB, 20 pages)</label>
        <input id="file" name="file" type="file" accept={ACCEPT} />
        {error && <p className="error" role="alert">{error}</p>}
        <p>
          <button className="primary" type="submit" disabled={busy}>
            {busy ? "Uploading…" : "Upload"}
          </button>
        </p>
      </form>
    </>
  );
}
