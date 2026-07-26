from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class TenantCreate(BaseModel):
    name: str
    email: str
    plan_name: str = "free"


class TenantOut(BaseModel):
    id: str
    name: str
    email: str
    plan_name: str
    stripe_customer_id: str | None = None

    class Config:
        from_attributes = True


class UsageRecordRequest(BaseModel):
    tenant_id: str
    usage_type: str
    quantity: int = Field(gt=0)
    idempotency_key: str


class UsageRecordResponse(BaseModel):
    usage_event_id: str
    deduped: bool
    usage_type: str
    quantity: int


class UsageRollupResponse(BaseModel):
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


class CheckoutRequest(BaseModel):
    tenant_id: str
    plan_name: str = "pro"


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str
