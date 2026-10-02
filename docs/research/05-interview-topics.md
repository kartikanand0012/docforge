# DocForge - Part B: Interview topics this project lets you answer from experience

Compiled 2026-10-01. Sources are 2026 interview-prep guides (not verified first-hand experience posts: I did not find specific Reddit/HN interview-experience threads for document AI; marked below). Frequency marks are MY judgment based on how many independent lists contain the topic: HIGH = in 3+ lists, MED = 2, LOW = 1. No source publishes real frequency statistics (the landedjobs and prachub guides state none).

Sources used:
- [L] https://github.com/landedjobs/rag-engineer-interview-questions (100 Qs, 7 categories; fetched)
- [P] https://prachub.com/resources/ai-engineer-interview-questions-2026-rag-agents-evals-and-production-systems (10 core Qs; fetched)
- [K] https://www.kore1.com/ai-engineer-interview-questions-2026/ (4 interview blocks; fetched)
- [T] https://pub.towardsai.net/7-rag-agent-system-design-questions-you-will-face-in-every-ai-engineer-interview-with-answers-45d31004ffe4 (403; snippet only)
- [C] https://www.coprep.ai/blog/top-ai-engineer-interview-questions-in-2026-llms-rag-agents-and-langchain , https://careery.pro/blog/ai-careers/ai-engineer-interview-questions , https://www.mockingly.ai/blog/rag-system-design-interview (search snippets only)
- Production-problem links in 02-production-problems.md back each "DocForge answer".

## 1. System design
| Question | Freq | DocForge experience to cite | Source |
|---|---|---|---|
| Design a document processing pipeline / RAG over documents end to end | HIGH | Ingest -> OCR fallback -> layout parse -> chunk -> extract -> validate -> store -> search | [P][K][C][T] |
| "Walk me through what happens between a query and an answer for a 200-page PDF" | HIGH | Page-level parsing, chunking, hybrid retrieval, rerank, grounded answer with citations | [K] |
| Walk me through an AI system you shipped | HIGH | DocForge itself, with numbers from your eval set | [P] |
| Sync vs async; where do queues go; how do you scale to N docs/day | HIGH | SKIP LOCKED queue, stateless workers, backpressure, DLQ | [C] [U on exact phrasing]; tianpan pipeline architecture |
| Agent vs deterministic workflow | MED | Fixed pipeline with LLM only at extraction step | [P] |
| Retrieval-time ACLs and enterprise design | MED | Per-tenant RLS | [L] |

