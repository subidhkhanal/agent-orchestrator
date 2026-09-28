from __future__ import annotations

import pytest

from orchestrator.gateway.budget import MIN_OUTPUT_TOKENS, BudgetExhausted, BudgetLedger
from orchestrator.gateway.config import NodeConfig, Price
from orchestrator.gateway.types import Usage
from tests.factories import make_budget

PRICE = Price(input_per_mtok=3.0, output_per_mtok=15.0)
FREE = Price(input_per_mtok=0.0, output_per_mtok=0.0)


def node_cfg(tokens: int, usd: float) -> NodeConfig:
    return NodeConfig(tier="strong", max_tokens_per_node=tokens, max_usd_per_node=usd)


def test_node_slice_is_min_of_node_cap_and_run_remaining() -> None:
    run = make_budget(tokens=10_000, usd=0.05)
    capped_by_node = BudgetLedger.for_node(run, node_cfg(tokens=2_000, usd=1.0))
    assert (capped_by_node.tokens_available, capped_by_node.usd_available) == (2_000, 0.05)
    capped_by_run = BudgetLedger.for_node(run, node_cfg(tokens=50_000, usd=0.01))
    assert (capped_by_run.tokens_available, capped_by_run.usd_available) == (10_000, 0.01)


def test_grant_is_capped_by_requested_tokens_and_usd() -> None:
    ledger = BudgetLedger(tokens_available=100_000, usd_available=1.0, deadline_at=None)
    assert ledger.grant(est_input_tokens=1_000, requested_output=2_000, price=PRICE) == 2_000

    # Token headroom: 1_500 left - 1_000 input = 500.
    ledger = BudgetLedger(tokens_available=1_500, usd_available=1.0, deadline_at=None)
    assert ledger.grant(1_000, 2_000, PRICE) == 500

    # USD headroom: $0.01 - input $0.003 = $0.007 -> 0.007 * 1e6 / 15 = 466 output tokens.
    ledger = BudgetLedger(tokens_available=100_000, usd_available=0.01, deadline_at=None)
    assert ledger.grant(1_000, 2_000, PRICE) == 466


def test_worst_case_spend_of_a_grant_never_exceeds_the_slice() -> None:
    ledger = BudgetLedger(tokens_available=5_000, usd_available=0.02, deadline_at=None)
    for est_input in (10, 500, 1_000, 2_000):
        granted = ledger.grant(est_input, 4_000, PRICE)
        assert est_input + granted <= ledger.tokens_left
        assert PRICE.cost(est_input, granted) <= ledger.usd_left + 1e-12


def test_grant_below_minimum_raises_budget_exhausted() -> None:
    ledger = BudgetLedger(
        tokens_available=1_000 + MIN_OUTPUT_TOKENS - 1, usd_available=1.0, deadline_at=None
    )
    with pytest.raises(BudgetExhausted):
        ledger.grant(1_000, 2_000, PRICE)
    broke = BudgetLedger(tokens_available=100_000, usd_available=0.0, deadline_at=None)
    with pytest.raises(BudgetExhausted):
        broke.grant(10, 100, PRICE)


def test_free_models_are_limited_by_tokens_only() -> None:
    ledger = BudgetLedger(tokens_available=600, usd_available=0.0, deadline_at=None)
    assert ledger.grant(100, 1_000, FREE) == 500


def test_charge_deducts_actual_usage_and_run_budget_waterfalls_down() -> None:
    run = make_budget(tokens=10_000, usd=0.10)
    ledger = BudgetLedger.for_node(run, node_cfg(tokens=5_000, usd=0.05))
    usd = ledger.charge(Usage(input_tokens=1_000, output_tokens=500), PRICE)
    assert usd == pytest.approx(0.003 + 0.0075)
    assert ledger.tokens_left == 3_500
    after = ledger.remaining_after(run)
    assert after.tokens_remaining == 8_500
    assert after.usd_remaining == pytest.approx(0.10 - 0.0105)
    assert after.token_limit == 10_000  # limits never change


def test_remaining_budget_never_goes_negative() -> None:
    run = make_budget(tokens=100, usd=0.001)
    ledger = BudgetLedger.for_node(run, None)
    ledger.charge(Usage(input_tokens=500, output_tokens=500), PRICE)
    after = ledger.remaining_after(run)
    assert (after.tokens_remaining, after.usd_remaining) == (0, 0.0)
