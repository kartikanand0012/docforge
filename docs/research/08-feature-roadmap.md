# Feature roadmap: everything DocForge could add, after comparing it with Jevbox

Written 2026-10-04, after reading Extend's Jevbox (https://github.com/extend-hq/jevbox).

## Where DocForge stands

Jevbox is a shared document library with source-grounded chat. DocForge turns documents into
checked records and acts on them. The two meet at chat over documents, knowledge bases and
connectors. My judgement, out of 10:

| Dimension | Jevbox | DocForge now | DocForge after P0 + P1 |
| --- | --- | --- | --- |
| Formats and viewers | 9 | 5 | 7 |
| Chat with citations | 9 | 3 | 8 |
| Retrieval quality, measured | 8 | 6 | 8 |
| Library UI | 10 | 4 | 6 |
| Permissions and teams | 9 | 6 | 8 |
| Integrations (API, MCP, providers) | 9 | 6 | 8 |
| Extraction, checks, acting on results | 2 | 9 | 10 |
| Compliance and audit | 5 | 9 | 9 |
| Quality engineering | 7 | 9 | 9 |
| Deployment | 8 | 6 | 8 |
| Cost and independence from vendors | 5 | 8 | 8 |
| **Average** | **7.4** | **6.5** | **8.1** |

Rule for everything below: chat and the library support the pitch "documents in, checked
records and actions out, with proof". They do not replace it.

Priority: **P0** now, **P1** next, **P2** later, **P3** only for a client who asks.
Effort: **S** a day or two, **M** about a week, **L** two weeks or more.
In the "Gap" column, **yes** marks a feature that closes a gap with Jevbox, and **ahead** one that Jevbox does not have.

## 1. Proof and reach (what a client sees first)

| Feature | Priority | Effort | Gap | Notes |
| --- | --- | --- | --- | --- |
| Public demo deployed | P0 | S | yes | Built in C9; needs the owner's AWS account |
| Two-minute recorded walkthrough | P0 | S | | Needs the owner |
| Public eval dashboard with history | P1 | M | | Accuracy per document type and cost per 1,000 pages, per release |
| Public benchmark (DocBench or similar) alongside our own sets | P1 | M | yes | Jevbox publishes 240-question runs |
| Case-study page per document pack | P2 | S | | Pharma invoice, certificate, then the next packs |

## 2. Getting documents in

| Feature | Priority | Effort | Gap | Notes |
| --- | --- | --- | --- | --- |
| `general` document type: any document indexed for chat, no field extraction | P0 | M | yes | C10, being built |
| DOCX, PPTX, XLSX and images (PNG, JPEG, TIFF) upload, with page images | P0 | M | yes | C10; converted to PDF so citations keep their page boxes |
| Phone photos of invoices into the typed packs | P1 | S | | Free once images become PDFs |
| Google Drive folder sync | P1 | L | ahead | C13; Jevbox has none |
| Email inbox: forward to an address, attachments become documents | P1 | M | ahead | Most invoices arrive by email |
| Bulk upload and ZIP | P1 | S | | |
| Automatic document type detection | P1 | M | | Classify, then the right pack |
| Split a PDF holding several documents | P2 | M | | Common in scanned batches |
| Duplicate detection (same bytes exists; near-duplicate and same invoice number next) | P1 | S | | Duplicate payments are a real loss |
| OneDrive and SharePoint, Dropbox, S3 bucket, SFTP | P2 | M each | ahead | |
| Hindi and other Indian languages; handwriting | P2 | L | | OCR models and an eval set per language |
| Plain text, Markdown, HTML, CSV, email (.eml, .msg) | P2 | S | yes | |

## 3. Extraction and checking (our core)

| Feature | Priority | Effort | Gap | Notes |
| --- | --- | --- | --- | --- |
| Goods receipt note pack: three-way match of order, receipt and invoice | P1 | M | ahead | The standard accounts-payable control |
| Custom document types without code: schema, prompt and rules from the UI | P1 | L | ahead | The biggest lever for general clients |
| Learning from corrections: reviewed values become examples and eval cases | P1 | M | ahead | Accuracy rises with use, and is measured |
| Confidence calibrated against measured error rates | P2 | M | | |
| Model routing: the cheap model first, a stronger one when checks fail | P1 | M | | Keeps cost low, raises accuracy |
| Several model providers (Gemini, Claude, OpenAI, local) | P1 | M | yes | Jevbox has 23; we need about 3 |
| GSTIN checked against the GST portal; vendor master | P2 | M | ahead | |
| More packs: bank statements, delivery challans and e-way bills, contracts, KYC, insurance claims, lab reports, batch manufacturing records | P2 | M each | ahead | Each with its eval set |

## 4. Chat and knowledge

