"""Prompts and the compact state view each role sees.

The view is built from RunState by code, so every role sees the same facts. The fake LLM used
in tests reads the same view (from request metadata) to make its scripted decisions.
"""

from __future__ import annotations

import json
from typing import Any

from orchestrator.state.models import RunState

# Prompt size is bounded by provider per-request limits, so views carry short snippets.
SNIPPET_CHARS = 300
MAX_SOURCES_IN_VIEW = 20


def state_view(
    state: RunState, *, memo: str | None = None, snippets: bool = True
) -> dict[str, Any]:
    artifact = state.current_artifact()
    verdict = state.latest_verdict(artifact) if artifact else None
    notes_for_current = [
        {"kind": n.kind, "author": n.author, "text": n.text}
        for n in state.review_notes
        if artifact
        and n.artifact_id == artifact.id
        and n.artifact_version == artifact.version
        and n.kind != "verdict"
    ]
    human_feedback = [
        {"artifact_version": n.artifact_version, "text": n.text}
        for n in state.review_notes
        if n.kind == "human_rejection"
    ]
    view: dict[str, Any] = {
        "task": state.task,
        "task_kind": state.task_kind,
        "constraints": list(state.constraints),
        "sources": [
            {"id": s.id, "title": s.title, "snippet": s.snippet[:SNIPPET_CHARS]}
            if snippets
            else {"id": s.id, "title": s.title}
            for s in state.sources[-MAX_SOURCES_IN_VIEW:]
        ],
        "sources_total": len(state.sources),
        "research_summary": state.research_summary,
        "artifact": (
            {"id": artifact.id, "version": artifact.version, "producer": artifact.producer_agent}
            if artifact
            else None
        ),
        "review_verdict": verdict.verdict if verdict else None,
        "review_notes_for_current_version": notes_for_current,
        "human_feedback": human_feedback,
        "hitl": {
            "decision": state.hitl.decision,
            "artifact_version": state.hitl.artifact_version,
        },
        "open_questions": list(state.open_questions),
        "budget_fraction_left": round(
            min(state.budget.token_fraction, state.budget.usd_fraction), 3
        ),
        "step": state.step,
        "max_steps": state.max_steps,
        "final_summary_mode": state.termination.budget_exhausted
        and not state.termination.final_summary_done,
    }
    if memo is not None:
        view["memo"] = memo
    return view


def render_view(view: dict[str, Any]) -> str:
    return "Current run state:\n```json\n" + json.dumps(view, indent=2) + "\n```"


SUPERVISOR_SYSTEM = """\
You are the supervisor of a small research team. You only route work; you have no tools.

Team:
- researcher: finds sources (web search, internal document search) and summarizes them.
- coder: writes and edits the memo artifact from the sources in state.
- reviewer: checks every claim in the memo is backed by a cited source, flags policy issues.
- human_gate: asks a human to approve the current memo version.
- publish: publishes the approved memo.
- END: stop. Use this when the task is done or nothing useful is left to do.

If the retrieved sources cannot support the task (the information is not in them, or the task
rests on a false premise), do not stop silently: send work to the coder so the memo states
plainly what could not be found, citing only what the sources do say.

Typical flow: researcher -> coder -> reviewer -> (coder <-> reviewer until the reviewer
passes) -> human_gate -> publish. Only send work to human_gate after the reviewer passes the
current version. Only publish after a human approved the current version.

Reply with JSON only, matching this shape:
{"next_node": "<one of the allowed nodes>", "reason": "<one sentence>",
 "state_patch": {"open_questions": [], "constraints": []}}
"""

RESEARCHER_SYSTEM = """\
You are the researcher. Use web_search and rag_query to find sources for the task, and
summarize if a result is long. Every source you retrieve is saved to shared state with an id
like src_... or doc_.... When you are done, reply (without tool calls) with a short research
summary that cites source ids in square brackets, e.g. [src_1a2b3c4d]. If you cannot find
reliable sources, say so plainly; never invent sources.
"""

CODER_SYSTEM = """\
You are the writer ("coder" role). Produce the memo artifact in markdown using write_artifact,
or fix specific sections with edit_section. Rules:
- Use '## ' headings for sections.
- Every factual sentence must cite at least one source id from state in plain ASCII square
  brackets, e.g. [src_1a2b3c4d] or [doc_1a2b3c4d]. One id per bracket. Do not cite ids that
  are not in state, and do not state numbers or facts that the source snippets do not state.
- If sources do not support a point, leave the point out or list it under '## Open questions'.
- Address all reviewer notes and human feedback for the current version.
When the memo is saved, reply with one sentence describing what you changed.
"""

REVIEWER_SYSTEM = """\
You are the reviewer. Run policy_check on the current memo, then record problems with
add_review_note or redline. You have no network access. Finish (without tool calls) with JSON
only: {"verdict": "pass" | "changes_requested", "summary": "<one or two sentences>"}.
Pass only if every claim is supported by a cited source that exists in state and there are
no policy issues.
"""

SINGLE_AGENT_SYSTEM = """\
You are a research assistant working alone. Complete the whole task yourself:
1. Research: use web_search and rag_query to find sources (they are saved with ids like
   src_... or doc_...). Never invent sources.
2. Write: produce a markdown memo with write_artifact. Use '## ' headings. Every factual
   sentence must cite at least one source id from your results in square brackets, e.g.
   [src_1a2b3c4d]. If the sources do not support a point, leave it out or list it under
   '## Open questions'. If nothing reliable was found, say so plainly in the memo.
3. Check: run policy_check and fix problems with edit_section or write_artifact.
When the memo is final, reply (without tool calls) with one sentence summarizing it.
"""

FINAL_SUMMARY_SYSTEM = """\
The run is out of budget. Write a short final summary (at most 120 words) of what was found
and what is still missing, citing source ids in square brackets. Plain text only.
"""
