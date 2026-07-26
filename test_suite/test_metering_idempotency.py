"""
Definition of Done: "Idempotent metering: a billable action records exactly
one usage event even under retries (dedupe by request/idempotency key).
Proven by a test."
"""
from __future__ import annotations

from app.config import USAGE_TYPE_API_CALL
from app.metering import record
from app.models import UsageEvent


def test_retried_request_does_not_double_count(db_session, free_tenant):
    key = "req-abc-123"

    first = record(
        db_session,
        tenant=free_tenant,
        usage_type=USAGE_TYPE_API_CALL,
        quantity=1,
        idempotency_key=key,
    )
    second = record(
        db_session,
        tenant=free_tenant,
        usage_type=USAGE_TYPE_API_CALL,
        quantity=1,
        idempotency_key=key,
    )

    assert first.deduped is False
    assert second.deduped is True
    assert first.usage_event_id == second.usage_event_id

    count = (
        db_session.query(UsageEvent)
        .filter_by(tenant_id=free_tenant.id, idempotency_key=key)
        .count()
    )
    assert count == 1, "retried request must not create a second usage_events row"


def test_retried_request_many_times_still_one_row(db_session, free_tenant):
    key = "req-retry-storm"
    for _ in range(10):
        record(
            db_session,
            tenant=free_tenant,
            usage_type=USAGE_TYPE_API_CALL,
            quantity=1,
            idempotency_key=key,
        )

    count = (
        db_session.query(UsageEvent)
        .filter_by(tenant_id=free_tenant.id, idempotency_key=key)
        .count()
    )
    assert count == 1


def test_different_idempotency_keys_are_separate_events(db_session, free_tenant):
    record(db_session, tenant=free_tenant, usage_type=USAGE_TYPE_API_CALL, quantity=1, idempotency_key="key-1")
    record(db_session, tenant=free_tenant, usage_type=USAGE_TYPE_API_CALL, quantity=1, idempotency_key="key-2")

    count = db_session.query(UsageEvent).filter_by(tenant_id=free_tenant.id).count()
    assert count == 2


def test_same_idempotency_key_isolated_per_tenant(db_session, free_tenant, pro_tenant):
    """Two different tenants using the same idempotency key string must not
    collide -- the uniqueness is (tenant_id, idempotency_key), not just key."""
    key = "shared-key-string"
    r1 = record(db_session, tenant=free_tenant, usage_type=USAGE_TYPE_API_CALL, quantity=1, idempotency_key=key)
    r2 = record(db_session, tenant=pro_tenant, usage_type=USAGE_TYPE_API_CALL, quantity=1, idempotency_key=key)

    assert r1.deduped is False
    assert r2.deduped is False
    assert r1.usage_event_id != r2.usage_event_id
