# Plan: C15 Claude and OpenAI as model providers

Planned 2026-10-06 with the ECC planner, from `docs/research/08-feature-roadmap.md` ("Several
model providers (Gemini, Claude, OpenAI, local)", P1; the owner marked it needed on 2026-10-06).
A deployment can extract fields and answer questions with Anthropic's Claude or OpenAI's models
as well as Gemini. Each provider is measured on the same eval sets against the same floors
before it may be switched on. Run like the earlier checkpoints: tests first, ECC reviewers,
gate, records.

Facts from the code that shape it:

- **Only two tasks call a model:** field extraction (invoice, purchase order, CoA, plus the
  preview endpoint) and chat answers. Search summaries are written by code
  (`search/chunking.py`), and general documents make no model call.
- **Recordings are keyed by model only** (`llm/replay.py`). Adding the provider to every key
  would make every committed Gemini recording miss.
- **The Gemini key also controls search.** `build_search` and `build_chat` silently fall back to
  replay when `GEMINI_API_KEY` is unset, and no production check catches it.
- **OpenAI counts reasoning tokens inside `output_tokens`.** `document_cost` adds thinking on
  top, so it would count them twice unless they are split out.
- **Multi-page extraction averages about 18,500 output tokens per call.** Claude and OpenAI
  write that more slowly than Flash-Lite, so the 120 s timeout is too short.

## Decisions

1. **The provider is chosen per deployment, in settings, separately for each task.** Two tasks
   call a model: extraction (invoice, purchase order and CoA, and the stateless preview
   endpoint) and chat answers (`ChatService`, and through it the MCP `ask` tool). The default
   stays Gemini for both: it is the measured, gated and cheapest option, and C15 changes
   nothing for a deployment that sets nothing.
2. **No per-organisation choice in C15.** The worker builds its pipelines once per process.
   C15 builds a small registry (`build_provider(settings, task)`), which makes that a later,
   local change, and is the groundwork for the roadmap's "model routing".
3. **Embeddings stay Gemini-only.** Anthropic has no embeddings API; moving to OpenAI's would
   mean re-embedding every document, new dimensions and new search floors - separate work.
   Consequence: a deployment that answers with Claude still sends chunk text and every
   question to Google to be embedded (see Data protection).
4. **The vendors' Python SDKs (`anthropic`, `openai`), not hand-written httpx.** They track each
   API's structured-output parameters, error types and headers. Each is pinned below its next
   major version, kept behind one module (`src/docforge/llm/anthropic.py`,
   `src/docforge/llm/openai.py`), imported lazily, and takes an injectable `client`, so tests
   drive the real SDK through `httpx.MockTransport` with captured bodies. The SDKs' own retries
   are off (`max_retries=0`): one tested retry policy applies, as with Gemini.
5. **Structured output on every provider, from one schema function.** The pydantic reply models
   (`RawInvoice`, `RawPurchaseOrder`, `RawCoa`, `RawAnswer`) are unchanged and still validate
   every reply. Gemini still receives the pydantic class. The other two receive
   `strict_json_schema(model)` (new, `src/docforge/llm/schema.py`), which inlines every `$ref`,
   sets `additionalProperties: false` on every object, lists every property in `required`
   (a field with a default becomes required; the model sends the default), writes
   `X | None` as `type: [X, "null"]`, drops `title` and `default`, keeps `description`, and
   refuses keywords either provider rejects in strict mode (length, pattern, numeric bounds,
   `format`).
6. **No change to prompts or prompt versions.** Every provider gets the same system
   instruction, prompt and schema (`invoice-v1`, `coa-v1`, `chat-3`), so the comparison is
   fair. A provider-specific prompt, if one misses a floor, is a separate, declared prompt
   version.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `EXTRACTION_PROVIDER`, `CHAT_PROVIDER` | `gemini` | `gemini`, `anthropic` or `openai` |
| `EXTRACTION_MODEL`, `CHAT_MODEL` | unset | Unset means that provider's pin below |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Unchanged |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | Also evaluated: `claude-haiku-4-5-20251001`; `claude-opus-5-5` only if neither passes |
| `OPENAI_MODEL` | unset | Chosen by the owner and pinned; required if `openai` is selected |
| `OPENAI_REASONING_EFFORT` | unset | Only for a reasoning model; part of the pin |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | unset | Secrets; blank means unset |
| `LLM_TIMEOUT_SECONDS` | 600 | Anthropic and OpenAI: long extractions take minutes |
| `MODEL_PRICES` | `{}` | JSON, USD per million tokens: `{"anthropic/claude-sonnet-5-5": [input, output]}` |

- A model name ending in `-latest` is refused at start-up for every provider.
- Each provider compares the model the API says it served with the pin and warns if they differ.
- New production checks: each selected provider's key is set; `GEMINI_API_KEY` is set (search
  needs it - today its absence silently replays recorded embeddings); `OPENAI_MODEL` is set if
  OpenAI is selected (in every environment).
