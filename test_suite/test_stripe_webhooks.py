"""
Definition of Done: "a forged webhook rejected; a duplicate webhook ignored."

These tests exercise app/billing.py directly using stripe's own test helper
(stripe.WebhookSignature.generate_header) to build a *real* valid signature,
so "forged" here genuinely means "signature does not match", not just "we
didn't call the verify function".
"""
from __future__ import annotations

import json
import time

import pytest
import stripe

from app.config import STRIPE_WEBHOOK_SECRET
from app.billing import verify_webhook, handle_webhook_event, WebhookVerificationError
from app.models import Tenant, ProcessedWebhookEvent, Subscription


def _signed_payload(event_dict: dict, secret: str = STRIPE_WEBHOOK_SECRET) -> tuple[bytes, str]:
    payload = json.dumps(event_dict).encode("utf-8")
    timestamp = int(time.time())
    header = stripe.WebhookSignature._compute_signature(
        f"{timestamp}.{payload.decode('utf-8')}", secret
    )
    sig_header = f"t={timestamp},v1={header}"
    return payload, sig_header


def _checkout_completed_event(event_id: str, tenant_id: str, plan_name: str = "pro") -> dict:
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "customer": "cus_test_123",
                "subscription": "sub_test_123",
                "client_reference_id": tenant_id,
                "metadata": {"tenant_id": tenant_id, "plan_name": plan_name},
            }
        },
    }


def test_forged_webhook_signature_is_rejected():
    event_dict = _checkout_completed_event("evt_forged_1", "tenant-doesnt-matter")
    payload = json.dumps(event_dict).encode("utf-8")
    forged_sig_header = "t=1234567890,v1=deadbeefnotarealsignature"

    with pytest.raises(WebhookVerificationError):
        verify_webhook(payload, forged_sig_header)


def test_missing_signature_header_is_rejected():
    event_dict = _checkout_completed_event("evt_forged_2", "tenant-doesnt-matter")
    payload = json.dumps(event_dict).encode("utf-8")

    with pytest.raises(WebhookVerificationError):
        verify_webhook(payload, None)


def test_valid_signature_is_accepted_and_updates_tenant_plan(db_session, free_tenant):
    event_dict = _checkout_completed_event("evt_valid_1", free_tenant.id, plan_name="pro")
    payload, sig_header = _signed_payload(event_dict)

    event = verify_webhook(payload, sig_header)
    result = handle_webhook_event(db_session, event)

    assert result["status"] == "processed"
    db_session.refresh(free_tenant)
    assert free_tenant.plan_name == "pro"

    sub = db_session.query(Subscription).filter_by(tenant_id=free_tenant.id).one()
    assert sub.status == "active"
    assert sub.stripe_subscription_id == "sub_test_123"


def test_duplicate_webhook_delivery_is_ignored(db_session, free_tenant):
    """Stripe explicitly documents it may deliver the same event more than
    once. The second delivery of the same event.id must be a no-op."""
    event_dict = _checkout_completed_event("evt_duplicate_1", free_tenant.id, plan_name="pro")
    payload, sig_header = _signed_payload(event_dict)

    event = verify_webhook(payload, sig_header)
    first_result = handle_webhook_event(db_session, event)
    second_result = handle_webhook_event(db_session, event)

    assert first_result["status"] == "processed"
    assert second_result["status"] == "duplicate_ignored"

    # Only one ProcessedWebhookEvent row and one Subscription row exist,
    # proving the side effects were not re-applied.
    processed_count = db_session.query(ProcessedWebhookEvent).filter_by(id="evt_duplicate_1").count()
    assert processed_count == 1
    sub_count = db_session.query(Subscription).filter_by(tenant_id=free_tenant.id).count()
    assert sub_count == 1


def test_subscription_deleted_downgrades_tenant_to_free(db_session, pro_tenant):
    pro_tenant.stripe_customer_id = "cus_test_456"
    db_session.commit()

    sub = Subscription(
        tenant_id=pro_tenant.id,
        stripe_subscription_id="sub_test_456",
        stripe_customer_id="cus_test_456",
        plan_name="pro",
        status="active",
    )
    db_session.add(sub)
    db_session.commit()

    event_dict = {
        "id": "evt_sub_deleted_1",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": "sub_test_456",
                "customer": "cus_test_456",
                "status": "canceled",
            }
        },
    }
    payload, sig_header = _signed_payload(event_dict)
    event = verify_webhook(payload, sig_header)
    handle_webhook_event(db_session, event)

    db_session.refresh(pro_tenant)
    assert pro_tenant.plan_name == "free"
