"""
Stripe integration: Checkout session creation + signature-verified,
idempotent webhook processing that keeps Tenant.plan_name and the
Subscription row in sync with Stripe's state of record.

Security/correctness rules encoded here:
  - Every webhook's signature is verified via stripe.Webhook.construct_event
    using the raw request body + Stripe-Signature header. A forged/unsigned
    payload raises SignatureVerificationError and is rejected with 400.
  - Every webhook's `event.id` is checked against ProcessedWebhookEvent
    before any side effect. A duplicate delivery (Stripe *will* redeliver)
    is acknowledged with 200 but does nothing the second time.
  - We only trust Stripe's webhook payload for plan/status changes, never
    a client-supplied "I upgraded" call -- Checkout only *starts* the flow,
    the webhook is what actually flips the tenant's plan.
"""
from __future__ import annotations

import stripe
from sqlalchemy.orm import Session

from app.config import (
    PLANS,
    STRIPE_SECRET_KEY,
    STRIPE_WEBHOOK_SECRET,
    STRIPE_SUCCESS_URL,
    STRIPE_CANCEL_URL,
)
from app.models import Tenant, Subscription, ProcessedWebhookEvent

stripe.api_key = STRIPE_SECRET_KEY


class WebhookVerificationError(Exception):
    pass


def create_checkout_session(db: Session, tenant: Tenant, plan_name: str) -> stripe.checkout.Session:
    """Start a Stripe Checkout session for `tenant` to subscribe to `plan_name`.

    Note: this does NOT change tenant.plan_name. The plan only changes once
    the `checkout.session.completed` webhook arrives -- Checkout completion
    can fail, be abandoned, or (rarely) be delayed, so the webhook is the
    single source of truth for "did they actually subscribe".
    """
    plan = PLANS.get(plan_name)
    if plan is None or plan.stripe_price_id is None:
        raise ValueError(f"Plan '{plan_name}' is not a purchasable Stripe plan")

    if tenant.stripe_customer_id is None:
        customer = stripe.Customer.create(email=tenant.email, name=tenant.name)
        tenant.stripe_customer_id = customer.id
        db.commit()

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=tenant.stripe_customer_id,
        line_items=[{"price": plan.stripe_price_id, "quantity": 1}],
        success_url=STRIPE_SUCCESS_URL,
        cancel_url=STRIPE_CANCEL_URL,
        client_reference_id=tenant.id,
        metadata={"tenant_id": tenant.id, "plan_name": plan_name},
    )
    return session


def verify_webhook(payload: bytes, sig_header: str | None) -> stripe.Event:
    """Verify the Stripe-Signature header against the raw payload.
    Raises WebhookVerificationError on any forged/malformed/missing signature.
    """
    if not sig_header:
        raise WebhookVerificationError("Missing Stripe-Signature header")
    try:
        return stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (stripe.error.SignatureVerificationError, ValueError) as exc:
        raise WebhookVerificationError(str(exc)) from exc


def _mark_processed_or_skip(db: Session, event: stripe.Event) -> bool:
    """Returns True if this is a new event (should be processed),
    False if it's a duplicate delivery (already processed -> skip)."""
    already = db.query(ProcessedWebhookEvent).filter_by(id=event["id"]).one_or_none()
    if already is not None:
        return False
    db.add(ProcessedWebhookEvent(id=event["id"], event_type=event["type"]))
    db.commit()
    return True


def _find_tenant_for_customer(db: Session, stripe_customer_id: str | None) -> Tenant | None:
    if not stripe_customer_id:
        return None
    return db.query(Tenant).filter_by(stripe_customer_id=stripe_customer_id).one_or_none()


def handle_webhook_event(db: Session, event: stripe.Event) -> dict:
    """Dispatch a verified Stripe event. Idempotent: returns early if this
    event.id has already been processed. Handles the three events required
    by the spec:
      - checkout.session.completed
      - customer.subscription.updated
      - customer.subscription.deleted
    """
    is_new = _mark_processed_or_skip(db, event)
    if not is_new:
        return {"status": "duplicate_ignored", "event_id": event["id"]}

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(db, data)
    elif event_type == "customer.subscription.updated":
        _handle_subscription_updated(db, data)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(db, data)
    # Unhandled event types are acknowledged (200) but ignored -- Stripe
    # sends many event types we don't need to act on.

    return {"status": "processed", "event_id": event["id"], "type": event_type}


def _handle_checkout_completed(db: Session, session_obj: dict) -> None:
    tenant_id = (session_obj.get("metadata") or {}).get("tenant_id") or session_obj.get("client_reference_id")
    plan_name = (session_obj.get("metadata") or {}).get("plan_name")
    stripe_customer_id = session_obj.get("customer")
    stripe_subscription_id = session_obj.get("subscription")

    tenant = db.query(Tenant).filter_by(id=tenant_id).one_or_none() if tenant_id else None
    if tenant is None:
        tenant = _find_tenant_for_customer(db, stripe_customer_id)
    if tenant is None:
        return  # Nothing we can safely attribute this to; ignore.

    if plan_name and plan_name in PLANS:
        tenant.plan_name = plan_name
    if stripe_customer_id:
        tenant.stripe_customer_id = stripe_customer_id

    sub = (
        db.query(Subscription)
        .filter_by(stripe_subscription_id=stripe_subscription_id)
        .one_or_none()
    )
    if sub is None:
        sub = Subscription(
            tenant_id=tenant.id,
            stripe_subscription_id=stripe_subscription_id,
            stripe_customer_id=stripe_customer_id,
            plan_name=plan_name or tenant.plan_name,
            status="active",
        )
        db.add(sub)
    else:
        sub.status = "active"
        if plan_name:
            sub.plan_name = plan_name

    db.commit()


def _handle_subscription_updated(db: Session, sub_obj: dict) -> None:
    stripe_subscription_id = sub_obj.get("id")
    status = sub_obj.get("status", "active")
    stripe_customer_id = sub_obj.get("customer")

    sub = (
        db.query(Subscription)
        .filter_by(stripe_subscription_id=stripe_subscription_id)
        .one_or_none()
    )
    tenant = None
    if sub is not None:
        tenant = db.query(Tenant).filter_by(id=sub.tenant_id).one_or_none()
    if tenant is None:
        tenant = _find_tenant_for_customer(db, stripe_customer_id)

    if sub is None and tenant is not None:
        sub = Subscription(
            tenant_id=tenant.id,
            stripe_subscription_id=stripe_subscription_id,
            stripe_customer_id=stripe_customer_id,
            plan_name=tenant.plan_name,
            status=status,
        )
        db.add(sub)
    elif sub is not None:
        sub.status = status

    if tenant is not None:
        # A subscription entering a non-active state (past_due, unpaid,
        # incomplete_expired, canceled) should not silently keep Pro perks.
        if status not in ("active", "trialing"):
            tenant.plan_name = "free"

    db.commit()


def _handle_subscription_deleted(db: Session, sub_obj: dict) -> None:
    stripe_subscription_id = sub_obj.get("id")
    stripe_customer_id = sub_obj.get("customer")

    sub = (
        db.query(Subscription)
        .filter_by(stripe_subscription_id=stripe_subscription_id)
        .one_or_none()
    )
    tenant = None
    if sub is not None:
        sub.status = "canceled"
        tenant = db.query(Tenant).filter_by(id=sub.tenant_id).one_or_none()
    if tenant is None:
        tenant = _find_tenant_for_customer(db, stripe_customer_id)

    if tenant is not None:
        tenant.plan_name = "free"

    db.commit()