- `PRICE_INPUT_PER_MILLION_USD` / `PRICE_OUTPUT_PER_MILLION_USD` are still read, as the price of
  `gemini/<GEMINI_MODEL>`, and documented as replaced by `MODEL_PRICES`.
- The new keys are added to `infra/secrets.tf`, `deploy/env.example`, `.env.example` and
  `docs/runbook.md`.

## The two new providers

Both implement `LLMProvider` unchanged. `LLMResponse` gains no fields, so every recording stays
valid.

**Anthropic** (`AnthropicProvider`, `name = "anthropic"`)

- Messages API: `system` = the instruction, one user message = the prompt, `temperature=0`,
  `max_tokens` = 32,768, extended thinking off.
- Reply format: Claude's native JSON-schema structured output with the strict schema; for a
  model without it, a single forced tool whose `input_schema` is the strict schema. Either way
  `LLMResponse.text` is JSON. Confirmed against Anthropic's documentation in step 4.
- Streams internally and returns the final message (long replies; the SDK may refuse a
  non-streaming request this large - confirmed in step 4).
- Tokens: input includes any cache tokens; `thinking_tokens` None.
- `refusal` raises `LLMError` without retry; `max_tokens` returns the text, so the pipeline's
  validation retry and `ExtractionError` handle it.

**OpenAI** (`OpenAIProvider`, `name = "openai"`)

- Responses API: `instructions`, `input`, `text.format` = `json_schema` with `strict: true`,
  `max_output_tokens` = 32,768, and always `store: false`.
- Temperature sent only if the pinned model accepts it (a test pins what is sent).
- Reasoning tokens are split out of `output_tokens` into `thinking_tokens`, so cost does not
  count them twice. Cached input counted at full price (an upper bound).
- A `refusal` raises `LLMError`; an `incomplete` reply cut off by the limit returns its text.

**Errors, quotas and retries** - the same policy as `GeminiProvider`: 4 attempts, backoff from
2 s, `retry-after` honoured, waits capped at 90 s, `sleep` injected for tests.

| Case | Anthropic | OpenAI | Becomes |
| --- | --- | --- | --- |
| Rate limit | 429 | 429 `rate_limit_exceeded` | retried, then `LLMError` |
| Overloaded or server error | 529, 5xx | 5xx | retried, then `LLMError` |
| Timeout or connection | SDK timeout and connection errors | same | retried, then `LLMError` |
| Out of credit or spend limit | billing error | 429 `insufficient_quota` | `LLMQuotaExhausted` |
| Bad key, bad request, unknown model | 400, 401, 403, 404 | same | `LLMError`, no retry |

Error messages carry the status, error type and the provider's request id, never the prompt.

## Recordings

- A recording is keyed by provider too, without breaking Gemini's: `RecordingProvider` takes a
  `provider` name and checks `inner.name` matches; the key gains `"provider"` and the
  provider's options only when the provider is not Gemini. A test recomputes one committed
  Gemini file's key to prove it still hits.
- One directory per provider: `recorded/llm/` (Gemini, unchanged), `llm-anthropic/`,
  `llm-openai/`.
- Parses and embeddings are shared: every provider reads the same parsed text and, in the
  answer eval, the same recorded embeddings and so the same passages.

## Cost and records

- `document_cost` takes each call's (provider, model, input, output, thinking) and the
  `MODEL_PRICES` table; it returns None if any call's model has no price.
- Migration 0023: `messages.provider` (nullable; rows with a model become `gemini`), for usage
  metering's cost per answer by provider.

## Data protection

| What is sent | Where |
| --- | --- |
| Document text, for extraction | The extraction provider |
| Passages, the question and the conversation, for answers | The chat provider |
| Chunk text and every question, for embeddings | Google (Gemini), always |
| Traces and logs | No content (unchanged) |