| Feature | Priority | Effort | Gap | Notes |
| --- | --- | --- | --- | --- |
| Chat with a document, citations checked against the source, abstention | P0 | L | yes | C11 |
| Citation opens the page and highlights the exact box | P0 | S | yes | We store block boxes already |
| Streamed answers, conversation history, regenerate | P1 | M | yes | |
| Knowledge bases (collections); chat scoped to a document, a base or the organisation | P1 | M | yes | C12 |
| Questions over extracted records ("spend with supplier X in Q3") | P1 | L | ahead | Answers from checked fields, not text; Jevbox cannot do this |
| Compare two documents (contract versions, order against invoice) | P2 | M | ahead | |
| Summaries per document and per base, with citations | P2 | S | | |
| Answer feedback (helpful or not) feeding the answer eval | P1 | S | | |
| Saved questions and alerts ("tell me when a certificate fails") | P2 | M | ahead | |
| Agentic search over folders, then documents, then sections (Jevbox's method) | P3 | L | | Only if hybrid search measures short on large bases |

## 5. Review and workflow

| Feature | Priority | Effort | Gap | Notes |
| --- | --- | --- | --- | --- |
| Approval rules from the UI (amount thresholds, approver by supplier) | P1 | M | ahead | |
| Assignment, due dates, several approval levels | P1 | M | ahead | |
| Notifications by email and Slack | P1 | S | | |
| Comments and mentions on a document | P2 | M | | |
| Bulk approve, keyboard shortcuts | P2 | S | | |
| Differences between versions of a document | P2 | S | | |
| Export to accounting systems: Tally, Zoho Books, QuickBooks, then SAP | P1 | M each | ahead | Extract-plus-act, finished |
| Zapier, Make and n8n apps over the webhooks | P2 | S | ahead | |

## 6. Library and interface

| Feature | Priority | Effort | Gap | Notes |
| --- | --- | --- | --- | --- |
| Documents page (list, filters, live stage) | done | | | C10 |
| Live processing timeline | done | | | C10 |
| Thumbnails and a page viewer for every format | P1 | M | yes | |
| Folders and tags | P1 | M | yes | |
| Dark mode, mobile layout | P2 | S | yes | |
| Saved views, sort and search within the list | P2 | S | | |
| Interface in Hindi | P3 | M | | |

## 7. Teams, access and security

| Feature | Priority | Effort | Gap | Notes |
| --- | --- | --- | --- | --- |
| Email invitations, email verification, password reset | P1 | M | yes | |
| Roles: viewer, reviewer, approver, admin | P1 | S | yes | |
| Sharing per knowledge base, then per document | P2 | M | yes | Row-level security can carry it |
| Sign-in with Google and Microsoft; SAML for enterprises | P2 | M | yes | |
| Two-factor sign-in | P2 | S | yes | |
| Audit log viewer and export in the UI | P1 | S | ahead | The chain exists; it needs a screen |
| Retention policies and deletion on request (DPDP Act) | P1 | M | ahead | |
| Hiding personal data in chat answers and exports | P2 | M | | |
| Data kept in India (Mumbai region) | P2 | S | | A deploy option |
| SOC 2 readiness | P3 | L | | See 07-soc2.md |

## 8. API, integrations and the platform

| Feature | Priority | Effort | Gap | Notes |
| --- | --- | --- | --- | --- |
| MCP server (search, read records, ask), read-only, per API key | P1 | M | yes | Agents can then use DocForge |
| Python and TypeScript SDKs from the OpenAPI spec | P2 | S | | |
| Webhook management screen with delivery log and resend | P1 | S | | |
| Scoped API keys and per-organisation rate limits | P1 | S | yes | |
| Usage metering per organisation (pages, questions, cost) | P1 | M | | Needed before billing |
| Billing (Razorpay or Stripe), plans and quotas | P2 | M | | |
| Sandbox organisation with sample documents for each new client | P2 | S | | |

## 9. Operations and quality

| Feature | Priority | Effort | Gap | Notes |
| --- | --- | --- | --- | --- |
| Managed database with backups, two hosts | P1 | M | yes | The runbook says a real deployment needs it |
| Accuracy on live documents tracked from corrections (drift) | P1 | M | ahead | |
| Error tracking (Sentry) and a status page | P2 | S | | |
| One-click deploy (Render or Railway blueprint) | P2 | S | yes | Jevbox has a Deploy to Render button |
| Helm chart for clients' own Kubernetes | P3 | M | yes | |
| Prompt comparisons on live traffic (A/B) | P3 | M | | |

## Order of work

1. **C10 (now):** `general` type, office files and images, a DOCX followed to ready in the browser test.
2. **Deploy the demo** as soon as the AWS account exists.
3. **C11:** chat with checked citations, box highlight, abstention, answer eval in the gate.
4. **C12:** knowledge bases; streamed answers and history.
5. **C13:** Drive sync; then the email inbox.
6. **Then:** MCP server, several model providers, invites and roles, audit log screen,
   three-way match, custom document types, learning from corrections.
