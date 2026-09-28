"""A scripted, state-driven policy for the fake LLM that plays every role in research-memo.

It makes the same decisions a well-behaved model would, based only on the state view the
real prompt contains. Integration tests and the offline demo use it to drive full runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from orchestrator.gateway.providers.fake import FakeReply
from orchestrator.gateway.types import LLMRequest


def _last_tool_output(request: LLMRequest) -> dict[str, Any]:
    for message in reversed(request.messages):
        if message.role == "tool":
            try:
                data = json.loads(message.content)
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {"items": data}
    return {}


def _memo(view: dict[str, Any], *, flawed: bool) -> str:
    sources = view["sources"]
    lines = [f"# Briefing memo: {view['task']}", "", "## Findings", ""]
    for s in sources:
        lines.append(
            f"- {s['title']} reports the following relevant point: {s['snippet']} [{s['id']}]"
        )
    if flawed:
        lines.append(
            "- Analysts widely expect this trend to accelerate sharply over the next decade."
        )
    if view["human_feedback"]:
        lines += ["", "## Revision notes", ""]
        lines.append(
            "- This version addresses the human reviewer feedback on the previous draft "
            f"and keeps every finding tied to its source [{sources[0]['id']}]"
        )
    return "\n".join(lines) + "\n"


@dataclass
class ResearchMemoPolicy:
    # Make the first memo draft contain an uncited claim, to exercise the review loop.
    flawed_first_draft: bool = False
    drafts_written: int = field(default=0, init=False)

    def __call__(self, request: LLMRequest, model: str) -> FakeReply:
        view: dict[str, Any] = request.metadata.get("view", {})
        handler = getattr(self, f"_{request.node}", None)
        if handler is None:
            return FakeReply(content="ok")
        reply: FakeReply = handler(request, view)
        return reply

    def _supervisor(self, request: LLMRequest, view: dict[str, Any]) -> FakeReply:
        artifact = view["artifact"]
        if (
            artifact
            and view["hitl"]["decision"] == "approved"
            and view["hitl"]["artifact_version"] == artifact["version"]
        ):
            nxt, why = "publish", "the current version is approved"
        elif not view["sources"]:
            nxt, why = "researcher", "no sources yet"
        elif artifact is None:
            nxt, why = "coder", "sources exist, no draft yet"
        elif view["review_verdict"] is None:
            nxt, why = "reviewer", "the current draft has not been reviewed"
        elif view["review_verdict"] == "changes_requested":
            nxt, why = "coder", "the reviewer requested changes"
        else:
            nxt, why = "human_gate", "the reviewer passed the draft"
        return FakeReply(content=json.dumps({"next_node": nxt, "reason": why, "state_patch": {}}))

    def _researcher(self, request: LLMRequest, view: dict[str, Any]) -> FakeReply:
        if request.metadata.get("round", 0) == 0:
            return FakeReply(
                tool_calls=(
                    ("web_search", {"query": view["task"], "max_results": 3}),
                    ("rag_query", {"question": view["task"], "top_k": 2}),
                )
            )
        if not view["sources"]:
            return FakeReply(content="I could not find reliable sources for this task.")
        cited = " ".join(f"[{s['id']}]" for s in view["sources"])
        return FakeReply(content=f"Found {len(view['sources'])} relevant sources. {cited}")

    def _coder(self, request: LLMRequest, view: dict[str, Any]) -> FakeReply:
        saved = any(m.role == "tool" and '"artifact_id"' in m.content for m in request.messages)
        if not saved:
            flawed = self.flawed_first_draft and self.drafts_written == 0
            self.drafts_written += 1
            return FakeReply(
                tool_calls=(("write_artifact", {"content": _memo(view, flawed=flawed)}),)
            )
        return FakeReply(content=f"Saved memo version {view['artifact']['version']}.")

    def _judge(self, request: LLMRequest, view: dict[str, Any]) -> FakeReply:
        verdict = {
            "task_success": True,
            "score": 4,
            "unsupported_claims": 0,
            "correct_abstention": None,
            "rationale": "fake judge",
        }
        return FakeReply(content=json.dumps(verdict))

    def _single_agent(self, request: LLMRequest, view: dict[str, Any]) -> FakeReply:
        tool_outputs = [m.content for m in request.messages if m.role == "tool"]
        if not tool_outputs:
            return self._researcher(request, view)
        if not any('"artifact_id"' in t for t in tool_outputs):
            memo = _memo(view, flawed=False)
            return FakeReply(tool_calls=(("write_artifact", {"content": memo}),))
        return FakeReply(content="Memo written.")

    def _reviewer(self, request: LLMRequest, view: dict[str, Any]) -> FakeReply:
        if request.metadata.get("mode") == "final_summary":
            return FakeReply(content="Out of budget. Partial findings are in the memo draft.")
        round_ = request.metadata.get("round", 0)
        if round_ == 0:
            return FakeReply(tool_calls=(("policy_check", {}),))
        if round_ == 1:
            findings = _last_tool_output(request).get("findings", [])
            if not findings:
                return FakeReply(
                    content=json.dumps({"verdict": "pass", "summary": "All claims cited."})
                )
            return FakeReply(
                tool_calls=tuple(
                    ("add_review_note", {"kind": "unsupported_claim", "text": f["detail"]})
                    for f in findings[:3]
                )
            )
        return FakeReply(
            content=json.dumps(
                {"verdict": "changes_requested", "summary": "Some claims lack citations."}
            )
        )
