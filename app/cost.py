"""
Cost math: rolls up usage_events into {used, limit, cost}.

The two AI-token gotchas this deliberately gets right (see config.py):
  1. Cached input tokens are billed at the cheaper cached rate, not the
     normal input rate -- summing all "input-like" tokens at one price
     overcharges the customer for cache hits.
  2. Reasoning tokens are NOT a separate line item. In this model they are
     simply recorded under USAGE_TYPE_AI_TOKEN_OUTPUT (same as any other
     output token) and billed once, at the output rate. There is no
     separate "reasoning_tokens" price -- adding one would double-count
     tokens that are already part of the output total.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import (
    PLANS,
    PRICING_PER_MILLION_USD,
    CALL_USAGE_TYPES,
    TOKEN_USAGE_TYPES,
)
from app.models import UsageEvent


@dataclass(frozen=True)
class UsageRollup:
    tenant_id: str
    plan_name: str
    period_start: datetime
    period_end: datetime
    api_calls_used: int
    api_calls_limit: int
    ai_tokens_used: int
    ai_tokens_limit: int
    cost_breakdown_usd: dict[str, float]
    total_cost_usd: float


def current_billing_period(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Calendar-month billing period in UTC: [first-of-month, first-of-next-month)."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def price_for(usage_type: str, quantity: int) -> float:
    """Exact cost in USD for `quantity` units of `usage_type`, using the
    pinned per-million prices. Kept as a pure function so it's trivially
    unit-testable and reusable outside of a full rollup."""
    per_million = PRICING_PER_MILLION_USD[usage_type]
    return (quantity / 1_000_000) * per_million


def rollup_cost(
    db: Session,
    tenant_id: str,
    *,
    plan_name: str,
    now: datetime | None = None,
) -> UsageRollup:
    period_start, period_end = current_billing_period(now)
    plan = PLANS[plan_name]

    sums = (
        db.query(UsageEvent.usage_type, func.sum(UsageEvent.quantity))
        .filter(
            UsageEvent.tenant_id == tenant_id,
            UsageEvent.created_at >= period_start,
            UsageEvent.created_at < period_end,
        )
        .group_by(UsageEvent.usage_type)
        .all()
    )
    qty_by_type: dict[str, int] = {usage_type: int(qty) for usage_type, qty in sums}

    cost_breakdown: dict[str, float] = {}
    for usage_type, qty in qty_by_type.items():
        cost_breakdown[usage_type] = round(price_for(usage_type, qty), 6)

    api_calls_used = sum(qty_by_type.get(t, 0) for t in CALL_USAGE_TYPES)
    ai_tokens_used = sum(qty_by_type.get(t, 0) for t in TOKEN_USAGE_TYPES)
    total_cost = round(sum(cost_breakdown.values()), 6)

    return UsageRollup(
        tenant_id=tenant_id,
        plan_name=plan_name,
        period_start=period_start,
        period_end=period_end,
        api_calls_used=api_calls_used,
        api_calls_limit=plan.api_call_quota,
        ai_tokens_used=ai_tokens_used,
        ai_tokens_limit=plan.ai_token_quota,
        cost_breakdown_usd=cost_breakdown,
        total_cost_usd=total_cost,
    )
