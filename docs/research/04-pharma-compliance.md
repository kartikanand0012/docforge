# DocForge: Pharma / Healthcare Compliance Research

Date: 2026-10-01. Prepared by web research (no legal or regulatory advice). Each claim has a source URL.
Legend: [P] = primary source (regulator/standards body/vendor docs) fetched or surfaced; [S] = secondary (blog/consultancy), treat as lower confidence; [UNVERIFIED] = could not confirm from a primary source in this session.

---------------------------------------------------------------------
## 0. Key framing (read first)

- Regulations apply to the REGULATED COMPANY (the pharma manufacturer, distributor, or sponsor), not to a software vendor directly. Part 11 and Annex 11 bind the "user" of the system. A vendor builds features that let the customer meet the rules, and the customer validates the system for its intended use. Source for the user-responsibility model: Annex 11 principle that the application is the "regulated user's" responsibility, and Part 11 scope guidance [P] https://www.fda.gov/regulatory-information/search-fda-guidance-documents/part-11-electronic-records-electronic-signatures-scope-and-application. (Annex 11 PDF itself could not be text-extracted this session: https://health.ec.europa.eu/system/files/2016-11/annex11_01-2011_en_0.pdf; the Annex 11 clause-level statements below come from secondary summaries and are marked.)
- The FDA's own AI-in-manufacturing signal as of 2026: an April 2026 warning letter (Purolea Cosmetics Lab) cited 21 CFR 211.22(c) and 211.100 because AI agents generated specifications, procedures and master production records without Quality Unit review [S] https://www.dlapiper.com/en-us/insights/publications/2026/04/fda-warning-letter-highlights-risks-of-using-ai-in-drug-manufacturing. Human accountability for AI output is the single most important design principle for DocForge.

---------------------------------------------------------------------
## 1. Regulations and guidance

