# ADR 0007: Model tier per node, and a waterfall budget

- Status: accepted (M1). Real providers are added in M3; this ADR will be amended with the
  verified model list then.
- Date: 2026-09-28

## Context

Nodes do very different work. The supervisor makes a small, frequent routing decision from
a compact state view. Workers read sources, write and critique a memo. Using the strongest
model everywhere wastes money on routing; using the cheapest everywhere hurts memo quality.

Each run also has a hard budget (tokens, USD, deadline). A public demo must never exceed it,
and one runaway node (for example a coder stuck in a tool loop) must not be able to spend
the whole run's budget.

Model names change. A model used in another of my projects was retired and broke the live
site, so model names must be configuration, checked against the provider before use.

## Options considered

1. **One model for everything.** Simplest, but either too expensive or too weak.
2. **Tiering by node type** (chosen). A `fast` tier for the supervisor and a `strong` tier
   for workers, each with a primary and a fallback route.
3. **Dynamic model selection per call** (a router model picks the model). More savings in
   theory, but it adds another LLM decision to debug and evaluate. Out of scope.

For budgets:

1. **Check the budget after each call.** Simple, but the call that crosses the limit has
   already spent the money.
2. **Fixed split per node** (for example 25% each). Wastes budget when a node needs less, and
   starves the node that needs more.
3. **Waterfall** (chosen). The run budget flows down: a node gets
   `min(node cap, run remaining)`, and each call gets a `max_output_tokens` sized so the
   worst case fits what the node has left.

## Decision

- `config/models.toml` maps each node to a tier, each tier to a primary and fallback
  `(provider, model)`, and each model to a price. Code never names a model. The config
  refuses to load if a model has no price or a tier names an unknown provider.
- The gateway (`gateway/gateway.py`) is the only path to a provider. For each call:
  1. estimate input tokens conservatively (3 characters per token),
  2. grant `max_output_tokens = min(tier max, tokens left - input estimate, USD left / output
     price)`; if that is under 32 tokens, raise `BudgetExhausted` *without* calling,
  3. cap the timeout at the time left before the run deadline,
  4. on 429, back off exponentially (honoring `Retry-After`), emitting a `waiting` event,
     but never sleeping past the deadline; if waiting would pass the deadline, go straight
     to the fallback route,
  5. on any other provider error or timeout, try the fallback route,
  6. charge actual usage and record an `llm_call` event (model, tokens, USD, latency).
- After a node finishes, the runtime writes the new run budget into state (a `system` patch).
  The code guards (ADR 0004) then wind down the run at 10% and stop it at zero.
- If the supervisor cannot even afford a routing call, it follows the same wind-down path
  as the 10% guard (final reviewer summary, then END).

## Consequences

- Good: a run cannot spend more than its budget. The worst case of every call is paid for
  before it is made. `test_tiny_budget_never_overspends` checks this end to end.
- Good: a retired model is a config change plus a failing availability check, not a code
  change.
- Cost: the conservative input estimate refuses some calls that would have fit. That is
  acceptable for a hard cap.
- Cost: per-node caps are guesses until M4's eval data shows real per-node usage.
- Open (M3): provider-reported usage can differ slightly from our own estimate. We charge the
  provider's numbers, and clamp the stored remaining budget at zero.

## Amendment (M3, 2026-09-28): real providers

- Provider: Groq (OpenAI-compatible API), free tier, chosen because the owner's RAG project
  already uses it and it needs no card.
- Verified against the live model list on 2026-09-28 and pinned in `config/models.toml`:
  - `fast` (supervisor): `openai/gpt-oss-20b`, fallback `qwen/qwen3.8-27b`;
  - `strong` (workers, judge): `openai/gpt-oss-120b`, fallback `qwen/qwen3.8-27b`.
  Prices from Groq's model page on the same date. Workers run `verify_models()` at startup and
  refuse to start if a configured model has disappeared.
- Both routes are on one provider, so the fallback covers a retired or overloaded *model*,
  not a Groq outage. Adding a second provider is a config change.
- Free-tier limits observed in practice, which shape everything else: about 8,000 tokens per
  minute per model, and 200,000 tokens per day per model. A research-memo run uses roughly
  15-40k tokens, so rate-limit waits dominate wall-clock time (a run took 5-6 minutes, most of
  it in backoff) and a day's quota covers only a handful of runs. The gateway handled a
  `Retry-After` of 43 minutes correctly: waiting would pass the run deadline, so it went
  straight to the fallback route.
- The demo therefore also caps *tokens* per day (`DAILY_TOKEN_CAP`), not only USD: on a free
  tier the token quota binds long before the dollar cap does.

## Amendment (M4, 2026-09-29): a request-size level in the waterfall

The first full eval run failed with HTTP 413 "request too large": Groq's free tier enforces its
tokens-per-minute limit (8,000) on each request and counts the reserved `max_tokens` against
it. The researcher's prompt (~5.4k tokens with search results) plus a 3,000-token output
reservation was 8,416 tokens.

The fix follows the same waterfall idea one level down. A tier can declare
`max_request_tokens`, and the output reservation becomes
`min(tier max_output, max_request_tokens - estimated input)`. If the prompt alone leaves no
room, the gateway raises `RequestTooLarge` without calling the provider. Prompts were also
made leaner: snippets in state views and tool results shown to the model are truncated (full
text stays in state). Both behaviors are unit-tested.

## Amendment (M4, 2026-09-29): three kinds of provider errors, three responses

The first eval runs surfaced a failure class the gateway did not distinguish: the provider
itself rejects what the *model* generated. Groq returns HTTP 400 with `json_validate_failed`
(the output is not valid JSON, often because `gpt-oss` spent its whole output budget on hidden
reasoning), `tool_use_failed` (a tool call that does not match its schema, e.g.
`max_results: 10` against a maximum of 6) or `output_parse_failed`. The gateway now handles
three classes differently:

| Class | Examples | Response |
|---|---|---|
| Rate limit | 429 | Exponential backoff with `Retry-After`, never past the deadline; then fallback |
| Invalid model output | 400 `json_validate_failed`, `tool_use_failed`, `output_parse_failed` | Resample on the *same* model (up to 2 times), charging the worst-case usage, since none is reported; then fallback |
| Provider failure | 5xx, timeouts, transport errors, other 4xx | Straight to the fallback route |

Related changes to keep prompts inside free-tier request limits, for both the multi-agent
graph and the baseline alike: `gpt-oss` runs with `reasoning_effort = "low"`; the supervisor
sees source titles only (routing does not need the text); state views list at most 20 recent
sources; tool loops shorten tool results older than the last three ("context compaction");
search tools accept any `max_results`/`top_k` but clamp to 5 in code (liberal in what they
accept, strict in what they do).
