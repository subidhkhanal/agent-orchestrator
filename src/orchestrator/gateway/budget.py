"""Budget waterfall.

The run budget flows down: a node execution gets min(node cap, run remaining), and each LLM
call inside the node gets at most what the node has left. A call is granted a max_output_tokens
value small enough that even the worst case (every granted token used) stays within both the
token and the USD budget. Actual usage is charged after the call returns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from orchestrator.gateway.config import NodeConfig, Price
from orchestrator.gateway.types import LLMRequest, Usage
from orchestrator.state.models import Budget

# Below this, a call cannot produce anything useful, so we treat the budget as exhausted.
MIN_OUTPUT_TOKENS = 32
# Conservative characters-per-token for the pre-call estimate (real tokenizers average ~4).
CHARS_PER_TOKEN = 3


class BudgetExhausted(Exception):
    pass


class DeadlineExceeded(Exception):
    pass


def estimate_input_tokens(request: LLMRequest) -> int:
    chars = sum(len(m.content) for m in request.messages)
    chars += sum(len(str(c.arguments)) for m in request.messages for c in m.tool_calls)
    chars += sum(len(t.description) + len(str(t.parameters)) for t in request.tools)
    if request.response_schema:
        chars += len(str(request.response_schema))
    return math.ceil(chars / CHARS_PER_TOKEN) + 8 * len(request.messages)


@dataclass
class BudgetLedger:
    """Tracks one node execution's spend against its slice of the run budget."""

    tokens_available: int
    usd_available: float
    deadline_at: datetime | None
    tokens_used: int = 0
    usd_used: float = 0.0

    @classmethod
    def for_node(cls, run_budget: Budget, node: NodeConfig | None) -> BudgetLedger:
        tokens = run_budget.tokens_remaining
        usd = run_budget.usd_remaining
        if node is not None:
            tokens = min(tokens, node.max_tokens_per_node)
            usd = min(usd, node.max_usd_per_node)
        return cls(tokens_available=tokens, usd_available=usd, deadline_at=run_budget.deadline_at)

    @property
    def tokens_left(self) -> int:
        return max(0, self.tokens_available - self.tokens_used)

    @property
    def usd_left(self) -> float:
        return max(0.0, self.usd_available - self.usd_used)

    def grant(self, est_input_tokens: int, requested_output: int, price: Price) -> int:
        """Return the max_output_tokens this call may use, or raise BudgetExhausted."""
        by_tokens = self.tokens_left - est_input_tokens

        input_cost = price.cost(est_input_tokens, 0)
        usd_headroom = self.usd_left - input_cost
        if price.output_per_mtok > 0:
            by_usd = math.floor(usd_headroom * 1e6 / price.output_per_mtok)
        else:
            by_usd = requested_output if usd_headroom >= 0 else 0

        granted = min(requested_output, by_tokens, by_usd)
        if granted < MIN_OUTPUT_TOKENS:
            raise BudgetExhausted(
                f"cannot grant {MIN_OUTPUT_TOKENS} output tokens "
                f"(tokens_left={self.tokens_left}, usd_left={self.usd_left:.6f}, "
                f"est_input={est_input_tokens})"
            )
        return granted

    def charge(self, usage: Usage, price: Price) -> float:
        usd = price.cost(usage.input_tokens, usage.output_tokens)
        self.tokens_used += usage.total
        self.usd_used += usd
        return usd

    def remaining_after(self, run_budget: Budget) -> Budget:
        """The run budget after deducting this node's spend (never below zero)."""
        return run_budget.model_copy(
            update={
                "tokens_remaining": max(0, run_budget.tokens_remaining - self.tokens_used),
                "usd_remaining": max(0.0, run_budget.usd_remaining - self.usd_used),
            }
        )