### 1.1 21 CFR Part 11 (FDA, US)
Primary text: https://www.ecfr.gov/current/title-21/chapter-I/subchapter-A/part-11 [P] (fetched via eCFR renderer).
What it requires (closed systems, 11.10):
- (a) Validation for accuracy, reliability, consistent intended performance, ability to discern invalid/altered records.
- (b) Ability to generate accurate and complete copies in human-readable and electronic form for inspection.
- (c) Protection of records to enable accurate retrieval throughout the retention period.
- (d) Limit system access to authorized individuals; (g) authority checks.
- (e) "Secure, computer-generated, time-stamped audit trails to independently record the date and time of operator entries and actions that create, modify, or delete electronic records." Record changes must not obscure previously recorded information; audit trail retained as long as the record and available for agency review.
- (f) Operational system checks (enforce sequencing of steps); (h) device checks; (i) personnel training; (j) written policies holding individuals accountable for actions under their e-signatures; (k) controls over documentation and change control (revision and change control procedures with an audit trail documenting time-sequenced development and modification).
E-signatures: 11.50 signature manifestation (printed name, date/time, meaning e.g. review/approval/responsibility); 11.70 signature/record linking; 11.100 unique to one individual, identity verified, certification to FDA; 11.200 two distinct identification components; 11.300 controls for ID codes/passwords.
Retention comes from predicate rules, not Part 11 (e.g. 21 CFR 211.180: batch records retained at least 1 year after expiry; [UNVERIFIED in this session beyond the 211.180 cross-reference in https://www.ecfr.gov/current/title-21/chapter-I/subchapter-C/part-211/subpart-J/section-211.188]).
FDA 2003 scope guidance [P] https://www.fda.gov/regulatory-information/search-fda-guidance-documents/part-11-electronic-records-electronic-signatures-scope-and-application: narrow interpretation; Part 11 applies when electronic records replace paper records required by predicate rules; FDA said it "does not intend to take enforcement action to enforce compliance with the validation, audit trail, record retention, and record copying requirements" (enforcement discretion), but predicate-rule requirements still apply and decisions should be risk-based. Do NOT lean on this as a reason to skip audit trails: EU and inspectors expect them anyway (see 1.2, 1.3).
Relevance to DocForge: if DocForge extraction output is only a working aid and the official record remains the signed original PDF, much of Part 11 applies lightly. As soon as DocForge's JSON, or a reviewer's approval inside DocForge, becomes the record of a GMP decision (e.g. lot disposition support), Part 11 controls become relevant to the customer.

### 1.2 EU GMP Annex 11 (EudraLex Vol. 4), current and 2025-2026 revision
- Current text (2011): https://health.ec.europa.eu/system/files/2016-11/annex11_01-2011_en_0.pdf [P, not text-extractable here]. Well-known contents: risk management throughout lifecycle, supplier/service-provider assessment, validation, audit trail (clause 9: record all GMP-relevant changes/deletions with reason, risk-based), e-signatures (clause 14: equivalent to handwritten, permanently linked, include time/date), change/configuration management (10), periodic evaluation (11), security (12), incident management (13), business continuity (16), archiving (17). [UNVERIFIED clause numbering from memory; confirm against the PDF.]
- Revision: Draft revised Annex 11, new Annex 22 (Artificial Intelligence) and revised Chapter 4 (Documentation) published 7 July 2025 by the European Commission/EMA/PIC/S; consultation closed 7 October 2025 [S] https://www.gmp-compliance.org/gmp-news/annex-11-draft-first-analysis and https://gmpinsiders.com/2025-eu-gmp-draft-chapter-4-annex-11-annex-22/. As of 1 Oct 2026 I found NO confirmation that a final version is adopted; finalisation reported as expected 2026-2027 [S] https://normecgroup.com/news/what-do-you-know-about-eudralex-volume-4-annex-11-2026/. Treat as DRAFT. Check https://health.ec.europa.eu/medicinal-products/eudralex_en before publishing claims.
- Draft Annex 11 content (secondary analysis [S] https://www.gmp-compliance.org/gmp-news/annex-11-draft-first-analysis): grows from about 5 to about 19 pages; detailed audit-trail section; detailed access management (a smart card alone is insufficient if another person could use it); full IT-security section (firewalls, DR, patching, malware, periodic penetration tests for critical systems); mandatory periodic reviews; data archiving; e-signature section aligned with Part 11 definitions.
- Draft Annex 22 (AI): applies only to static, deterministic AI/ML models used in critical GMP applications affecting patient safety, product quality or data integrity; dynamic/self-learning, probabilistic-output models, generative AI and LLMs are not to be used in critical applications; in non-critical GMP use they are acceptable only with a qualified human-in-the-loop responsible for the output [S] https://intuitionlabs.ai/articles/eu-gmp-annex-22-ai-compliance-pharma-2 and https://gmpinsiders.com/annex-22-draft-regulatory-guidance-on-ai-use-in-gmp/. DIRECT CONSEQUENCE: an LLM extractor must be positioned as a non-critical assistive tool with mandatory human review, never as an autonomous GMP decision-maker, in EU contexts.

### 1.3 ALCOA / ALCOA+ data integrity
- FDA "Data Integrity and Compliance With Drug CGMP: Questions and Answers" (Dec 2018, final) [P landing page] https://www.fda.gov/regulatory-information/search-fda-guidance-documents/data-integrity-and-compliance-drug-cgmp-questions-and-answers (PDF body not extracted; details below from secondary).
- ALCOA = Attributable, Legible, Contemporaneous, Original, Accurate; ALCOA+ adds Complete, Consistent, Enduring, Available [S] https://www.beckman.com/resources/industry-standards/alcoa; WHO TRS 1033 Annex 4 data integrity guideline [P] https://cdn.who.int/media/docs/default-source/medicines/norms-and-standards/guidelines/inspections/trs1033-annex4-guideline-on-data-integrity.pdf.
- Concrete requirement: audit trails are part of routine record review; metadata must be retained; shared logins are findings [S, consistent with FDA Q&A; verify in PDF].
- For DocForge: the "original" is the uploaded file (store byte-identical, hash it); the extracted JSON is a derived "true copy"/interpretation, and must always link back to the original with page/bbox provenance.

### 1.4 GAMP 5 (2nd ed.), CSV vs CSA, AI guidance
- ISPE GAMP 5 Second Edition published July 2022; adds cloud, AI/ML, agile, etc. [S] https://intuitionlabs.ai/articles/gamp-5-second-edition-updates ; ISPE page https://ispe.org/topics/gamp [P].
- ISPE GAMP Guide: Artificial Intelligence published July 2025 (about 290 pages; lifecycle, data integrity, monitoring, covers rule-based, ML and generative AI) [P announcement] https://ispe.org/news/ispe-announces-availability-ispe-gampr-guide-artificial-intelligence (summary via [S] https://intuitionlabs.ai/articles/gamp-5-second-edition-updates).
- FDA Computer Software Assurance (CSA): final guidance "Computer Software Assurance for Production and Quality System Software" issued 24 Sept 2025; risk-based, critical-thinking, "scenario-based testing" replaces "ad hoc testing"; supersedes Section 6 of the 2002 General Principles of Software Validation [S] https://kneat.com/article/fda-final-csa-guidance/ and https://www.polarisbiomedical.com/post/fda-finalizes-guidance-on-computer-software-assurance-what-changed-from-the-draft. CAVEAT: this CSA guidance is from CDRH and scoped to medical device production/quality system software, not drug CGMP; pharma firms nonetheless use CSA thinking with GAMP 5 2nd ed. [S, interpretation; mark as practice, not a drug regulation].
- What it means: the customer validates DocForge for its intended use using risk-based assurance; the vendor supplies evidence (design docs, test results, traceability) to leverage.

### 1.5 FDA guidance on AI
- Draft guidance (Jan 2025) "Considerations for the Use of Artificial Intelligence to Support Regulatory Decision-Making for Drug and Biological Products": risk-based 7-step credibility framework around a defined context of use; excludes AI used for internal operational efficiency that does not affect patient safety, drug quality, or reliability of study results [P press release] https://www.fda.gov/news-events/press-announcements/fda-proposes-framework-advance-credibility-ai-models-used-drug-and-biological-product-submissions ; [P hub] https://www.fda.gov/about-fda/center-drug-evaluation-and-research-cder/artificial-intelligence-drug-development. Could not confirm finalisation as of Oct 2026 [UNVERIFIED].
- FDA/EMA "Guiding Principles of Good AI Practice" (Jan 2026; ten principles incl. human oversight, risk-based, data governance) [S] https://intuitionlabs.ai/articles/fda-ai-guidance-2026-drug-manufacturing-digital-health [UNVERIFIED primary].
- A CDER 2026 guidance agenda item on AI/ML in drug manufacturing exists [S, same URL]; no final doc confirmed.
- Practical reading: DocForge's extraction is document-handling assistance; its most plausible regulatory touchpoint is CGMP data integrity, not the AI credibility framework.

### 1.6 HIPAA (HHS)
- Applies only where DocForge processes PHI on behalf of a covered entity; then it is a business associate and needs a BAA. Most pharma manufacturing/distribution docs (CoA, batch records, invoices, SPL labels) contain no PHI. PHI risk arises with prescriptions, patient-support forms, adverse-event reports, clinical docs, Schedule H1-style sale registers.
- Security Rule technical safeguards 45 CFR 164.312 incl. audit controls (b), access control, integrity, authentication, transmission security [P] https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-C. Security Rule documentation retained 6 years (164.316(b)(2)(i)) [S] https://www.schellman.com/blog/healthcare-compliance/hipaa-audit-log-retention-policy.
- De-identification: Safe Harbor (18 identifiers) or Expert Determination; de-identified data is outside HIPAA [P] https://www.hhs.gov/sites/default/files/ocr/privacy/hipaa/understanding/coveredentities/De-identification/hhs_deid_guidance.pdf.
- Anthropic offers HIPAA-ready API access with BAA, enabled in Console (see section 5) [P] https://platform.claude.com/docs/en/manage-claude/api-and-data-retention.

### 1.7 GDPR (EU)
- Health data is special-category (Art. 9): needs an Art. 6 basis plus an Art. 9 condition [P text] https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX%3A02016R0679-20160504 ; summary https://gdpr-text.com/read/article-9/.
- Processor contract Art. 28 (DPA, sub-processors, instructions) https://gdpr-text.com/read/article-28/; security Art. 32; international transfers Chapter V (relevant to calling US-hosted LLM APIs); erasure Art. 17 conflicts with GMP retention, resolved by the Art. 17(3) legal-obligation exception [UNVERIFIED in this session, confirm in EUR-Lex].
- Most pharma docs contain limited personal data (names of QA signatories, analysts, pharmacists); still personal data. Provide DPA, subprocessor list, region pinning.

### 1.8 EU AI Act
- Digital Omnibus on AI entered into force 27 July 2026 (Regulation (EU) 2026/1744); Annex III high-risk obligations postponed to 2 Dec 2027, Annex I (embedded in regulated products) to 2 Aug 2028 [S] https://www.whitecase.com/insight-alert/eu-ai-omnibus-enters-force-amending-ai-act and https://www.gibsondunn.com/eu-ai-act-omnibus-agreement-postponed-high-risk-deadlines-and-other-key-changes/ [UNVERIFIED primary: check EUR-Lex].
- Document extraction for pharma QA is unlikely to be high-risk by itself; transparency and AI-literacy duties still relevant. Not a core demo concern.

### 1.9 India
- CDSCO / Revised Schedule M (GMP) under Drugs Rules 1945: MSME manufacturers' deadline of 31 Dec 2025 (for those who applied via Form A) has passed; CDSCO directed state regulators to inspect [S] https://qvents.in/news/cdsco-dcgi-health-ministry-india-timeline-revised-schedule-m-implementation-extension-december-2025/ and https://www.imarcengineering.com/news/india-pharma-compliance-overhaul-revised-schedule-m-cdsco-reforms. Secondary sources report revised Schedule M requires validated computerised systems with tamper-resistant audit trails and ALCOA+ [S, same URLs; UNVERIFIED primary, check the Gazette notification G.S.R. 922(E) text of Dec 2023 on cdsco.gov.in].
- Drugs Rules 1945, Rule 65 (conditions of wholesale/retail licences): wholesale supply against cash/credit memo bearing licensee name, address and licence number; for Schedule H/H1 drugs the memo/records carry batch number and expiry; Schedule H1 register retained 3 years [S] https://indiankanoon.org/doc/147665881/ and https://faolex.fao.org/docs/pdf/ind157231.pdf [P text of Rules]. Retention periods vary by licence class, [UNVERIFIED exact periods; confirm in Rules text].
- Schedule H labelling: "Rx" symbol and the words "Schedule H drug - Warning: To be sold by retail on the prescription of a Registered Medical Practitioner only" [S] https://laafon.com/understanding-schedule-h-drugs-regulations-and-restrictions-in-india/ and Rules text above.
- DPDP Act 2023 and DPDP Rules 2025: Rules notified 13 Nov 2025; phased: Board provisions immediate, Consent Manager rules at 12 months (Nov 2026), most obligations (notice, safeguards, breach notification, erasure, SDF duties, transfers) at 18 months (about May 2027) [P] https://www.pib.gov.in/PressReleasePage.aspx?PRID=2190014&reg=3&lang=2 and https://static.pib.gov.in/WriteReadData/specificdocs/documents/2025/nov/doc20251117695301.pdf ; phase detail [S] https://www.macksofy.com/blog/dpdp-rules-2025-compliance-deadlines. A DocForge customer is the data fiduciary; DocForge would be a data processor: needs contract, security safeguards, breach reporting, erasure support.
- WHO guideline on data integrity (TRS 1033 Annex 4) is a useful international anchor that Indian inspectors reference [P] URL in 1.3.

---------------------------------------------------------------------
## 2. Engineering requirements and honest claims

### 2.1 Checklist (designed to SUPPORT Part 11 / Annex 11 workflows)
Map: P11 = Part 11 clause; A11 = Annex 11 (current/draft).

Records and audit
1. Immutable append-only audit log: who (unique user id, not shared), what (entity, field, old value, new value), when (server-side UTC timestamp, NTP-synced), why (mandatory reason-for-change on edits/approvals/deletes), and context (source IP/session, model+prompt version for machine actions). Enforce append-only at DB level (revoke UPDATE/DELETE, triggers, partitioning) and hash-chain entries for tamper evidence. P11 11.10(e); A11 clause 9. Audit log retained as long as the record. Audit-trail review UI/export.
2. Original documents stored byte-identical, content-hashed (SHA-256), WORM-style object storage with versioning; never overwritten. ALCOA "Original".
3. Versioned extraction records: every re-extraction or human edit creates a new version; old versions remain readable; diff view. P11 11.10(e) "shall not obscure previously recorded information".
4. Field-level provenance: each value links to source page + bounding box + OCR/LLM method + confidence. ALCOA "Attributable/Accurate".
5. Record copies: export of record + audit trail in human-readable (PDF) and electronic (JSON/CSV) formats. P11 11.10(b).
Access and signatures
6. RBAC with least privilege and segregation of duties (uploader, reviewer, approver, admin, auditor read-only); unique accounts; MFA/SSO; session timeout; lockout; no shared logins. P11 11.10(d),(g), 11.300; A11 draft section on access.
7. Tenant isolation (Postgres RLS plus per-tenant keys), tested.
8. Review/approval step with electronic signature: re-authentication at signing, signature record shows printed name, timestamp, meaning (reviewed/approved), permanently linked to the exact record version hash. P11 11.50, 11.70, 11.200. Identity-proofing/certification (11.100) is the customer's procedure; provide the mechanism.
AI-specific
9. Human-in-the-loop sign-off: AI-extracted values are "proposed" until a named human accepts or corrects; no auto-approval for critical fields; confidence thresholds only route to review, never bypass it. Supports draft Annex 22 and the 2026 Purolea warning-letter lesson (sections 0 and 1.2).
10. Model/prompt traceability: log provider, model id/version, prompt template hash, parameters, schema version, OCR engine version, per extraction. Pin model versions; change of model/prompt is a controlled change that triggers regression evals before enablement. GAMP AI guide lifecycle monitoring.
11. Eval harness as validation evidence: golden set per document type, per-field precision/recall, regression run on every model/prompt/code change, results stored and exportable. CSA "scenario-based testing" evidence.
12. Deterministic validations layered on LLM output (checksum for GSTIN, date logic, expiry > mfg, spec-limit arithmetic) so critical comparisons are computed in code, not by the LLM.
Lifecycle, security, retention
13. Change control: versioned releases, release notes, migration records, config audit trail; semantic version shown in UI and logged. P11 11.10(k); A11 change/config management.
14. Retention and legal hold: per-tenant configurable retention policy by record class; deletion only by authorised role with reason, producing a tombstone audit entry; legal-hold flag; deletion requests (GDPR/DPDP) handled via documented exception logic when GMP retention applies.
15. Encryption in transit (TLS 1.2+/1.3) and at rest (AES-256, KMS, per-tenant keys optional); secrets management; backups with restore tests; DR plan. A11 draft IT security section.
16. Logging and monitoring, vulnerability scanning, dependency pinning, penetration test before any customer use (draft Annex 11 requires periodic pen tests for critical systems [S]).
17. PII/PHI minimisation: redact/pseudonymise before LLM calls where possible; data-classification flags per document; region pinning.
18. Documentation set (even for a demo): intended-use statement, URS-style requirements, risk assessment, design spec, test protocols and results, traceability matrix, known limitations. Customer adapts into their validation.

### 2.2 What a vendor must NOT claim
- "21 CFR Part 11 compliant" or "Annex 11 compliant": compliance is a property of a regulated user's validated use plus procedures, not of software alone. FDA itself has no Part 11 certification/accreditation (consistent with the guidance's user-responsibility model) https://www.fda.gov/regulatory-information/search-fda-guidance-documents/part-11-electronic-records-electronic-signatures-scope-and-application . Use "includes features designed to support customers' Part 11 / Annex 11 controls".
- "Validated", "GxP validated", "FDA approved/certified/cleared": validation is performed for a specific intended use in the customer's environment (11.10(a), GAMP 5). Say "provides validation support documentation".
- "GAMP 5 compliant", "ALCOA+ compliant" as a blanket: GAMP is guidance, not a certifiable standard; say "built with ALCOA+ principles in mind".
- "SOC 2 / ISO 27001 certified/compliant" unless an auditor-issued report/certificate exists (a demo has none). "SOC 2 aligned controls" is also risky; prefer "controls mapped to SOC 2 criteria (not audited)".
- "HIPAA compliant" (no HIPAA certification exists; say "can support HIPAA-regulated workloads with a BAA" only if BAAs exist along the whole chain including the LLM provider and hosting). [S] general; HHS has no HIPAA certification program [UNVERIFIED primary].
- "GDPR compliant" without DPA/subprocessor/transfer mechanisms in place.
- "Zero data retention" unless the specific provider arrangement is contractually in place and the chosen model is eligible (see section 5; some current Anthropic models require 30-day retention).
- "AI makes quality decisions / auto-releases lots", "eliminates manual review", or "100% accurate": contradicts draft Annex 22 and the 2026 warning letter lesson.
- "Used by / trusted by" any named pharma company unless true; do not use real company CoAs/batch data without permission (use synthetic documents).

---------------------------------------------------------------------
## 3. Document types in detail

General note: structure below is composed from regulatory text and industry sources; field lists are typical, not a legal template.

### 3.1 Certificate of Analysis (CoA)
- Sources: WHO model CoA (TRS 1010 Annex 4) [P] https://cdn.who.int/media/docs/default-source/medicines/norms-and-standards/guidelines/quality-control/trs1010_annex4_who_model_certificate_analysis.pdf ; practitioner summary [S] https://contractlaboratory.com/certificate-of-analysis-coa-understanding-its-importance-and-key-components/.
- Typical fields: supplier/manufacturer name and address; product/material name (INN/pharmacopoeial name), grade/monograph (USP/EP/IP/BP); batch/lot number; manufacture date; expiry or retest date; quantity/pack; test table with test name, method/compendial reference, specification (acceptance criteria), result, conformance; overall statement (complies/does not comply); authorised signatory (QC/QA), date; sometimes storage conditions, country of origin, TSE/BSE statement, residual solvents, elemental impurities, microbial limits.
- Layout quirks: multi-page tables with repeated headers; specification expressed as ranges ("98.0%-102.0%"), one-sided limits ("NMT 0.5%", "NLT 95%"), qualitative ("white crystalline powder", "complies"), footnotes; units and "on anhydrous basis" qualifiers; results like "<0.05", "ND", "BLQ"; dates in mixed formats (DD/MM/YYYY vs MM/YYYY vs "Jan-2027"); manufacturer vs site vs distributor re-issued CoAs; stamps/signatures as images; scanned PDFs with skew.
- Checks by QA/procurement:
  1. Batch number on CoA = batch on label/invoice/GRN.
  2. Material name and grade/monograph match PO and approved spec.
  3. Each result within specification limits (computed, handling NMT/NLT/ranges/"<").
  4. Tests required by the buyer's spec are all present; method references appropriate.
  5. Manufacture/expiry/retest dates sane and consistent with remaining shelf life policy.
  6. Signed/dated by authorised person; manufacturer identity matches approved supplier.
  7. Reliance on supplier CoA requires the buyer to do at least one identity test and periodically validate supplier results (21 CFR 211.84(d)) [P] https://www.ecfr.gov/current/title-21/chapter-I/subchapter-C/part-211/subpart-E/section-211.84 (summary fetched). This is a strong, citable reason CoA-vs-spec checking is a real QA task.
  8. Detection of tampering/re-issued CoA (a data-integrity concern): metadata, font inconsistencies [UNVERIFIED approach; heuristic].

### 3.2 Batch manufacturing/production records
- 21 CFR 211.188 content: accurate reproduction of the master record checked/dated/signed; dates; equipment and line identification; component batch identification; weights/measures; in-process and lab results; packaging/labelling inspections; actual and percent theoretical yield; labelling specimens; container/closure; sampling; identification of persons performing/checking significant steps; investigations; examinations [P] https://www.ecfr.gov/current/title-21/chapter-I/subchapter-C/part-211/subpart-J/section-211.188 (fetched). Lab control records: 211.194. Retention: 211.180.
- Quirks: long, multi-section forms; handwritten entries, initials, checkboxes, strike-throughs with reasons; attached printouts (balance tickets, chromatograms); deviations referencing other documents; page X-of-Y numbering; wet-ink signatures. High OCR/handwriting difficulty, high regulatory sensitivity, large documents (100+ pages).
- QA checks: all steps signed and dated, no blank fields, corrections follow GDP (single line, initials, date, reason), yields within limits, component lots match approved/released lots, deviations referenced and closed, review-by-exception.
- Regulatory burden: high (core GMP record, disposition decisions). Not recommended for first demo; at most a "completeness check assistant" with human review.

### 3.3 Pharma distribution invoices / purchase orders (India focus)
- GST tax invoice requirements: supplier name, address, GSTIN; unique sequential invoice number and date; buyer name, address, GSTIN (B2B); place of supply; description, HSN code, quantity, unit, taxable value, tax rate/amount (CGST+SGST or IGST), total; from April 2022 6-digit HSN for turnover above INR 5 crore (4-digit otherwise) [S] https://shelflifepro.in/blog/gst-invoice-requirements-pharmacy-billing-2026 and https://invoicedataextraction.com/blog/india-pharma-distributor-invoice-batch-expiry-extraction ; verify at https://www.cbic.gov.in / CGST Rules 46 [UNVERIFIED primary]. Medicines are Chapter 30 HSN (3003/3004 etc.) [S same].
- Pharma-specific line fields: product name, pack size/packing (e.g. 10x10), manufacturer/MFG name, batch number, expiry (MM/YY), MRP, PTR (price to retailer), PTS (price to stockist), quantity and free quantity/scheme, discount %, GST %, line amount; header: drug licence numbers (wholesale licence numbers, often two: Form 20B/21B; "DL No."), FSSAI sometimes, e-way bill/IRN/QR code for e-invoicing, transporter, LR number. [S] https://invoicedataextraction.com/blog/india-pharma-distributor-invoice-batch-expiry-extraction, https://www.terra-insight.com/insights/pharmacy-stockist-reconciliation-india/. Drug licence form numbers [UNVERIFIED; confirm Form 20B/21B in Drugs Rules].
- Regulatory underpinning: Rule 65 memo with licence number and, for Schedule H/H1, batch and expiry [S] https://indiankanoon.org/doc/147665881/. Schedule H marking "Rx" appears on product packs; some invoices flag "H"/"H1" per line [S; UNVERIFIED practice].
- Layout quirks: dense multi-column line tables, one batch per line with multiple lines for the same product, pharma-specific abbreviations, free-goods lines at zero value, tax summary tables by HSN and rate, rounding lines, handwritten stamps, dot-matrix style prints from legacy ERPs (Marg, Tally, Busy), landscape pages, multiple pages with carry-forward totals.
- Checks (procurement/stores/finance):
  1. Line math: qty x rate - discount = taxable value; tax = rate x taxable; totals and rounding.
  2. GSTIN format and checksum; state code vs place of supply decides CGST+SGST vs IGST.
  3. HSN vs GST rate consistency.
  4. Invoice vs PO: product, quantity, price/PTR, scheme; invoice vs GRN (goods received).
  5. Batch and expiry captured per line; minimum remaining shelf life (e.g. at least 75% or 6+ months) per company policy; MRP not above registered MRP; PTR below MRP.
  6. Supplier drug licence valid/present.
  7. Invoice vs CoA batch/expiry match where CoA is supplied.
  8. Duplicate invoice detection.
  9. Cold-chain/Schedule H1/narcotic handling flags.

### 3.4 Drug labels / package inserts (SPL / DailyMed)
- Structured Product Labeling: HL7 v3 XML standard; mandatory format for FDA drug labeling; sections coded with LOINC (e.g. Boxed Warning 34066-1, Indications 34067-9, Dosage and Administration 34068-7, Contraindications 34070-3, Warnings and Precautions 43685-7, Adverse Reactions 34084-4 [codes 34067-9 and 34084-4 UNVERIFIED]), UNII for ingredients, NDC for products [P] FDA SPL Implementation Guide https://www.fda.gov/media/84201/download ; DailyMed published by NLM https://dailymed.nlm.nih.gov/ ; summary [S] https://en.wikipedia.org/wiki/Structured_Product_Labeling and https://pmc.ncbi.nlm.nih.gov/articles/PMC3628062/.
- Fields: set id/version, labeler, product name, dosage form, route, strength, active/inactive ingredients (with UNII), NDC, package description, marketing category (NDA/ANDA/BLA), approval application number, effective time, the 16 PLR sections and subsections, patient counseling info, package label principal display panel text.
- Quirks: PDF labels are not the SPL; DailyMed provides the XML, so for US labels you can parse XML directly rather than LLM-OCR (better ground truth for evals). Tables inside sections, nested subsections, old-format (non-PLR) labels. For India: package inserts are unstructured PDFs; strip labelling carries batch/MFG/EXP/MRP, "Rx" and Schedule H warning (Rule 96 labelling requirements [UNVERIFIED rule number]).
- Checks: label text vs approved version, expiry/batch/MRP printed on pack vs invoice, Schedule H wording present, strength and NDC/product code consistency.
- Regulatory sensitivity: public data (DailyMed) is free to use and ideal for demos; no patient data.

---------------------------------------------------------------------
## 4. Workflow selection for a first demo

Scoring factors: business value, regulatory burden, data availability, technical showcase, builder's domain edge.

| Workflow | Value | Reg. burden | Data for demo | Notes |
|---|---|---|---|---|
| Invoice/PO to GRN reconciliation incl. batch/expiry/GST checks (India pharma distribution) | High: daily, manual, error-prone; direct reduction in stock/expiry/return losses | Low: commercial/finance records, GST rules, not GMP release records; human review sufficient | Synthetic invoices easy to generate; builder has domain knowledge | Strong audit-log/provenance story still applies |
| CoA vs specification and invoice cross-check | High for QA/procurement; clear pass/fail logic | Medium: CoA is a GMP quality record; 211.84 reliance makes accuracy matter; QA decision downstream | Synthetic CoAs from WHO model format | Natural extension |
| Batch record review | High but complex | High (core GMP record, disposition) | Hard (confidential) | Avoid first |
| Drug label/SPL extraction | Medium (regulatory affairs, pharmacovigilance) | Low for public labels | Excellent, free DailyMed XML ground truth | Good eval showcase, weaker business pull |

Recommendation (reasoning, my judgment, not sourced):
- PRIMARY: Pharma distribution invoice + PO + (optional) CoA three-way match for batch, expiry, MRP/PTR, GST/HSN and licence checks. Reason: clearest recurring pain, objective deterministic validations that the LLM does not decide, the builder's domain edge, synthetic data is easy, and the regulatory exposure is lowest (commercial documents; human review and audit log are sufficient to be credible).
- SECONDARY: CoA to specification-limit check with batch/expiry match to the invoice (reuses the same schema/provenance/review UI; demonstrates the GMP-flavoured features such as e-sign review and audit trail). Present it explicitly as "decision support, QA retains disposition authority". Optionally add DailyMed SPL parsing as an eval benchmark rather than a workflow.

---------------------------------------------------------------------
## 5. How regulated buyers evaluate vendors

### 5.1 Documents and assessments requested (typical)
- Supplier/vendor qualification: audit (on-site/remote/paper) or documentation review; quality agreement for GxP SaaS [P] https://ispe.org/pharmaceutical-engineering/march-april-2022/quality-agreements-saas-solutions-intended-gxp-use ; SOC 2 Type II and ISO 27001 used as supporting evidence in supplier assessment and as infrastructure-layer evidence in IQ [S] https://www.pharmavalidations.com/vendor-qualification-soc-2-iso-27001-vs-gxp-expectations/ and https://www.freyrsolutions.com/blog/cyber-risk-considerations-in-pharma-vendor-assessments. USP <1083> supplier qualification is an excipient/material reference but shows the concept [P] https://www.usp.org/sites/default/files/usp/document/supply-chain/apec-toolkit/USP%20GC1083.pdf.
- Security questionnaire (e.g. SIG, CAIQ, custom): access control, encryption, SDLC, vulnerability mgmt, incident response, BCP/DR, subprocessors, data location, personnel screening, pen-test summary. [S general; UNVERIFIED specific questionnaire names beyond common knowledge.]
- Validation support: URS/FS/DS, risk assessment, IQ/OQ/PQ templates or vendor test evidence, traceability matrix, release notes/change log, SDLC/QMS description, vendor-audit readiness, SLAs, escrow/exit plan, data export. In SaaS, the customer typically performs a leveraged IQ/OQ/PQ using vendor evidence [S] https://rxerp.com/gxp-cloud-erp-validation-life-sciences-guide/.
- Expect: DPA (GDPR, DPDP), BAA (HIPAA) where relevant, subprocessor list, data-flow diagram, AI-specific questions (model provider, retention, training on data, human oversight, drift monitoring, hallucination controls).
- For a portfolio demo: none of the certifications exist. Credible substitute: public "trust and compliance" page, architecture and data-flow diagram, control-mapping table (labelled unaudited), an intended-use + validation-support pack sample, and a threat model. SOC 2 Type II needs an observation period and auditor; ISO 27001 needs an ISMS and certification body [S general].

### 5.2 Deployment models
- Buyers often require single-tenant, VPC-hosted, or on-prem/private cloud for confidential formulations and batch data; data residency (EU, India) is a frequent requirement; DPDP Rules include transfer provisions and possible restrictions for Significant Data Fiduciaries [S] https://www.macksofy.com/blog/dpdp-rules-2025-compliance-deadlines. Design DocForge so the LLM/OCR backends are swappable (cloud API, Bedrock/Azure in-region, or self-hosted open-weights) and storage is per-tenant region-pinned.

### 5.3 Third-party LLM APIs with confidential data (verify before promising)
- Anthropic API [P] https://platform.claude.com/docs/en/manage-claude/api-and-data-retention : ZDR is per-organization via sales; under ZDR prompts/responses are not stored at rest after the response. Conversation content not retained by default, EXCEPT designated "Covered Models" (Claude Fable 5, Fable 5.1, Mythos 5, Mythos 5.1) which require 30-day retention and are not available under ZDR unless Anthropic expressly authorises; ZDR can be mixed per workspace. Even with ZDR/HIPAA arrangements, flagged content or legal holds may be retained up to 2 years. HIPAA-ready API: signed BAA plus HIPAA-enabled organization in the Console; some features (e.g. code execution) not eligible. On Bedrock the cloud provider is the processor and retained data stays in your cloud environment.
- OpenAI API [P] https://developers.openai.com/api/docs/guides/your-data : not used for training by default; abuse-monitoring logs up to 30 days by default; Modified Abuse Monitoring and ZDR available by approval; endpoints such as chat completions, responses, embeddings eligible, while stateful endpoints (assistants, threads, vector stores) retain data until deleted; data residency regions incl. US and EU (some uplift).
- AWS Bedrock [P] https://docs.aws.amazon.com/bedrock/latest/userguide/data-protection.html : model providers have no access to the Model Deployment Accounts, logs, or customer prompts/completions; supports TLS, encryption, VPC/PrivateLink, CloudTrail; abuse detection page applies. Statement that Bedrock does not store prompts and completions [S] https://repost.aws/knowledge-center/amazon-bedrock-model-data-use. Check data-retention page for any retention modes for newer models [UNVERIFIED].
- Azure OpenAI / Foundry Models [P] https://learn.microsoft.com/en-us/legal/cognitive-services/openai/data-privacy (content current to mid-2026): prompts/completions not available to OpenAI, not used for training; abuse-monitoring data store with possible human review in the customer's geography; managed customers may apply to modify abuse monitoring (turns off storage/human review); Global and DataZone deployments process outside the single region while stored data stays in the designated geography; stateful features (Responses API, Assistants, stored completions) persist data in the customer's tenant, encrypted, deletable.
- Practical guidance (judgment): use stateless inference endpoints only; no provider-side file/vector stores; pass minimum necessary text; redact personal data where feasible; record provider/model/region in audit log; offer customer-selectable provider and a no-third-party mode; state the retention terms you actually have, and re-check the Covered Models restriction since it can contradict a "zero retention" promise.

---------------------------------------------------------------------
## 6. Gaps and items to verify before publishing claims
1. Annex 11 (2011) clause numbers and wording: re-read the PDF; draft 2025 content is from secondary summaries; confirm whether finalised after Oct 2025 consultation.
2. FDA Data Integrity Q&A (2018): read the PDF directly for exact audit-trail/metadata statements.
3. FDA AI draft guidance status (final or still draft) and the CDER manufacturing-AI guidance.
4. Revised Schedule M gazette text on computerised systems; Rules 65/96 and licence form numbers from the Drugs Rules text; GST CGST Rule 46 invoice content from CBIC.
5. EU AI Act Omnibus dates against EUR-Lex.
6. LOINC codes beyond the ones cited.
7. Anthropic Covered Models and ZDR terms can change; confirm in contract.
8. Any legal conclusions: get review from a regulatory/QA professional before marketing statements.
