from __future__ import annotations

import stripe as stripe_sdk
from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db, init_db
from app.config import PLANS, DEFAULT_PLAN
from app.models import Tenant
from app.metering import record as meter_record, QuotaExceededError
from app.cost import rollup_cost
from app.billing import create_checkout_session, verify_webhook, handle_webhook_event, WebhookVerificationError
from app.schemas import (
    TenantCreate,
    TenantOut,
    UsageRecordRequest,
    UsageRecordResponse,
    UsageRollupResponse,
    CheckoutRequest,
    CheckoutResponse,
)

app = FastAPI(title="Usage Metering & Billing Service")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def _get_tenant_or_404(db: Session, tenant_id: str) -> Tenant:
    tenant = db.query(Tenant).filter_by(id=tenant_id).one_or_none()
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return tenant


# ---------------------------------------------------------------------------
# Tenants
# ---------------------------------------------------------------------------

@app.post("/tenants", response_model=TenantOut, status_code=201)
def create_tenant(payload: TenantCreate, db: Session = Depends(get_db)) -> Tenant:
    if payload.plan_name not in PLANS:
        raise HTTPException(status_code=400, detail=f"Unknown plan '{payload.plan_name}'")
    existing = db.query(Tenant).filter_by(email=payload.email).one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Tenant with this email already exists")

    tenant = Tenant(name=payload.name, email=payload.email, plan_name=payload.plan_name or DEFAULT_PLAN)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    return tenant


@app.get("/tenants/{tenant_id}", response_model=TenantOut)
def get_tenant(tenant_id: str, db: Session = Depends(get_db)) -> Tenant:
    return _get_tenant_or_404(db, tenant_id)


# ---------------------------------------------------------------------------
# Metering
# ---------------------------------------------------------------------------

@app.post("/usage/record", response_model=UsageRecordResponse)
def record_usage(payload: UsageRecordRequest, db: Session = Depends(get_db)) -> UsageRecordResponse:
    tenant = _get_tenant_or_404(db, payload.tenant_id)
    try:
        result = meter_record(
            db,
            tenant=tenant,
            usage_type=payload.usage_type,
            quantity=payload.quantity,
            idempotency_key=payload.idempotency_key,
        )
    except QuotaExceededError as exc:
        status_code = 402 if exc.kind == "payment_required" else 429
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return UsageRecordResponse(
        usage_event_id=result.usage_event_id,
        deduped=result.deduped,
        usage_type=result.usage_type,
        quantity=result.quantity,
    )


@app.get("/usage", response_model=UsageRollupResponse)
def get_usage(tenant_id: str, db: Session = Depends(get_db)) -> UsageRollupResponse:
    tenant = _get_tenant_or_404(db, tenant_id)
    rollup = rollup_cost(db, tenant.id, plan_name=tenant.plan_name)
    return UsageRollupResponse(
        tenant_id=rollup.tenant_id,
        plan_name=rollup.plan_name,
        period_start=rollup.period_start,
        period_end=rollup.period_end,
        api_calls_used=rollup.api_calls_used,
        api_calls_limit=rollup.api_calls_limit,
        ai_tokens_used=rollup.ai_tokens_used,
        ai_tokens_limit=rollup.ai_tokens_limit,
        cost_breakdown_usd=rollup.cost_breakdown_usd,
        total_cost_usd=rollup.total_cost_usd,
    )


# ---------------------------------------------------------------------------
# Stripe billing
# ---------------------------------------------------------------------------

@app.post("/billing/checkout", response_model=CheckoutResponse)
def start_checkout(payload: CheckoutRequest, db: Session = Depends(get_db)) -> CheckoutResponse:
    tenant = _get_tenant_or_404(db, payload.tenant_id)
    try:
        session = create_checkout_session(db, tenant, payload.plan_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except stripe_sdk.error.StripeError as exc:
        raise HTTPException(status_code=502, detail=f"Stripe error: {exc}") from exc

    return CheckoutResponse(checkout_url=session.url, session_id=session.id)


@app.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = verify_webhook(payload, sig_header)
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=400, detail=f"Webhook verification failed: {exc}") from exc

    result = handle_webhook_event(db, event)
    return result


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
