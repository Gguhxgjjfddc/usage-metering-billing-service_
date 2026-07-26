"""
Definition of Done: "Quota enforcement: usage checked against the plan's
limit; over-limit refused with an honest status code + message."

Free plan quota (see config.py): 1,000 API calls / 100,000 AI tokens.
"""
from __future__ import annotations

import pytest

from app.config import USAGE_TYPE_API_CALL, PLANS
from app.metering import record, QuotaExceededError


def test_usage_exactly_at_limit_is_allowed(db_session, free_tenant):
    limit = PLANS["free"].api_call_quota
    result = record(
        db_session,
        tenant=free_tenant,
        usage_type=USAGE_TYPE_API_CALL,
        quantity=limit,
        idempotency_key="fill-to-limit",
    )
    assert result.deduped is False  # boundary: exactly at quota must succeed


def test_usage_one_over_limit_is_refused(db_session, free_tenant):
    limit = PLANS["free"].api_call_quota
    record(
        db_session,
        tenant=free_tenant,
        usage_type=USAGE_TYPE_API_CALL,
        quantity=limit,
        idempotency_key="fill-to-limit",
    )

    with pytest.raises(QuotaExceededError) as exc_info:
        record(
            db_session,
            tenant=free_tenant,
            usage_type=USAGE_TYPE_API_CALL,
            quantity=1,
            idempotency_key="one-more-call",
        )
    assert exc_info.value.kind == "payment_required"  # free plan -> 402


def test_pro_plan_over_limit_returns_quota_kind_not_payment(db_session, pro_tenant):
    """Paid plans over-limit should be a 429-style throttle, not a 402
    payment prompt -- they're already paying."""
    limit = PLANS["pro"].api_call_quota
    record(
        db_session,
        tenant=pro_tenant,
        usage_type=USAGE_TYPE_API_CALL,
        quantity=limit,
        idempotency_key="pro-fill",
    )
    with pytest.raises(QuotaExceededError) as exc_info:
        record(
            db_session,
            tenant=pro_tenant,
            usage_type=USAGE_TYPE_API_CALL,
            quantity=1,
            idempotency_key="pro-over",
        )
    assert exc_info.value.kind == "quota"  # -> 429


def test_retry_after_quota_exhaustion_still_dedupes(db_session, free_tenant):
    """A retried request (same idempotency key) that already succeeded
    before quota was exhausted must keep returning its original result,
    not suddenly start raising QuotaExceededError."""
    limit = PLANS["free"].api_call_quota
    key = "already-succeeded"
    first = record(
        db_session,
        tenant=free_tenant,
        usage_type=USAGE_TYPE_API_CALL,
        quantity=limit,
        idempotency_key=key,
    )
    # Retry the exact same request after the tenant is already at quota.
    second = record(
        db_session,
        tenant=free_tenant,
        usage_type=USAGE_TYPE_API_CALL,
        quantity=limit,
        idempotency_key=key,
    )
    assert second.deduped is True
    assert second.usage_event_id == first.usage_event_id
