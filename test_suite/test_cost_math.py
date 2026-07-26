"""
Definition of Done: "Cost computation: usage rolls up to a monthly cost;
AI token cost correct (pinned constants, cached-input + reasoning rules)."

These tests are PINNED against the literal constants in app/config.py.
If someone changes a price there, these tests should be updated deliberately
in the same commit -- that's the point of pinning.
"""
from __future__ import annotations

from app.config import (
    USAGE_TYPE_API_CALL,
    USAGE_TYPE_AI_TOKEN_INPUT,
    USAGE_TYPE_AI_TOKEN_INPUT_CACHED,
    USAGE_TYPE_AI_TOKEN_OUTPUT,
)
from app.cost import price_for, rollup_cost
from app.metering import record


def test_pinned_price_per_million_api_calls():
    # $1,000 / 1M calls -> 1,000 calls costs exactly $1.00
    assert price_for(USAGE_TYPE_API_CALL, 1_000) == 1.0


def test_pinned_price_fresh_input_tokens():
    # $3.00 / 1M input tokens -> 1,000,000 tokens costs exactly $3.00
    assert price_for(USAGE_TYPE_AI_TOKEN_INPUT, 1_000_000) == 3.0


def test_cached_input_tokens_are_cheaper_than_fresh(db_session, free_tenant):
    """Gotcha #1: cached-input tokens must be billed at the cached rate,
    not the fresh-input rate. Same quantity, different (lower) cost."""
    fresh_cost = price_for(USAGE_TYPE_AI_TOKEN_INPUT, 1_000_000)
    cached_cost = price_for(USAGE_TYPE_AI_TOKEN_INPUT_CACHED, 1_000_000)

    assert cached_cost < fresh_cost
    assert cached_cost == 0.30
    assert fresh_cost == 3.00


def test_reasoning_tokens_are_not_a_separate_line_item(db_session, free_tenant):
    """Gotcha #2: 'reasoning' tokens are part of OUTPUT tokens, billed once
    at the output rate. This service models that by never having a separate
    reasoning usage_type -- all output-like tokens (including reasoning)
    are recorded as USAGE_TYPE_AI_TOKEN_OUTPUT and billed exactly once.

    This test proves that recording 1,000 "output" tokens (which in a real
    provider call would include some reasoning tokens) costs exactly the
    output rate for 1,000 tokens -- not double, not a separate add-on.
    """
    record(
        db_session,
        tenant=free_tenant,
        usage_type=USAGE_TYPE_AI_TOKEN_OUTPUT,
        quantity=1_000,
        idempotency_key="output-with-reasoning-1",
    )

    rollup = rollup_cost(db_session, free_tenant.id, plan_name=free_tenant.plan_name)

    expected_cost = price_for(USAGE_TYPE_AI_TOKEN_OUTPUT, 1_000)
    assert rollup.cost_breakdown_usd[USAGE_TYPE_AI_TOKEN_OUTPUT] == round(expected_cost, 6)
    # Exactly one cost line for output tokens -- no separate "reasoning" key exists.
    assert "reasoning" not in rollup.cost_breakdown_usd
    assert "ai_token_reasoning" not in rollup.cost_breakdown_usd


def test_rollup_used_limit_cost_add_up_exactly(db_session, free_tenant):
    record(db_session, tenant=free_tenant, usage_type=USAGE_TYPE_API_CALL, quantity=500, idempotency_key="calls-1")
    record(
        db_session,
        tenant=free_tenant,
        usage_type=USAGE_TYPE_AI_TOKEN_INPUT,
        quantity=10_000,
        idempotency_key="in-1",
    )
    record(
        db_session,
        tenant=free_tenant,
        usage_type=USAGE_TYPE_AI_TOKEN_INPUT_CACHED,
        quantity=20_000,
        idempotency_key="cached-1",
    )
    record(
        db_session,
        tenant=free_tenant,
        usage_type=USAGE_TYPE_AI_TOKEN_OUTPUT,
        quantity=5_000,
        idempotency_key="out-1",
    )

    rollup = rollup_cost(db_session, free_tenant.id, plan_name=free_tenant.plan_name)

    assert rollup.api_calls_used == 500
    assert rollup.api_calls_limit == 1_000
    assert rollup.ai_tokens_used == 10_000 + 20_000 + 5_000
    assert rollup.ai_tokens_limit == 100_000

    expected_total = (
        price_for(USAGE_TYPE_API_CALL, 500)
        + price_for(USAGE_TYPE_AI_TOKEN_INPUT, 10_000)
        + price_for(USAGE_TYPE_AI_TOKEN_INPUT_CACHED, 20_000)
        + price_for(USAGE_TYPE_AI_TOKEN_OUTPUT, 5_000)
    )
    assert rollup.total_cost_usd == round(expected_total, 6)
    # The breakdown itself must sum to the total -- no silently dropped or
    # double-added component.
    assert round(sum(rollup.cost_breakdown_usd.values()), 6) == rollup.total_cost_usd


def test_deduped_retry_does_not_inflate_cost(db_session, free_tenant):
    """A retried billable call (same idempotency key) must not be billed twice."""
    key = "retry-cost-check"
    for _ in range(5):
        record(
            db_session,
            tenant=free_tenant,
            usage_type=USAGE_TYPE_AI_TOKEN_OUTPUT,
            quantity=2_000,
            idempotency_key=key,
        )

    rollup = rollup_cost(db_session, free_tenant.id, plan_name=free_tenant.plan_name)
    expected = price_for(USAGE_TYPE_AI_TOKEN_OUTPUT, 2_000)  # billed once, not 5x
    assert rollup.cost_breakdown_usd[USAGE_TYPE_AI_TOKEN_OUTPUT] == round(expected, 6)
