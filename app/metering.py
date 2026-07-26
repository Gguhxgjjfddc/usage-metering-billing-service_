"""
MeterService: the correctness-critical core of the whole project.

Guarantee: calling record() N times with the SAME (tenant_id, idempotency_key)
results in exactly ONE usage_events row, no matter how many times a client
retries. This is enforced at the database level via a UNIQUE constraint on
(tenant_id, idempotency_key), not just "checked in application code" -- a
race between two concurrent retries is caught by the DB, not a TOCTOU bug in
Python.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import PLANS, CALL_USAGE_TYPES, TOKEN_USAGE_TYPES, USAGE_TYPES
from app.models import Tenant, UsageEvent
from app.cost import rollup_cost


class QuotaExceededError(Exception):
    """Raised when a metering request would push the tenant over their plan
    quota. Carries enough info for the API layer to pick 402 vs 429."""

    def __init__(self, message: str, *, kind: str):
        super().__init__(message)
        self.kind = kind  # "quota" -> 429, "payment_required" -> 402


@dataclass(frozen=True)
class MeterResult:
    usage_event_id: str
    deduped: bool  # True if this call reused an existing event (retry)
    usage_type: str
    quantity: int


def _bucket_for(usage_type: str) -> str:
    if usage_type in CALL_USAGE_TYPES:
        return "api_call_quota"
    if usage_type in TOKEN_USAGE_TYPES:
        return "ai_token_quota"
    raise ValueError(f"Unknown usage_type: {usage_type}")


def check_quota(db: Session, tenant: Tenant, usage_type: str, additional_qty: int) -> None:
    """Raise QuotaExceededError if recording `additional_qty` more of
    `usage_type` would put the tenant over their plan's limit this month.

    Status-code policy (Definition of Done: "honest status codes"):
      - Free plan over limit  -> 402 Payment Required (they need to upgrade)
      - Paid plan over limit  -> 429 Too Many Requests (rate/quota limited,
        e.g. mid-cycle burst; they are already paying, this is a throttle)
    """
    plan = PLANS.get(tenant.plan_name)
    if plan is None:
        raise ValueError(f"Tenant has unknown plan: {tenant.plan_name}")

    bucket = _bucket_for(usage_type)
    limit = plan.api_call_quota if bucket == "api_call_quota" else plan.ai_token_quota

    rollup = rollup_cost(db, tenant.id, plan_name=tenant.plan_name)
    used = rollup.api_calls_used if bucket == "api_call_quota" else rollup.ai_tokens_used

    if used + additional_qty > limit:
        if tenant.plan_name == "free":
            raise QuotaExceededError(
                f"Free plan limit reached for {bucket} "
                f"({used}/{limit}); upgrade to Pro to continue.",
                kind="payment_required",
            )
        raise QuotaExceededError(
            f"Plan limit reached for {bucket} ({used}/{limit}) this billing period.",
            kind="quota",
        )


def record(
    db: Session,
    *,
    tenant: Tenant,
    usage_type: str,
    quantity: int,
    idempotency_key: str,
    enforce_quota: bool = True,
) -> MeterResult:
    """Record one billable usage event, idempotently.

    Order of operations matters:
      1. Check for an existing event with this idempotency key FIRST. If
         found, return it as a dedupe -- we do not re-check quota, because
         the original call already passed (or failed) that check, and a
         retry must be a pure no-op, not a second quota decision.
      2. Only if it's a genuinely new key do we check the quota and insert.
      3. The INSERT is also protected by a DB unique constraint, so even a
         concurrent double-submit race is caught (IntegrityError -> treat
         as dedupe).
    """
    if usage_type not in USAGE_TYPES:
        raise ValueError(f"Unknown usage_type: {usage_type}")
    if quantity <= 0:
        raise ValueError("quantity must be positive")

    existing = (
        db.query(UsageEvent)
        .filter_by(tenant_id=tenant.id, idempotency_key=idempotency_key)
        .one_or_none()
    )
    if existing is not None:
        return MeterResult(
            usage_event_id=existing.id,
            deduped=True,
            usage_type=existing.usage_type,
            quantity=existing.quantity,
        )

    if enforce_quota:
        check_quota(db, tenant, usage_type, quantity)

    event = UsageEvent(
        tenant_id=tenant.id,
        usage_type=usage_type,
        quantity=quantity,
        idempotency_key=idempotency_key,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race against a concurrent identical request: the other
        # request's row is now the row of record. Treat this as a dedupe.
        db.rollback()
        existing = (
            db.query(UsageEvent)
            .filter_by(tenant_id=tenant.id, idempotency_key=idempotency_key)
            .one()
        )
        return MeterResult(
            usage_event_id=existing.id,
            deduped=True,
            usage_type=existing.usage_type,
            quantity=existing.quantity,
        )

    db.refresh(event)
    return MeterResult(
        usage_event_id=event.id,
        deduped=False,
        usage_type=event.usage_type,
        quantity=event.quantity,
    )
