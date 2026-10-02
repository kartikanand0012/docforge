# DocForge research 01: what clients actually ask for in document intelligence

Date: 2026-10-01. Method: web search snippets + page fetches. Upwork job pages and G2 pages returned HTTP 403 to fetch, so Upwork/G2 claims below come from SEARCH-ENGINE SNIPPETS unless stated. Many "complaint" and "demand" sources are vendor blogs (biased, they sell the alternative) - flagged [VENDOR]. Anonymous practitioner posts are flagged [UNVERIFIED].

## 0. Evidence quality, read first
- I found NO actual Upwork buyer job posts (the "client wrote this" kind). Upwork evidence is freelancer-side Project Catalog listings and hire pages, i.e. what sellers advertise and price, which reflects demand indirectly. Treat "what buyers ask for" as inferred.
- No direct Reddit threads were retrievable. The one aggregator citing practitioner posts (idp-software.com) says itself the posts are anonymous and unverified.
- Pharma "manual cost" figures are almost all vendor marketing. Not independently verified.

## 1. What buyers ask for (recurring requirements)

Document types (invoices dominate): Upwork catalog listings advertise extraction from "PDFs, scanned documents, invoices, receipts, purchase orders, bank statements, forms, reports" [snippet: https://www.upwork.com/hire/data-extraction-freelancers/ ; https://www.upwork.com/services/product/development-it-automate-data-extraction-process-from-pdf-invoices-however-number-of-pdfs-1725490742977343488].

Recurring requirements:
- Human review with confidence: one Upwork seller sells "low-confidence or unclear values sent to a review screen, final approved data exported to Excel, CSV, JSON, Google Sheets, or REST API", pitched as "high accuracy, auditability" [snippet, Upwork hire page above]. A GitHub reference project is built around "per-field confidence and a Review sheet" https://github.com/dmanhlr/pdf-data-extractor
- Per-field confidence as baseline; accuracy tested on the client's own documents not vendor benchmarks; a defined handling strategy for low-confidence output; cost transparency at real volume; table-extraction tests on real data [UNVERIFIED practitioner synthesis] https://idp-software.com/news/idp-accuracy-reckoning-2026/
- Accuracy: practitioners report 85-95% typed-invoice accuracy with classic OCR, 96% field-level with specialised routing, and 15-30% of documents routed to human review in production [UNVERIFIED] (same idp-software URL).
- Export/integration: Excel/CSV/JSON, Google Sheets, REST/webhooks, QuickBooks API (an Upwork seller lists Django + Textract + OpenAI + QuickBooks API) [snippets, Upwork]. In India: TallyPrime Excel/XML, Busy, Marg CSV batch-import https://invoicedataextraction.com/blog/india-pharma-distributor-invoice-batch-expiry-extraction [VENDOR].
- Pharma/GxP integrations: LIMS, QMS, ERP, DMS over REST/FTP https://www.acodis.io/automate-certificate-analysis [VENDOR]; SAP/Oracle ERP with 4-way match https://ezintegrations.ai/po-document-intelligence-pharma-oracle-sap/ [VENDOR]; MES, ERP, LIMS for batch records https://mareana.com/paper-batch-record-digitization/ [VENDOR].
- Deployment/compliance: on-prem/VPC, zero data retention, signed BAA, SOC 2 Type II, audit logs for PHI workloads https://llms.reducto.ai/hipaa-compliant-document-processing [VENDOR]; Google Document AI documents data residency + VPC-SC https://docs.cloud.google.com/document-ai/docs/security
- Audit trail in pharma: document hashes, extraction confidence, match decisions, approver identity, e-signatures per 21 CFR Part 11, UTC timestamps, RBAC https://ezintegrations.ai/po-document-intelligence-pharma-oracle-sap/ [VENDOR]. Generic OCR criticised for lacking "GxP-grade audit trails and confidence scoring" https://mareana.com/blog/how-to-digitize-paper-batch-records-with-mareana/ (via search summary; page not fetched).
- Volumes / SLAs: NOT found in any buyer's words. Only vendor batch-queue SLAs (Reducto 20% discount, 12-hour completion) https://llms.reducto.ai/best-document-processing-apis-2026 [VENDOR]. Unverified: typical buyer volumes.

## 2. Complaints: about tools and about builders

Tools:
- Rossum (G2, via snippet https://www.g2.com/products/rossum/reviews): "expensive for small amounts of documents"; one reviewer says it costs 3x a human; "exorbitant" price increases; needs professional services.
- Hyperscience / ABBYY (G2 snippets https://www.g2.com/products/abbyy-vantage/reviews?qs=pros-and-cons): long enterprise sales cycles, heavy IT effort, struggles with variable layouts and handwriting; ABBYY "$50K-200K+ to implement, 6-12 months" (that figure is from a competitor blog https://www.lido.app/blog/best-abbyy-alternatives [VENDOR]).
- Azure Document Intelligence: struggles with highly variable layouts, steep custom-model learning curve, no built-in feedback/retraining API, cannot edit OCR text in the labeling UI https://www.lido.app/blog/azure-document-intelligence-alternative [VENDOR]; https://learn.microsoft.com/en-us/answers/questions/1703062/azure-document-intelligence-feedback-mechanism-wit (Microsoft Q&A, real users).
- Textract: no JSON-Schema extraction; flat block output, so teams write their own logic to rebuild tables/fields/cross-page context; handwriting English-only and weaker https://www.extend.ai/resources/aws-textract-when-to-use-alternative [VENDOR].

Builders / why projects fail:
- "The demo works, production does not"; 8 OCR tools on 200+ multilingual shipping invoices mostly destroyed table structure; multi-page/merged-cell tables fail everywhere; one pharma+finance project reports ~70% multi-page-table success, rest manual; GPT-4.1 on 150k handwritten pages fell from ~85% (page 1) to ~65% (page 3); agent systems "work then edge cases accumulate" https://idp-software.com/news/idp-accuracy-reckoning-2026/ [UNVERIFIED, anonymous].
- Accuracy with no denominator: 99% per-character means ~91% on a 9-char invoice number and ~20% on a 20-field document; analysts kept checking everything; fix = write a business-rule validation library first (arithmetic, referential, format, temporal, cross-document) https://www.kore1.com/why-ocr-projects-stall/ [VENDOR/consultant essay].
- Failures originate in the extraction layer (broken reading order, lost table structure), and fluent LLM output hides errors; LLM-as-OCR is risky; "68% of extraction errors in financial docs are hallucinated numbers" (stat from a search summary, original study not verified) https://altersquare.medium.com/why-enterprise-document-ai-fails-at-the-extraction-layer-not-the-model-layer-9be60a3460ec ; https://medium.com/@evalowisz/dont-use-llms-as-ocr-lessons-from-complex-documents-8401b6a54d62
- Modular pipelines moved invoice accuracy from <40% to >90% (blog claim, unverified) https://www.layernext.ai/post/ocr-invoice-processing-errors
- Could NOT find first-person "I fired my freelancer" accounts. Gap.

## 3. Pharma / life-sciences workflows

Caveat: all cost numbers below are vendor or consultancy claims.

| Workflow | Buyer | Fields that matter | Manual cost (claimed) | Who serves it |
|---|---|---|---|---|
| CoA verification | QA/QC, receiving, procurement at manufacturers and distributors | lot/batch, product, test results, units, limits (NMT/NLT), method, dates, supplier, spec revision; match to PO/GRN/ASL | 30-90 min per CoA; "$200K+/yr hidden labor for mid-size distributors" (https://upbrains.ai/blog/certificate-of-analysis-definition-management-automation via search summary); 60-80% time saving claim https://www.acodis.io/automate-certificate-analysis | Acodis, UpBrains, Artsyl docAlpha, CIKLab, Beseek; open n8n template https://n8n.io/workflows/10491-automate-pharmaceutical-coa-verification-and-vendor-scoring-with-ai-document-analysis/ |
| Inbound CoA matching (process detail) | Receiving + QA | identity match, spec-revision alignment, unit conversion (ppm vs mg/kg), quarantine until matched; failure modes: spec drift, uncontrolled CoA reissues | n/a | https://sgsystemsglobal.com/glossary/inbound-coa-matching-workflow/ |
| Pharma invoice/PO (AP) | AP + QA at pharma companies on SAP/Oracle | invoice batch vs CoA batch vs GRN lot, 4-way match, ASL check, 21 CFR Part 11 trail | 18-30 min AP + 15-45 min QA per direct-material invoice; exception rate 28% (claims) https://ezintegrations.ai/po-document-intelligence-pharma-oracle-sap/ | eZintegrations, generic IDP (Rossum etc.) |
| India pharma distribution invoices | Retail chemists, stockists, distributors | brand/salt/strength, HSN (3003/3004/3006), batch, mfg+expiry, pack, qty, FREE qty (10+1 schemes), PTR, MRP, discount, per-line GST %, GSTIN; checks: expiry > mfg, PTR <= MRP, free-qty <20% | invoice entry 20-30 min -> under 3 min (https://medlens.in/blog-pharmacy-billing-software-india, via snippet); common OCR failures: scheme collapse, PTR/MRP flip, multi-rate GST https://invoicedataextraction.com/blog/india-pharma-distributor-invoice-batch-expiry-extraction | Marg ERP, Pharma247, MastersIndia invoice OCR, azapi.ai (snippets), invoicedataextraction.com |
| Batch records | QA release, manufacturers/CMOs | handwritten entries and corrections, nested tables, line clearance, equipment calibration checks, signatures | industry avg 48h per batch review; 18-34 h/batch lost to transcription; $0.5-2M/yr release delays (https://ifactoryapp.com/article/pharma-batch-record-review-digital, https://sgsystemsglobal.com via search summary) | Mareana (claims >99%, 4-8 weeks, J&J/Merck/BMS), EBR vendors (POMS, Tulip, iFactory) which avoid the problem by going digital |
| Pharmacovigilance intake | Drug-safety teams, PV CROs | reporter, drug, AE terms, validity, dates, seriousness | ~30 -> 5-10 min per case claimed; CROs charge $15-25/case for intake+submission; median 69 min background work per report https://intuitionlabs.ai/articles/ai-roi-pharmacovigilance-business-case | Veeva Vault Safety.AI intake agent, ArisGlobal Advanced Intake, Oracle Argus, IQVIA, PPD. Dominated by safety-DB incumbents; regulators expect validated tools + human in loop |
| Regulatory submissions / labels | Regulatory affairs | SPL/labeling concepts, local label deviations, eCTD structure | not found | Veeva Vault RIM (15 of top 20 pharma, own AI layer) https://intuitionlabs.ai/articles/veeva-rim-labeling-workflows . Weak freelancer target: incumbent-owned, long validation. Not verified: any SPL-extraction vendor |

Positioning inference (mine): the best-evidenced, freelancer-sized, defensible pharma wedge is distributor/AP invoices with batch+expiry+scheme fields, and CoA-to-invoice/PO matching. PV and RIM are incumbent territory.

## 4. Pricing evidence

Commercial tools:
- AWS Textract: OCR $1.50/1k pages; tables ~$15/1k; forms ~$50/1k; combined up to $65/1k https://aiproductivity.ai/blog/document-ai-cost-comparison/ (aggregator, not AWS page).
- Azure Document Intelligence: prebuilt ~$10/1k pages, 500 free/mo https://docuocr.com/blog/azure-document-intelligence-pricing
- Google Document AI: OCR ~$1.50/1k, layout ~$10/1k, specialised parsers priced per processor (same aggregator as Textract).
- Reducto $0.015/page (15k free credits), 2-4 credits for agentic; LlamaParse ~$0.00125 to ~$0.056/page by tier; Unstructured $0.03/page https://llms.reducto.ai/best-document-processing-apis-2026 [VENDOR, Reducto's own comparison]
- Nanonets: from $0.30/page Starter, Pro $999/mo per workflow; est $360-560/1k pages. Veryfi $500/mo min then $0.16/invoice. Affinda $0.20 -> $0.05/page. Docparser $159/1k docs. Parseur $99/1k pages at low volume down to ~$30/1k https://parseur.com/blog/idp-pricing
- Rossum ~from $1,500/mo (snippet https://www.docsumo.com/compare/rossum-alternative-docsumo); Docsumo from $25/mo, otherwise quote. ABBYY/Hyperscience: no public price, "six figures a year" (parseur above).
- Implementation: Azure partner lists IDP projects at $50K-$100K over 1-3 months (parseur above); agency blog claims production IDP with review+integrations $75K-$200K, single-doc MVP $30K-$75K https://gmware.com/blog/intelligent-document-processing-cost/ [VENDOR, low reliability].

Freelance market (Upwork snippets, not page-verified):
- Catalog "AI driven automated document processing solution": tiers $1,000 (Starter) to $6,000 (Advanced) https://www.upwork.com/services/product/development-it-an-ai-driven-automated-document-processing-solution-1784499820833429740 [snippet]
- Other catalog gigs: basic PDF extraction from $2,000; OpenCV+OCR tool ~$900; custom language support $4.5K-7K; end-to-end system integration $7K-$12K [search-summary of several Upwork listings; individual tiers not checked].
- AI developer rate $30-$150/h; AI automation/document processing projects $1.5K-$6K [snippet, Upwork hire pages].
- Takeaway: your $3.5K-$12K band sits inside the freelance market; the 10-20x gap to agency/enterprise ($50K+) is the argument for "production-grade at freelance price". Per-page cost to beat: roughly $0.01-0.06/page managed API; a self-hosted pipeline wins on predictability and data residency, not raw price.

## 5. Ranked features (production-ready doc-intelligence service)

MUST-HAVE
1. Per-field confidence + routing thresholds (auto-approve / review / reject). Evidence: Upwork review-workflow listings; idp-software "baseline requirement"; eZintegrations flags <0.91-0.93.
2. Human review UI with source-highlight (bbox on page), edit, approve; corrections stored. Evidence: Textract A2I exists because buyers need it; Azure UI complaints; "human review infrastructure is critical, not ancillary".
3. Business-rule validation layer (arithmetic, referential, dates, cross-field). Evidence: kore1 essay; pharma invoice validations (PTR<=MRP, expiry>mfg); CoA unit/limit logic.
4. Real accuracy evals: field-level, on the client's docs, with a labeled set and regression run on each change; report with denominators. Evidence: idp-software; kore1.
5. OCR fallback + scan/quality handling, multilingual, handwriting flagged not guessed. Evidence: Textract/Hyperscience handwriting complaints; batch records.
6. Reliable table extraction incl. multi-page and merged cells. Evidence: practitioner ~70% success; every parser failing at least one case.
7. Typed JSON schema output with versioned schemas per doc type, plus Excel/CSV export and webhooks. Evidence: Textract lacks JSON schema; Upwork export lists.
8. Audit log + source provenance (doc hash, model/prompt version, who changed what, timestamps), immutable. Evidence: eZintegrations Part 11 list; Acodis "traceability back to source"; Mareana "GxP-grade audit".
9. Multi-tenancy + RBAC + data isolation, encryption, retention/deletion controls. Evidence: Reducto HIPAA/zero-retention/BAA page; Google VPC-SC. (Buyer pull is inferred; no buyer quote.)
10. Async batch API: idempotency, retries, status, queueing, cost/latency tracking. Evidence: Reducto batch SLA; cost-transparency requirement.

DIFFERENTIATORS
11. Self-hostable / VPC deployment with local-model option (docker-compose + data residency). Evidence: Reducto on-prem/VPC; MS data residency. Strong in pharma and India.
12. Pharma-specific packs: invoice (batch/expiry/scheme/PTR/GST), CoA (spec compare, NMT/NLT, unit normalisation), 4-way match with ASL. Evidence: invoicedataextraction, sgsystems, eZintegrations.
13. Accounting/ERP connectors: Tally/Busy/Marg export first, QuickBooks, SAP/Oracle via REST adapter. Evidence: Upwork QuickBooks listing; Tally/Marg formats.
14. Hybrid search over extracted fields + chunks (pgvector) with citations to page/bbox, for "ask the documents". Evidence: RAG/parsing demand in parser blogs (Reducto/LlamaParse/Unstructured positioning). Weakest buyer evidence for invoice buyers; stronger for regulatory-document buyers.
15. Learning loop: reviewer corrections feed few-shot examples / eval set (gap in Azure). Evidence: Azure Q&A on missing feedback API.
16. Validation pack for GxP (IQ/OQ-style docs, e-signature with meaning). Evidence: eZintegrations 3-8 weeks of CSV. Likely out of scope for a portfolio, but a "validation-ready" documentation set is a credible talking point.

## 6. Three biggest surprises
1. Buyers' pain is trust, not extraction. The winning claim is "errors are caught and a human sees only the 15-30%" with measurable denominators. Validation rules + review UI + evals beat model choice.
2. Pharma AP is 4-way matching (invoice + PO + GRN + CoA), not plain invoice OCR; and in India the fields that break generic OCR are free-quantity schemes, PTR vs MRP and per-line GST, with Tally/Marg export as the integration.
3. Price anchors: managed APIs cost pennies/page ($0.0015-$0.06) while enterprise IDP is six figures and agencies quote $30K-$200K; Upwork catalog gigs sit $1K-$12K. The freelance sweet spot is a 'small-team IDP, self-hostable' offer. Also: I found no real buyer job posts; this research is indirect.

## 7. Not verified / next steps
- Real Upwork job posts (needs logged-in browser; try Chrome MCP) for budgets, volumes, SLAs.
- Reddit/HN buyer threads, first-person freelancer-failure stories.
- G2 quotes beyond snippets; Veeva/SAP connector demand from buyers; regulatory (SPL) extraction vendors.