## 2. RAG / retrieval
| Question | Freq | Experience | Source |
|---|---|---|---|
| Chunking strategy and trade-offs (size, overlap, tables, structure-aware) | HIGH | Layout-aware chunks, table-as-unit, heading-path metadata | [L][K][C] |
| Hybrid search: BM25 + dense + RRF; why dense alone fails on identifiers | HIGH | Batch numbers, CAS numbers, SKUs; tsvector + pgvector + RRF | [L] |
| Reranking, cross-encoders, latency budget | MED | Optional rerank stage with measured gain | [L][C] |
| Contextual retrieval / orphaned chunks | MED | Anthropic numbers: 5.7% -> 1.9% failure (https://www.anthropic.com/news/contextual-retrieval) | [L] |
| "Answer is wrong though the document exists - debug it" | HIGH | Trace parse -> chunk -> retrieve -> generate; per-stage evals | [P] |
| Embedding choice, dimensionality as RAM bill (N x dim x 4B) | MED | halfvec, dimension trade-offs | [L] |
| Why embedding similarity differs from relevance | MED | Domain jargon in pharma | [K] |
| Query rewriting, freshness, re-indexing via CDC | LOW | Document versions and re-embedding | [L] |

## 3. Evaluation
| Question | Freq | Experience | Source |
|---|---|---|---|
| How do you build an eval set and decide a change can ship? | HIGH | Golden set of labelled documents, CI gate, per-field metrics | [P][K][L] |
| "How do you know it is good? Your number, not your gut" | HIGH | Field-level precision/recall/exact-match, hallucination vs omission rates | [K] |
| Prompt regression detection and prevention | HIGH | Versioned prompts, CI eval on PR, per-model baselines | [K][P] |
| Retrieval metrics (recall@k, MRR, nDCG) and generation faithfulness (Ragas, DeepEval, TruLens) | HIGH | Retrieval eval over labelled queries | [L][C] |
| Can LLM-as-judge be trusted? Calibrate with Cohen's kappa | MED | Judge only for free text; exact-match for numbers | [P][L] |
| Online monitoring, drift, sampling 1-2% for human audit | MED | Review queue sampling | tianpan link |

## 4. LLM reliability
| Question | Freq | Experience | Source |
|---|---|---|---|
| Integrate an LLM behind a reliable application contract (schema validation, retries) | HIGH | JSON schema, constrained output, semantic validators, repair-or-reject | [P] |
| Handle malformed JSON from structured output | HIGH | Validation + bounded retry + DLQ | [K] |
| Hallucination mitigation | HIGH | Source-span grounding, null over guess, cross-field rules, HITL | [C][P] |
| Non-determinism, temperature, model upgrades | MED | Pinned versions, golden-set re-run | [K] |
| Prompt injection from untrusted documents | MED | Hidden-text PDFs, data/instruction separation | [P] (security group) |
| Incident response after model or prompt change | MED | Version stamps, rollback, reprocessing | [P] |
| Fine-tune vs prompt vs RAG | MED | HITL corrections as training data | [K] |

## 5. Data pipelines / async
| Question | Freq | Experience | Source |
|---|---|---|---|
| Idempotency, retries, exactly-once illusions | MED | Content-hash idempotency keys | [U: general backend staple; not found in the AI lists fetched] |
| Dead-letter queues, poison documents | MED | DLQ classification and redrive | same |
| Rate limits (429), backoff with jitter, backpressure | MED | Retry-After, per-tenant concurrency | https://www.getmaxim.ai/articles/handle-429-errors-in-production-llm-applications/ |
| Large-file handling, memory, OOM | LOW | Worker recycling (docling memory issues) | docling GitHub issues in file 02 |
| OCR vs VLM trade-offs, parser selection with numbers | HIGH for document-AI roles [U] | Section 3 of file 02 | file 02 |

## 6. Postgres / pgvector
| Question | Freq | Experience | Source |
|---|---|---|---|
| HNSW vs IVFFlat, tuning ef_search, build params, quantization | HIGH | Measured recall/latency | [L] |
| Filtered ANN recall collapse; iterative scan | MED | Reproduce and fix with 0.8 iterative scan | file 02 section 6 |
| When pgvector is enough vs a dedicated vector DB | HIGH | Scale bands, memory, p99 | [L][C] |
| Partitioning, partial indexes per tenant | MED | Whale-tenant handling | file 02 |
| Queue in Postgres (SKIP LOCKED) | LOW | See above | file 02 |

## 7. Security / multi-tenancy
| Question | Freq | Experience | Source |
|---|---|---|---|
| Secure a RAG or tool-using agent: trust boundaries, authorization | HIGH | RLS, tenant context per transaction | [P][L] |
| Multi-tenant isolation in pgvector (RLS, FORCE RLS, SECURITY DEFINER trap) | MED | Tests that prove cross-tenant reads fail | [L]; file 02 |
| PII/PHI redaction, audit logs, retention | MED | Per-tenant audit log, redaction hooks | [L] |

## 8. Cost / latency
| Question | Freq | Experience | Source |
|---|---|---|---|
| "Quality improved but latency and cost are unacceptable - what do you do?" | HIGH | Route simple pages to cheap path, batch API, caching, smaller models | [P] |
| Token cost tripled - investigate | MED | Per-stage cost telemetry | [K] |
| Semantic caching, latency budget (e.g. sub-800 ms) | MED | Retrieval endpoint timings | [L][C] |
| Build vs buy; stakeholder pushes to ship with weak evidence | MED | Eval gate | [P] |

## Top 10 most frequent (judgment)
1. End-to-end RAG/document pipeline design
2. Chunking strategy
3. Hybrid search (BM25 + dense + RRF)
4. Building eval sets and ship/no-ship gates
5. Hallucination mitigation and structured-output reliability
6. Debugging a wrong RAG answer
7. pgvector vs dedicated vector DB, index tuning
8. Cost/latency optimisation
9. Security and multi-tenancy
10. Prompt/model regression and incident response

## Caveat
These lists are prep-industry content, partly SEO. Frequency claims are inferred, not measured. Live-coding items ("stand up a retrieval endpoint in 40 minutes", "debug a broken agent") appear in [K].

## Learned while building (tagged to the code)

### C0 Foundations (2026-10-02)

| Question it answers | What happened in this project | Where to point |
|---|---|---|
| How do you build an eval set? | Generated the documents from a seeded model, so the label is the source and the PDF is derived from it. The label also stores the page box of every value, ready for provenance scoring in C3. | `src/docforge/synth/builder.py`, `src/docforge/synth/render.py` |
| How do you know your ground truth is right? | A review found labels held values the page never prints (per-line CGST/SGST, supply type). An extractor would have been marked wrong for not reading something invisible. Each label now lists its unprinted paths, and a test walks every value so none can go unaccounted for. | `DocumentBoxes.unprinted` in `src/docforge/synth/models.py`; `tests/unit/test_synth_render.py` |
| How do you test a calculation without repeating its bug? | The first arithmetic tests re-implemented the builder's formulas, so a shared mistake would pass. Added cases worked out by hand, including a half-paisa tie where CGST and SGST each round up and their sum is a paisa over the rate. | `TestHandComputedArithmetic` in `tests/unit/test_synth_builder.py` |
| A test that passes but proves nothing | The bucket check used an anonymous request and expected 403. MinIO returns 403 for a missing bucket too, so it passed either way. Writing the "missing bucket" case exposed it. | `tests/integration/test_services.py` |
| How do you make generated artefacts reproducible? | Byte-identical PDFs on macOS and Linux needed three things: fixed timestamps and document id, uncompressed streams (bytes otherwise depend on the zlib build), and a fixed month table instead of locale-dependent `%b`. | `_Page.__init__` and `_dated` in `src/docforge/synth/render.py` |
| Idempotency key design | Content hash unique per tenant, not globally, enforced by the database with a format check. Tests cover duplicate within a tenant, same file across tenants, and a malformed hash. | `src/docforge/migrations/versions/0001_core_tables.py`; `tests/integration/test_migrations.py` |
| How can a secret leak without being logged? | The database URL carries the password. It leaked through `repr(settings)` and through the validation error for a bad URL, which echoes the rejected input. | `hide_input_in_errors` in `src/docforge/config.py` |
| Making a replace-in-place operation safe | The generator deleted the old set before building the new one, so a failure left a half-written fixture folder. It now builds everything in memory, then swaps, and writes the manifest last. | `generate_dataset` in `src/docforge/synth/dataset.py` |
| Supply-chain hygiene in CI | A floating action tag did not exist and the vendor's image had been withdrawn. Actions are pinned to commit SHAs and images to digests. | `.github/workflows/ci.yml`, `docker-compose.yml` |
