# DocForge research 03: components, references, stack (2026-10-01)

Method: GitHub stars/licence/last-push pulled live via `gh api` on 2026-10-01 (marked [gh]). Everything else comes from web search/fetch summaries; secondary sources (blogs, aggregator pages) are marked [unverified] where I could not confirm against a primary source. Licences are not legal advice: get counsel to read the actual LICENSE files before shipping to a client.

## 1. Open-source parsing / OCR

| Component | Licence (code / weights) | Stars [gh] | Last push [gh] | Notes |
|---|---|---|---|---|
| Docling https://github.com/docling-project/docling | MIT / weights Apache-2.0, MIT, CDLA (permissive) | 68,258 | 2026-10-01 | Safest licence profile. TableFormer tables, "heron" layout model default since v2.50 [unverified]. Granite-Docling-258M VLM (Apache-2.0, https://huggingface.co/ibm-granite/granite-docling-258M) reports TEDS-struct 0.97 / TEDS-content 0.96 on FinTabNet [unverified, vendor claim]. CPU-viable; ~2.1 pages/s on B200 per third-party [unverified]. Weak on scans vs VLM OCRs: 50.3 on olmOCR-bench overall (64.0 on born-digital) [unverified]. Docling-serve: https://github.com/docling-project/docling-serve (MIT, 1,844 stars). Docling-eval: https://github.com/docling-project/docling-eval (MIT, 77 stars). Donated to Linux Foundation AAIF [unverified]. |
| Marker https://github.com/datalab-to/marker | Code Apache-2.0 (was GPL-3.0 until ~July 2026 [unverified]) / **weights modified OpenRAIL-M: restricted** | 40,148 | 2026-09-13 | **FLAG: weights free only under ~$5M revenue AND funding, and not for competing products; otherwise paid Datalab licence.** Client may exceed the threshold. 76.0 olmOCR-bench, 2.9 p/s balanced GPU, 7.4 p/s fast [vendor claim]. https://particula.tech/blog/docling-vs-mineru-vs-marker-pdf-parser |
| Surya https://github.com/datalab-to/surya | Apache-2.0 code / **same restricted OpenRAIL-M weights** | 21,434 | 2026-09-11 | Same weight flag as Marker. OCR/layout/table detection building block. |
| Chandra OCR 2 https://github.com/datalab-to/chandra | Apache-2.0 code / **OpenRAIL-M weights (restricted)** | 12,350 | 2026-06-26 | Top olmOCR-bench score 85.8 [unverified]; same Datalab weight flag. |
| MinerU https://github.com/opendatalab/MinerU | **Custom "MinerU Open Source License" (Apache-2.0 + extra terms)**: commercial licence needed above 100M MAU or USD 20M monthly revenue, mandatory attribution [unverified, from third-party]; weights vary by backend, some untagged | 80,948 | 2026-09-30 | Strong on scientific docs, tables to HTML, formulas to LaTeX. MinerU2.5-Pro 95.75 on OmniDocBench v1.6 [unverified]; pipeline backend slow (0.54 p/s) and 72.7 olmOCR-bench [unverified]. Older PDF-Extract-Kit dependency is AGPL-3.0 [unverified]: audit dependency tree. Client thresholds are very high, but attribution and non-OSI licence need legal read. |
| PaddleOCR / PP-StructureV3 / PaddleOCR-VL https://github.com/PaddlePaddle/PaddleOCR | Apache-2.0 code; PaddleOCR-VL weights Apache-2.0 (https://huggingface.co/PaddlePaddle/PaddleOCR-VL) | 90,490 | 2026-09-16 | PaddleOCR-VL-1.6 (0.9B-1B) 96.34 OmniDocBench v1.6, ranked first [unverified, blog roundup]. 109 languages, tables, formulas, charts; vLLM serving; modest GPU. Paddle framework install is the pain point. PP-OCR v6 tiny CPU models. https://blog.roboflow.com/best-open-source-ocr-models/ |
| GLM-OCR https://github.com/zai-org/GLM-OCR | Apache-2.0 code, MIT weights [unverified] | 7,481 | 2026-04-21 | 0.9B, 95.22 OmniDocBench v1.6 [unverified]. Newer entrant. |
| dots.ocr / dots.mocr https://github.com/rednote-hilab/dots.ocr | MIT (check weights) | 9,157 | 2026-03-24 | 3B; 83.9 olmOCR-bench [unverified]. |
| olmOCR https://github.com/allenai/olmocr | Apache-2.0 end to end | 19,685 | 2026-03-25 | 7B VLM, needs ~12 GB VRAM; 82.4 olmOCR-bench (olmOCR-2); 85.74 OmniDocBench v1.6 [unverified]. Slower, GPU-bound; good for batch scans. Bench data: https://huggingface.co/datasets/allenai/olmOCR-bench (ODC-BY). |
| DeepSeek-OCR https://github.com/deepseek-ai/DeepSeek-OCR | MIT | 23,925 | 2026-01-27 | Optical-compression research model; GPU. |
| Unstructured https://github.com/Unstructured-IO/unstructured | Apache-2.0 (OSS lib); hosted API paid | 15,522 | 2026-10-01 | Broad format coverage, partitioning/chunking helpers; OSS quality lags Docling/VLMs on tables. Heavy dependency footprint. |
| Tesseract https://github.com/tesseract-ocr/tesseract | Apache-2.0 | 76,783 | 2026-09-28 | CPU, fast, cheap, weak on layout/tables/handwriting. Pair with OCRmyPDF (MPL-2.0, 34,913 stars, https://github.com/ocrmypdf/OCRmyPDF) for searchable-PDF fallback. |
| pdfplumber https://github.com/jsvine/pdfplumber | MIT | 10,788 | 2026-08-06 | Good coordinates for born-digital PDFs and rule-based tables; no OCR. |
| pdfminer.six https://github.com/pdfminer/pdfminer.six | MIT | 7,033 | 2026-03-13 | Underlies pdfplumber. |
| **PyMuPDF / pymupdf4llm** https://github.com/pymupdf/PyMuPDF | **AGPL-3.0 (flagged)**; commercial licence from Artifex | 10,816 / 2,224 | 2026-09-30 | **FLAG: AGPL. Do not embed in a client deliverable or network service without Artifex commercial licence.** Use pypdfium2 (BSD/Apache, unverified) or pdfminer/pdfplumber instead. |
| Kreuzberg https://github.com/kreuzberg-dev/kreuzberg | MIT | 9,361 | 2026-10-01 | Rust-core multi-format extractor with Python bindings; fast, newer. Not evaluated in depth. |
| MarkItDown https://github.com/microsoft/markitdown | MIT | 187,858 | 2026-10-01 | Office/DOCX to Markdown; fine for DOCX path, not for layout/OCR. |
| LiteParse https://github.com/run-llama/liteparse | Apache-2.0 | 12,755 | 2026-10-01 | LlamaIndex local parser; not evaluated. |