As the providers' terms stood when planned (the owner confirms before customers' documents are
sent): Anthropic's API is not used for training under commercial terms, with limited retention
for safety review and zero retention by agreement; OpenAI's API is not used for training by
default, with abuse-monitoring logs up to 30 days, zero retention by approval, and `store:
false` always sent; Gemini paid-tier data is not used to improve Google's products (confirm
every key in use is on the paid tier). A new provider is a new sub-processor: a customer is
told before their documents go to it (invoices hold personal data under India's DPDP Act). A
customer who wants no Google cannot be served yet, because of embeddings.

## Test-first order

1. Strict schema function (unit): the four reply schemas strict everywhere; recursion and
   unsupported keywords refused; what Gemini is sent unchanged (snapshot).
2. Recording keys: a committed Gemini recording still hits; provider and options in the others'
   keys; directories per provider; a mismatched inner provider refused.
3. Settings: new fields; `-latest` refused; OpenAI model required; production checks;
   `MODEL_PRICES` with the old price settings mapped.
4. `AnthropicProvider` through `httpx.MockTransport`: request shape, streamed reply, tokens,
   refusal, cut-off, every error-table row, attempts and waits, SDK retries off, served model
   differing from the pin.
5. `OpenAIProvider`, the same, plus `store: false`, temperature only when allowed, reasoning
   tokens split out, `insufficient_quota`, incomplete.
6. Cost per model; None without a price; migration 0023 (integration).
7. Wiring: `build_provider(settings, task)`; replay when a key is missing, locally only;
   `CHAT_RECORD` per provider; the preview uses the extraction provider.
8. Pipelines and chat with each provider on hand-written replies through the transports
   (integration).
9. Evals: `--provider` and `--model`; reports under `evals/baselines/<provider>/<model>/`
   (Gemini's where they are); `provider` in every report; `python -m docforge.evals.compare`
   side by side; `make eval` replays every committed provider report.
10. Live smoke tests (skipped without keys): one invoice and one question per provider.
11. Record with the owner's keys, cheapest first: Haiku 4.5 on the invoice suite, then the full
    set (invoice, multipage, trust, scans, coa, answers), then Sonnet 5.5, then the OpenAI model.
12. Gate entries per provider recorded; docs; ECC reviews (security, python, mle); records.

## Gate

All existing checks are unchanged and pass on replay: Gemini's keys, prompts and behaviour did
not move. For each provider and model the owner funds, the same checks are added under report
names such as `anthropic/claude-sonnet-5-5/invoice` - the same quality floors as Gemini, not
looser (fields ≥ 0.99, wrong 0, hallucinated 0, multipage ≥ 0.91, trust, scans, CoA, answers
correct ≥ 0.95 with wrong 0, unanswerable declined and explained, cross-tenant 0), pins on
provider, model and prompt version, and token ceilings at each provider's own measured numbers
plus 25% (comparisons between providers in US dollars, since tokenisers differ).

Fairness: the same fixtures, parsed text, prompts, schemas and passages; temperature 0 where
allowed; one recording per provider, replayed; latency indicative only; small sets, so the
zero-tolerance checks decide; prompts were tuned on Gemini (recorded as a limit). A provider
may be selected in production only once its reports are committed and pass (a runbook rule).

## Needs the owner

- Agreement on the decisions: per deployment and per task; Gemini the default; embeddings stay
  on Google; no per-organisation choice yet.
- `ANTHROPIC_API_KEY` in a workspace of its own with a spend limit; `OPENAI_API_KEY` as a
  project key with a budget, and the OpenAI model to pin (and its reasoning effort).
- Which models to record: proposed Haiku 4.5 and Sonnet 5.5, Opus 5.5 only if neither passes.
- Budget: about 0.5 million input and 0.8 million output tokens per full run, plus a third;
  proposed cap USD 50 per provider, set in each console.
- Prices for each pinned model, or agreement to copy them from the providers' pages with the
  date beside them.
- Data protection: whether to ask for zero data retention, and the sub-processor wording.

Everything else is built and tested offline with hand-written replies and existing recordings.

## Risks

- **Gemini's replays break** - keys unchanged for Gemini, a test on a committed recording, and
  the whole existing gate replayed.
- **The APIs' structured-output parameters change** - SDK pins, one module per provider,
  transport tests on captured bodies, live smoke tests.
- **Claude and OpenAI are slower on long extractions** - streaming, a 600 s timeout, latency in
  the reports; the worker's job time and heartbeat checked against it.
- **Strict schemas change the replies** - constrained decoding may fill a field Gemini left
  null; the zero-hallucination floors catch it.
- **Recording overspends** - spend caps, the cheapest model on the smallest suite first,
  resumable runs.
- **Recordings grow the repository** - compressed JSON; retired models' recordings dropped.
- **Models are retired** - pins and the gate make a change deliberate; re-record before the
  retirement date.
- **Documents go to a new sub-processor unannounced** - production checks name what is
  selected; the runbook rule; a data-protection page.
- **No Google at all is not possible** until embeddings move: recorded, proposed for the
  roadmap.

Two API details are from memory and are checked against current documentation in step 4: the
exact form of Anthropic's JSON-schema output on the pinned models, and the SDK's handling of
large non-streaming requests. A fallback is named for each.

Estimate: 3 to 4 working days to build, plus recording time once keys arrive (about half a day
per model, quota allowing).
