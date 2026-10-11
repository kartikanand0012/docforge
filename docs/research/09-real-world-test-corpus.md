# Real-world test data and load testing

Researched 2026-10-08 (web search; nothing downloaded). For the stage after C16: verify
DocForge on real-world documents, then load and stress test it. Every claim below has a
source; where a licence could not be confirmed from a primary source, it says so. Licences
must be re-checked at the source before any file is committed.

## 1. What clients send, and what breaks extraction

**Accounts payable (Indian GST).** Tax invoices carry about 16 fields under CGST Rule 46:
GSTIN, HSN/SAC, place of supply, a CGST/SGST or IGST split, a sequential number of up to 16
characters, a signature ([incorpx](https://www.incorpx.io/blog/gst-invoice-rules-format-mandatory-fields),
[startuptalky](https://startuptalky.com/gst-invoice-mandatory-fields/)). E-invoices add a
64-character IRN and a signed QR code; without a valid IRN an invoice is not a valid tax
invoice under Rule 48(5) ([Masters India](https://www.mastersindia.co/blog/e-invoice-generation-compliance-guide/),
[invoicedataextraction](https://invoicedataextraction.com/blog/verify-irn-on-gst-invoice)).
Reading the QR payload, not OCR, is the reliable way to the IRN and totals. Delivery challans,
goods receipt notes, credit and debit notes and purchase orders share the layouts.

What breaks: line-item tables across pages with merged cells and multi-row items; phone photos
with skew, shadows and blur; stamps, signatures and staple holes; handwritten corrections
([arXiv 2507.07029](https://arxiv.org/html/2507.07029v1),
[lido](https://www.lido.app/blog/how-to-process-scanned-faxed-and-mobile-photo-invoices-accurately),
[veryfi](https://www.veryfi.com/technology/real-world-document-processing-challenges/)); and as
quality drops, systems start inventing values rather than only missing them
([llamaindex](https://www.llamaindex.ai/glossary/multi-page-document-processing)).
India-specific (inferred, no source): several invoices merged in one PDF; Hindi and English
mixed; lakh grouping (1,00,000).

**Pharma certificates of analysis.** Typically 200-400 a month from 50-100 suppliers, each
with its own layout ([kognitos](https://www.kognitos.com/blog/certificate-of-analysis/)).
What breaks: specification and result tables, units, "complies" against numeric results, on
native PDFs and scans ([algoscale](https://documentiq.algoscale.com/blog/automating-certificate-of-analysis-coa-extraction-pharma-chemical-manufacturing)).
No public CoA dataset exists; nor do public batch records or drug licences.

**Bank statements**: tables across pages, running balances, layouts by bank (Bankstatemently
lists 37 documented challenges). **Contracts and NDAs**: long, many pages
([Kleister](https://arxiv.org/pdf/2105.05796)). **KYC ids**: glare and perspective in photos
([MIDV-500](https://arxiv.org/pdf/1807.05786)). Logistics, healthcare, insurance: no sources
found; client samples or synthetic data needed.

## 2. Public datasets

| Dataset | Size / type | Licence | Commercial use | In a public repo |
| --- | --- | --- | --- | --- |
| [CORD](https://github.com/clovaai/cord) | 1,000 receipts, 30-42 field classes | CC BY 4.0 | Yes | Yes, with attribution |
| [katanaml invoices-donut-data-v1](https://huggingface.co/datasets/katanaml-org/invoices-donut-data-v1) | 501 invoice images, header and line JSON | MIT | Yes | Yes |
| [DocLayNet](https://huggingface.co/datasets/docling-project/DocLayNet) | 80,863 pages (reports, law, tenders, manuals) | CDLA-Permissive-1.0 | Yes | Yes |
| [PubLayNet](https://github.com/ibm-aur-nlp/PubLayNet) | 300k+ medical paper pages | CDLA-Permissive (annotations); images under PMC Open Access | Mostly | Check each image |
| [CUAD](https://www.atticusprojectai.org/cuad) | 510 contracts, 13k clause labels | CC BY 4.0 | Yes | Yes |
| [AgamiAI Indian-Bank-Statements](https://huggingface.co/datasets/AgamiAI/Indian-Bank-Statements) | Synthetic scanned PDFs + JSON | Apache-2.0 | Yes | Yes |
| [AgamiAI Indian-Income-Tax-Returns](https://huggingface.co/datasets/AgamiAI/Indian-Income-Tax-Returns) | ~200, English and Hindi | Apache-2.0 | Yes | Yes |
| Bankstatemently Open Benchmark | 5 synthetic statements, 14 edge cases | MIT (reported) | Yes | After checking |
| [SROIE](https://arxiv.org/abs/2103.10213) | 1,000 receipts | Unclear (mirrors disagree) | Unclear | No |
| [FUNSD](https://guillaumejaume.github.io/FUNSD/work/) | 199 forms | Non-commercial | No | No |
| XFUND | Forms, 7 languages (no Hindi) | CC BY-NC-SA 4.0 | No | No |
| [RealKIE](https://indicodatasolutions.github.io/RealKIE/) | FCC invoices, NDAs, charity reports | CC BY-NC 4.0 | No | No |
| [RVL-CDIP](https://huggingface.co/datasets/aharley/rvl_cdip) | 400k images, 16 classes | UCSF archive terms | Unclear | No |
| [DocVQA](https://www.docvqa.org/datasets/docvqa) | 12,767 images, 50k questions | RRC terms (mirror licences not authoritative) | Unclear | No |
| Kleister NDA / Charity | 540 NDAs; 2,778 charity reports | No licence file | Unclear | No (local only) |

**Gap:** no permissively licensed set of real Indian GST invoices or pharma CoAs exists. Both
need synthetic generation, or client samples under an NDA.

## 3. Adversarial and edge files

- [govdocs1](https://digitalcorpora.org/corpora/files/): about 1M real .gov files ("freely
  redistributable to the best of our knowledge"); 1,000-file subsets suit an odd-format sweep.
- [SafeDocs CC-MAIN-2021-31-PDF-UNTRUNCATED](https://digitalcorpora.org/cc-main-2021-31-pdf-untruncated/):
  about 8M real PDFs with metadata; sample encrypted, huge and broken ones (mixed copyright:
  keep out of the repo).
- Apache Tika / PDFBox / POI regression corpus and POI's known-corrupt Office files.
- pdf.js `test/pdfs` (each PDF has its own licence), the OPF "PDF Cabinet of Horrors", the
  veraPDF corpus (CC-BY-4), the PDF Association's stressful PDF corpus.
- **Bombs, encrypted and huge files are generated in the repo**, not downloaded: nested
  Flate and xref-stream bombs, a zip-bomb DOCX/XLSX, owner- and user-password PDFs, a
  5,000-page PDF. pypdf has had decompression-bomb fixes up to 6.18.1 (the advisories named
  were not verified here); the lock has 6.19.0, and the floor in `pyproject.toml` (4.2) should
  be raised to match.

## 4. Load and stress testing

- **Tools:** k6 (`constant-arrival-rate`, `ramping-arrival-rate`: an open model, so uploads
  keep arriving when the system slows - right for bursts;
  [Grafana](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/open-vs-closed/));
  Locust (Python; scripts "upload, then poll until done" and multi-step chat sessions).
- **Measure:** upload p50/p95/p99 and errors; time to done per document and per page; pages
  per minute; queue depth and oldest job's age; worker memory and OOM kills; database
  connections and lock waits; model provider 429s and retries; webhook lag; behaviour under
  burst (clean back-off, no duplicate processing, recovery after a worker is killed).
- **Profiles (inferred):** a business-day trickle; a month-end and GST-filing spike (GSTR-1 is
  due around the 11th) at 5-10x for 2-4 hours; a backfill of 10k files; a size mix (mostly
  1-3 page invoices, a tail of large reports and spreadsheets); chat alongside ingestion; an
  8-hour soak for leaks.
- **Published figures** (vendor, self-reported): 1-15 s per page, 85-99% field accuracy,
  about 85% on non-standard scans. Rough targets, not proof.
- **The model provider's rate limits** will likely cap throughput before our workers: load
  tests of the infrastructure replay or mock the model; real-model runs are separate.

## Recommended corpus, in order

1. katanaml invoices-donut (MIT) and CORD (CC BY 4.0): invoice and receipt fields, committable
   with attribution.
2. AgamiAI Indian bank statements and ITRs (Apache-2.0): Indian tables and Hindi text.
3. CUAD (CC BY 4.0) and DocLayNet (CDLA-P): general Q&A, citations, long layouts.
4. A generated set we own: Indian GST invoices and e-invoices with real-format IRN QR codes,
   delivery challans, credit notes and CoAs, each rendered clean, scanned at 150 dpi, as a
   phone photo, rotated, stamped, merged several to a PDF, with Hindi labels, and
   password-protected.
5. Robustness: a govdocs1 subset, a SafeDocs sample, Tika/POI corrupt files (local, ignored by
   git).
6. Evaluation only, never committed or used commercially: SROIE, FUNSD, XFUND, RealKIE,
   Kleister, DocVQA, RVL-CDIP.

## Open risks

- No real Indian GST invoices or CoAs under a usable licence: synthetic data may hide layout
  quirks; a pilot client's samples under NDA are the real test.
- Mirror licences are often wrong: trust primary sources and record each in `DATASETS.md`.
- No Hindi or Devanagari invoice benchmark exists: Indic accuracy cannot be measured until
  one is built.
- Even public scans can hold names and signatures: committed fixtures stay synthetic.
