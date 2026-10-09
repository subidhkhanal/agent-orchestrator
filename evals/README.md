# Evaluation: Graph A vs single-agent baseline

**Status: incomplete; no results are claimed.** A full run was started on Groq's free tier
(`config/models.groq.toml`) on 2026-09-29 and stopped when the demo moved to Claude.

`evals/run_eval.py` runs each task in `evals/tasks.jsonl` (30 tasks: 10 answerable from the
Document Q&A platform, 8 needing web search, 6 needing both, and 6 impossible or adversarial)
through Graph A and the single-agent baseline. Both get the same tools and the same budget
(60k tokens and $0.05 per run on Groq; set in `run_eval.py`). For each run it reports:
- task success (an LLM judge with a rubric, plus deterministic reference-fact checks);
- citation validity;
- cost, wall-clock time, and p95 latency per node;
- supervisor routing validity;
- runs over budget.

It also writes a spot-check file for manual review.

On Groq's free tier, what limited the full run was API quota, not code: about 200k tokens
per model per day, and a run uses about 50-60k tokens, mostly on `gpt-oss-120b`. The eval
therefore ran with `--resume --wait-for-quota`:
- when the daily quota runs out it pauses until the reset instead of recording a failure;
- a run cut off by the quota is discarded and redone later;
- tasks are interleaved across categories, so partial results stay balanced.

During the eval, fallback models are disabled so that every run uses the same models. The
current partial tables can be rebuilt at any time with `python evals/run_eval.py
--summarize-only`.

Smoke runs so far (n=2 tasks, **not results**, for transparency only):
- `rag-parental`: Graph A succeeded (judge 5/5, 80% citation validity, $0.016, 338 s; most
  of the time was rate-limit backoff). The baseline did not (judge 2/5, 14% citation
  validity, $0.018).
- `none-ceo` (impossible task): Graph A correctly did not invent a CEO profile, but ended
  without a memo explaining why, so it was scored as a failure. The baseline hit the daily
  token quota and failed.

Changes made because of the smoke runs, disclosed because they were informed by eval tasks:
1. **Citation syntax normalization.** `gpt-oss` cites in its native `【id】` format, so
   syntax is now normalized in code (ADR 0004 amendment).
2. **Supervisor prompt.** It now tells the supervisor to have the writer state what could not
   be found, instead of ending silently.
3. **Judge input.** The eval grades the research summary when a run produced no memo; this
   applies to both systems.

Expected trade-off (to be confirmed or refuted by the full run): Graph A costs more tokens per
task than one agent, because the supervisor re-reads state every step and the reviewer adds a
pass. It should win on citation validity, because a separate reviewer plus a code-level
citation check reject unsupported claims. On simple single-fact tasks the extra cost may
buy nothing.
