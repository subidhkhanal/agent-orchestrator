# ADR 0004: Safety invariants are enforced in code, not in prompts

- Status: accepted (M1)
- Date: 2026-09-28

## Context

The supervisor is an LLM choosing the next node. Some routing choices must never happen,
whatever the model says:

1. `publish` must be unreachable unless a human approved the *current* artifact version.
2. The coder must not draft before at least one source exists (except for code-only tasks).
3. Below 10% of the token or USD budget, the run must wrap up with a final reviewer summary
   and end.
4. At zero budget the run must end, keeping the partial artifact.

Models follow instructions most of the time, not all of the time. Prompt injection from a
retrieved web page can also push a model to "just publish it".

## Options considered

1. **Prompt-only rules.** Put the rules in the supervisor's system prompt.
   - No extra code. But it is probabilistic: a rule that holds 99% of the time fails
     about once in every 100 runs, and our worst case is publishing unapproved content.
2. **Constrained decoding only.** Give the model a schema whose `next_node` enum is
   computed from state (for example, leave `publish` out until approval).
   - Good at preventing invalid output, but it depends on provider support for strict
     schemas, and it hides the reason for a refusal from the audit log.
3. **Code guards after the decision** (chosen), with the rules also described in the prompt
   so the model rarely needs correcting.

## Decision

- `agents/guards.py` holds pure functions that take (state, proposed route) and return the
  route that is allowed:
  - *Forced guards* (zero budget, under 10% budget, step cap) ignore the proposal. If one
    applies, the supervisor does not call the model at all, which also saves the call.
  - *Route guards* (publish needs approval, human gate needs an artifact, coder needs a
    source) redirect one proposal. They are applied until nothing changes, so an absurd
    proposal (for example `publish` on an empty run) chains back to `researcher`.
- Every override is logged as a `guard_override` event with the proposed and forced routes.
- Defense in depth: the `publish` node checks the approval invariant again right before the
  side effect, and the `await_approval` node checks the decision's gate id matches the
  pending gate.
- The reviewer's "pass" verdict is also backstopped in code: if the deterministic citation
  check finds uncited claims or unknown source ids, the verdict becomes
  `changes_requested`.
- Each invariant has its own unit test (`tests/unit/test_guards.py`), plus end-to-end tests
  where the fake model deliberately proposes forbidden routes.

## Consequences

- Good: the invariants hold at 100% under test, independent of model quality or prompt
  injection.
- Good: guard overrides are visible in the audit log. A high override rate is a signal that
  the prompt or model tier needs work (this becomes an eval metric in M4).
- Cost: the rules exist in two places (prompt text and code). The code is the source of
  truth; the prompt copy only reduces wasted overrides.
- Cost: code guards only cover what we thought of in advance. They are not a substitute for
  review of new graph designs.

## Amendment (M3, 2026-09-28): what the first real-model run taught us

The first run against `gpt-oss-120b` produced a correct memo whose every citation was written
in the model's native style, `【doc_94e09c6b】`, instead of `[doc_94e09c6b]`. The strict
citation check rejected every line, the reviewer kept requesting changes, and the budget guard
ended the run. Two fixes, both in code:
- `normalize_citations()` rewrites known citation *syntaxes* to the canonical form when an
  artifact is written. It never makes an unknown source id valid; the check itself stays
  strict (tested).
- The prompt now states the exact format. The prompt reduces how often normalization is
  needed; the code makes the check independent of it.

## Amendment (2026-09-29): two more rules moved from prompt to code after a live run

A live demo run showed two prompt-level rules failing with a fast model on a free tier:
- The supervisor answered "coder" four times in a row, so the memo was rewritten 10 times and
  never reviewed until the budget guard ended the run. New route guard
  `review_before_rewrite`: the current draft must be reviewed before the coder may rewrite it.
- The researcher never called `rag_query`, so internal documents were never consulted. The
  researcher node now queries the knowledge base with the task in code, through its own tool
  router, before the model starts.
The coder's turn also ends as soon as it saves a draft. Each case has a regression test that
reproduces the live behavior with the fake model.