Benchmarks to know: OmniDocBench (https://github.com/opendatalab/OmniDocBench, Apache-2.0, v1.6 Mar 2026); olmOCR-Bench (1,403 PDFs, 7,000+ unit tests); RealDocBench for field-level QA on regulated documents (https://arxiv.org/pdf/2606.07401, not read). Vendor-run numbers differ by benchmark; none of these include pharma labels or CoAs, so build our own eval set (section 7).

Key takeaways:
- Licence-safe tier: Docling, PaddleOCR(-VL), olmOCR, Tesseract, pdfplumber/pdfminer, Unstructured, GLM-OCR, Kreuzberg.
- Restricted: Marker/Surya/Chandra weights (revenue/funding cap), MinerU (custom licence). AGPL: PyMuPDF, Unstract (7,267 stars [gh], AGPL-3.0), ParadeDB (AGPL).
- Practical architecture: native-text path (pdfplumber/Docling) for born-digital; OCR only for pages with no/poor text layer; VLM parser (PaddleOCR-VL or hosted LLM) as fallback for hard tables and scans.

## 2. Hosted APIs

| Service | Price (first tier, per page unless noted) | Strengths / caveats |
|---|---|---|
| AWS Textract https://aws.amazon.com/textract/pricing/ | Detect text $0.0015; Tables $0.015; Forms $0.05; Queries $0.015; Signatures $0.0035; Expense $0.01; ID $0.025; Lending $0.07; text drops to $0.0006 after 1M/month (verified from AWS page) | Native AWS, IAM/VPC, no data leaves AWS; word/line bounding boxes give provenance for free; invoices via AnalyzeExpense. Layout quality below VLMs on complex tables. |
| Azure Document Intelligence | Read ~$1.50/1k; custom extraction ~$10/1k; commitment tier ~$0.53/1k [unverified] https://grooper.com/blog_posts/textract-vs-document-ai-vs-azure-vs-grooper/ | Strong prebuilt invoice/receipt models, good handwriting. Not AWS. |
| Google Document AI | Enterprise OCR ~$1.50/1k (to $0.60/1k at volume); layout parser ~$10/1k; specialised parsers per processor [unverified] https://aiproductivity.ai/blog/document-ai-cost-comparison/ | Good OCR, many processors. |
| Mistral OCR | $4/1k pages, $2/1k batch, ~$5/1k with annotations [unverified] | Cheap, markdown+tables, 85.66 OmniDocBench v1.6 [unverified]. Data goes to Mistral (EU). |
| Reducto https://llms.reducto.ai/best-document-processing-apis-2026 | Parse ~$10/1k, Extract ~$20/1k [unverified; vendor page] | Bounding-box citations, strong tables, enterprise/VPC options. Vendor marketing claims untested. |
| LlamaParse | $1.25/1k (Fast) to $56.25/1k (Agentic Plus); form-page surcharge Sep 2026 [unverified] | Tiered credits; costs vary with tier. |
| Claude (Sonnet 5 $2/$10 per M tokens, Opus 5.5 $4/$20, Haiku 4.5 $1/$5; batch -50%; cache -90%) [unverified, aggregator pricing as of Sep 2026: https://costgoat.com/pricing/claude-api] | tokens | Native PDF input, text citations with page numbers (https://platform.claude.com/docs/en/build-with-claude/citations); no bounding boxes (unverified). Available via Bedrock (https://aws.amazon.com/about-aws/whats-new/2025/06/citations-api-pdf-claude-models-amazon-bedrock/), fits AWS data-residency story. |
| Gemini Flash | 3.7 Flash $0.75/$3.75 per M intro through 2026-12-31, then $1.50/$7.50 [unverified: https://apidog.com/blog/gemini-3-7-flash-pricing-explained/] | Cheapest strong long-doc vision; can return box coordinates. |
| GPT-5 family | GPT-5 $1.25/$10, mini $0.25/$2 [unverified: https://pricepertoken.com/pricing-page/model/openai-gpt-5] | Newer 5.x models exist; check current. |

Rule of thumb: dedicated OCR/parse APIs cost $1.5-20 per 1k pages; an LLM page is roughly 1.5-3k input tokens, so extraction by LLM costs in the same order. Fallback tiering by confidence saves most of the cost.

## 3. Reference repositories

| # | Repo | Licence | Stars | Borrow | Avoid |
|---|---|---|---|---|---|
| 1 | ChunkyTortoise/docextract https://github.com/ChunkyTortoise/docextract | MIT | ~0 [gh] | Closest match to our target: FastAPI + ARQ worker + pgvector HNSW; 20 ADRs; eval-gate CI replaying deterministic fixtures at zero API cost; confidence-gated two-pass extraction; model circuit breaker; independent LLM judge. | Tiny and single-author; its own audit says live accuracy/cost are unmeasured, branch protection unenforced; Streamlit UI. Treat as design notes, not production code. |
| 2 | JSv4/OpenContracts https://github.com/JSv4/OpenContracts | MIT | 1,493 | Annotation/provenance-first document model, Django + Postgres + Celery, CI, docs, extraction with analyzers. | Django-centric; different stack from FastAPI. |
| 3 | docling-project/docling-serve https://github.com/docling-project/docling-serve | MIT | 1,844 | Async task API, result polling, Docker/K8s packaging for the parser itself; use as is or copy API shape. | Parser only, no extraction/search. |
| 4 | infiniflow/ragflow https://github.com/infiniflow/ragflow | Apache-2.0 | 91,581 | Deep-document-understanding pipeline, chunk templates, citations with source highlighting, template-based chunking UX. | Heavy (ES/MySQL/Redis/MinIO), monolith; too big to learn from end to end. |
| 5 | Zipstack/unstract https://github.com/Zipstack/unstract | **AGPL-3.0** | 7,267 | Prompt-studio UX, extraction workflow concepts, connectors. | **AGPL: read for ideas only, do not copy code or embed.** |
| 6 | paperless-ngx https://github.com/paperless-ngx/paperless-ngx | **GPL-3.0** | 46,215 | Mature ingestion queue, OCRmyPDF integration, consumer flow, tests, multi-user permissions. | GPL: ideas only. |
| 7 | CatchTheTornado/text-extract-api https://github.com/CatchTheTornado/text-extract-api | MIT | 3,182 | FastAPI + Celery + Redis OCR API with PII removal; simple. | Last push 2025-12; light on evals; Ollama-centric. |
| 8 | Cinnamon/kotaemon https://github.com/Cinnamon/kotaemon | Apache-2.0 | 25,795 | Document-QA UI with in-browser PDF citation highlighting. | Gradio UI; RAG not structured extraction. |
| 9 | docling-project/docling-graph https://github.com/docling-project/docling-graph | MIT | 910 | Docling output into LLM extraction with Pydantic templates. | Young, graph focus. |
| 10 | ucbepic/docetl https://github.com/ucbepic/docetl | MIT | 4,114 | Declarative LLM document-processing operators with optimisation. | Research-style; batch not service. |
| 11 | anthropics/anthropic-cookbook https://github.com/anthropics/anthropic-cookbook | MIT | 53,120 | Citations, PDF, structured-output recipes (e.g. https://platform.claude.com/cookbook/misc-using-citations). | Snippets, no service structure. |

Pharma/healthcare: I found no credible, maintained open-source pharma-document extraction service. rohitpeets/pharma-document-extraction-rag (https://github.com/rohitpeets/pharma-document-extraction-rag) is a 0-star, 2026-09-22 repo, [unverified, not read]. Treat the pharma space as open; our differentiator is DailyMed/EMA-grounded schemas and evals. Useful pharma-adjacent: Microsoft Presidio (MIT, 11,123 stars, https://github.com/microsoft/presidio) for PII/PHI redaction. No pharma-specific labelled extraction dataset found; see section 7.

## 4. Structured-extraction libraries

| Library | Licence / stars [gh] | Fit |
|---|---|---|
| Instructor https://github.com/567-labs/instructor | MIT, 13,965 | Pydantic response models + validation-retry across providers. Simple, light. Good default. |
| Pydantic AI https://github.com/pydantic/pydantic-ai | MIT, 20,322 | Typed agents, output validators, retries, evals integration, Logfire/OTel. Heavier than needed for pure extraction; good if we add agent steps. |
| BAML https://github.com/BoundaryML/baml | Apache-2.0, 9,367 | Schema DSL, tolerant parsing, strong with models lacking native structured outputs; adds a codegen toolchain. |
| LangExtract https://github.com/google/langextract | Apache-2.0, 38,927 | Source-grounded extraction: each extracted entity maps to character spans in source text, with interactive visualiser. The only library with provenance built in; Gemini-first but supports other providers. Spans are on text, not page coordinates. |
| Outlines https://github.com/dottxt-ai/outlines | Apache-2.0, 15,895 | Constrained decoding for self-hosted models (vLLM); irrelevant if using API models. |
| Provider-native structured outputs (Claude, OpenAI, Gemini JSON-schema modes) | n/a | Guaranteed-schema output with no retries; no recursive/complex schema limits vary by provider [unverified]. |

Provenance design (our own): carry block IDs/page+bbox from the parser into the prompt (e.g. `[b17] text`), make each schema field `{value, block_ids, confidence}`, and verify post-hoc that the value string appears in the cited blocks. Library choice does not give this; schema design does. Claude citations give page-level support as an additional cross-check.

Recommendation: Pydantic models + provider-native structured output, wrapped with Instructor for retries; borrow LangExtract's span-alignment idea or use it for text-only docs.

## 5. Job queue

| Option | Licence / stars [gh] | Assessment |
|---|---|---|
| Celery https://github.com/celery/celery | BSD-3 (gh shows NOASSERTION), 28,927 | Mature, huge ecosystem, Flower; sync-first, heavy, async FastAPI awkward, config-heavy. |
| Dramatiq https://github.com/Bogdanp/dramatiq | **LGPL-3.0**, 5,320 | Clean; LGPL is usually fine as a dependency but flag for client review. |
| ARQ https://github.com/python-arq/arq | MIT, 3,013 | Async-native, simple; effectively maintenance mode (last push 2026-04) and Redis-only. |
| Taskiq https://github.com/taskiq-python/taskiq | MIT, 2,346 | Async-native, FastAPI integration, brokers for Redis/SQS/RabbitMQ/Postgres; smaller community. |
| Temporal https://github.com/temporalio/sdk-python | MIT, 1,204 (server 23,403) | Durable multi-step workflows, retries, visibility, ideal for parse -> OCR -> extract -> embed with resume; but operating the server (or paying Temporal Cloud) is a lot for one engineer. |
| pgmq https://github.com/pgmq/pgmq | PostgreSQL licence, 5,307 | SQS-like queue as Postgres extension; needs extension availability on RDS (check) [unverified]. |
| Procrastinate https://github.com/procrastinate-org/procrastinate | MIT, 1,402 | Postgres-backed (LISTEN/NOTIFY, SKIP LOCKED), async + sync, retries, scheduling, no extra infra, transactional enqueue with business rows, works on RDS. |
| SQS (+ worker) | AWS managed | Durable, scales, DLQ; you write consumer/retry/visibility logic; no state/progress. |
| Hatchet https://github.com/hatchet-dev/hatchet | MIT, 8,041 | Postgres-backed durable workflow engine, newer; heavier than Procrastinate. |

Recommendation: **Procrastinate** (Postgres is already required; transactional enqueue with tenant/job rows, zero new infra, async-friendly, per-queue concurrency for GPU/OCR vs LLM). Fallback: **Taskiq + SQS** when throughput/isolation outgrows Postgres, or Temporal if multi-step durable workflows become the product. Keep job state in our own `jobs` table with idempotency keys regardless.

## 6. Eval and observability

| Tool | Licence / stars [gh] | Use |
|---|---|---|
| Langfuse https://github.com/langfuse/langfuse | MIT except `ee/` directories (verified from LICENSE), 35,282 | Tracing, datasets, prompt versions, scores; self-host (Postgres + ClickHouse + Redis) or cloud. Self-hosted core is fine for commercial use; avoid `ee/` features. |
| Arize Phoenix https://github.com/Arize-ai/phoenix | Elastic License 2.0 (gh says NOASSERTION; [unverified]), 11,674 | OTel-native tracing/evals; ELv2 forbids offering as a managed service: flag for client resale. |
| DeepEval https://github.com/confident-ai/deepeval | Apache-2.0, 18,549 | pytest-style LLM tests, custom metrics; useful for judge metrics (faithfulness). |
| Ragas https://github.com/explodinggradients/ragas | Apache-2.0, 15,900 | RAG retrieval metrics (context precision/recall); last push 2026-02, slower. |
| promptfoo https://github.com/promptfoo/promptfoo | MIT, 25,621 | YAML test matrices across models/prompts, CI gating, red-teaming; good cheap regression harness. |

How extraction accuracy is measured (standard practice):
- Field-level exact match after normalisation (dates, units, case, whitespace), reported per field and micro/macro averaged; field-level precision/recall/F1 where "missing" and "hallucinated" are counted separately (null vs wrong).
- Fuzzy match for free text: normalised Levenshtein / ANLS (threshold 0.5, DocVQA/SROIE standard).
- Tables: TEDS and TEDS-S (structure only) from PubTabNet; cell-level F1 for key tables.
- OCR/layout: character/word error rate, edit distance (OmniDocBench), reading-order and layout mAP.
- Provenance: citation precision (does cited block contain the value) and citation recall.
- Retrieval: recall@k, MRR/nDCG on labelled queries; end-to-end answer faithfulness via LLM-judge, calibrated against human labels.
- Also: schema-validity rate, latency p50/p95, cost/page, confidence calibration (ECE) and review-rate at a given error rate.

Recommendation: promptfoo or plain pytest golden-set runner for CI gating (offline replay of recorded LLM outputs, copying docextract's pattern); Langfuse for traces and online scores; DeepEval only for judge metrics.

## 7. Public sample documents and datasets

| Source | Licence / status | Use |
|---|---|---|
| DailyMed SPL labels https://dailymed.nlm.nih.gov/dailymed/spl-resources-all-drug-labels.cfm | US government work, public reuse; bulk zips (full/daily/weekly) with XML SPL + PDFs. Note: label text is manufacturer-authored but published under FDA/NLM public availability; check NLM terms, https://dailymed.nlm.nih.gov/dailymed/app-support.cfm [unverified] | Primary pharma demo: PDF + structured SPL XML gives free ground truth for sections, strengths, NDC. |
| openFDA drug label API https://open.fda.gov/apis/drug/label/ | Public domain/CC0-style per openFDA terms [unverified exact wording] | JSON ground truth cross-check. |
| Drugs@FDA labels and EMA EPARs (https://www.ema.europa.eu) | FDA public; EMA reuse policy generally CC-BY 4.0 [unverified] | Additional PDFs, scanned older labels. |
| Certificates of Analysis | No public labelled CoA dataset found. Vendor sample CoAs (e.g. Sigma-Aldrich, Cayman) are copyrighted web PDFs; do not redistribute. | Generate **synthetic CoAs** (templated, varied layouts, noise/scan degradation) with known ground truth; label ~50 by hand. |
| CORD-v2 https://huggingface.co/datasets/naver-clova-ix/cord-v2 | CC-BY-4.0 [hf api] | Receipts, field-level labels. |
| DUDE https://huggingface.co/datasets/jordyvl/DUDE_loader | CC-BY-4.0 [hf api] | Multipage business docs QA. |
| olmOCR-bench https://huggingface.co/datasets/allenai/olmOCR-bench | ODC-BY [hf api] | Unit-test style PDF parsing eval. |
| DocVQA (pixparse mirror https://huggingface.co/datasets/pixparse/docvqa-single-page-questions) | MIT on mirror; **original DocVQA terms are restrictive, check https://www.docvqa.org** [unverified] | ANLS evaluation; research use caution. |
| FUNSD https://guillaumejaume.github.io/FUNSD/ | Licence unclear; stated for non-commercial research [unverified] | Form understanding; do not ship in client deliverable. |
| SROIE https://github.com/zzzDavid/ICDAR-2019-SROIE | ICDAR competition data; licence unclear [unverified] | Receipts; research eval only. |
| katanaml-org/invoices-donut-data-v1 https://huggingface.co/datasets/katanaml-org/invoices-donut-data-v1 | MIT [hf api] | Synthetic-ish invoices with JSON GT. |
| manuelaschrittwieser/invoice-extraction-dataset-v2 https://huggingface.co/datasets/manuelaschrittwieser/invoice-extraction-dataset-v2 | Apache-2.0 [unverified] | Invoices. |
| getomni-ai/ocr-benchmark https://huggingface.co/datasets/getomni-ai/ocr-benchmark | MIT [hf api] | JSON-extraction benchmark across document types, good template for our own eval. |
| Invoice-annotation (longmaodata) | academic only; commercial needs licence [unverified] | Avoid. |

Recommendation: eval set = 100-200 documents: ~60 DailyMed PDFs (print-degraded to simulate scans for half), ~50 synthetic CoAs, ~30 synthetic invoices/POs, plus CORD/DUDE/olmOCR-bench subsets as public parser sanity checks only. Keep research-only datasets out of the delivered repo.

## 8. Recommended stack

| Layer | Primary | Fallback | Reason |
|---|---|---|---|
| Born-digital PDF text/layout | Docling (MIT, permissive weights) | pdfplumber (MIT) / pypdfium2 | Best licence profile plus tables; avoid AGPL PyMuPDF. |
| OCR/scan + hard tables | PaddleOCR-VL (Apache-2.0, 0.9B, top OmniDocBench) served via vLLM | Tesseract via OCRmyPDF, or hosted Textract (AWS-native) | Best published accuracy at small GPU cost; Textract keeps data in AWS. |
| DOCX | python-docx / Docling DOCX backend | MarkItDown | Native structure, no OCR. |
| Hosted escape hatch | Claude via Bedrock (vision + citations) | Gemini Flash / Mistral OCR | Handles pages that fail confidence gates; data stays in AWS with Bedrock. |
| Structured extraction | Pydantic schemas + provider-native structured output + Instructor retries, block-ID provenance | LangExtract (span grounding) / BAML | Provenance is schema design; Instructor is light and MIT. |
| Storage/search | Postgres + pgvector (licence: PostgreSQL-style, https://github.com/pgvector/pgvector) + tsvector, RRF fusion | OpenSearch | One datastore; avoid AGPL ParadeDB. |
| Queue | Procrastinate (Postgres) | Taskiq + SQS (or Temporal) | No new infra, transactional enqueue, fits single engineer. |
| API/UI | FastAPI + Next.js admin | n/a | Stated requirement. |
| Eval | pytest golden set + promptfoo (MIT), offline replay in CI | DeepEval (Apache-2.0) | Cheap deterministic gating on field F1/TEDS/ANLS. |
| Observability | Langfuse self-hosted (MIT core) | OTel + Postgres audit tables | Traces, datasets, scores; Phoenix ELv2 less clean for resale. |
| PII/PHI | Presidio (MIT) | regex + LLM check | Redaction before logging. |
| Deploy | ECS/Fargate API+workers, GPU node (g5/g6) for OCR on demand, RDS Postgres, S3, Bedrock | EKS | Minimal ops. |

Licence watch-list for client delivery: PyMuPDF (AGPL), Marker/Surya/Chandra weights (revenue cap), MinerU (custom licence), Dramatiq (LGPL), Unstract/paperless-ngx/ParadeDB (AGPL/GPL), Phoenix (ELv2), Langfuse `ee/`, FUNSD/SROIE/DocVQA (restricted data).

Open questions / unverified items to settle before build: exact current licence text of MinerU and Datalab weights; PaddleOCR-VL real throughput on our GPU; whether RDS Postgres supports pgmq (not needed with Procrastinate); current Claude/Gemini/GPT prices from official pages; Azure/Google/Mistral/Reducto/LlamaParse prices from official pages.
