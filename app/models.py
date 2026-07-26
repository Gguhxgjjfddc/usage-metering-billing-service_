from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    """A billable customer. Every other table is scoped by tenant_id so that
    tenants are fully data-isolated from one another (multi-tenant safety)."""

    __tablename__ = "tenants"

    id = Column(String, primary_key=True, default=_uuid)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False, unique=True)
    plan_name = Column(String, nullable=False, default="free")
    stripe_customer_id = Column(String, nullable=True, unique=True)
    created_at = Column(DateTime(timezone=True), default=_now)

    subscriptions = relationship("Subscription", back_populates="tenant")
    usage_events = relationship("UsageEvent", back_populates="tenant")


class Subscription(Base):
    """Mirrors the Stripe subscription for a tenant. This table is the
    tenant's *billing state of record* on our side; webhooks are the only
    thing allowed to write to it, keeping it in sync with Stripe."""

    __tablename__ = "subscriptions"

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    stripe_subscription_id = Column(String, nullable=True, unique=True)
    stripe_customer_id = Column(String, nullable=True, index=True)
    plan_name = Column(String, nullable=False, default="free")
    status = Column(String, nullable=False, default="active")  # active, past_due, canceled, incomplete...
    current_period_end = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)
    created_at = Column(DateTime(timezone=True), default=_now)

    tenant = relationship("Tenant", back_populates="subscriptions")


class UsageEvent(Base):
    """One billable action. `idempotency_key` is unique PER TENANT so a
    retried request (same key) is a no-op instead of a double charge --
    this is the core correctness guarantee of the whole service."""

    __tablename__ = "usage_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_tenant_idempotency"),
        Index("ix_usage_tenant_type_created", "tenant_id", "usage_type", "created_at"),
    )

    id = Column(String, primary_key=True, default=_uuid)
    tenant_id = Column(String, ForeignKey("tenants.id"), nullable=False, index=True)
    usage_type = Column(String, nullable=False)  # api_call | ai_token_input | ai_token_input_cached | ai_token_output
    quantity = Column(Integer, nullable=False)
    idempotency_key = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, index=True)

    tenant = relationship("Tenant", back_populates="usage_events")


class ProcessedWebhookEvent(Base):
    """Dedupe table for Stripe webhooks. Stripe's `event.id` is globally
    unique and Stripe explicitly documents that the same event can be sent
    more than once, so recording processed ids is what makes our webhook
    handler idempotent."""

    __tablename__ = "processed_webhook_events"

    id = Column(String, primary_key=True)  # Stripe event id, e.g. evt_123
    event_type = Column(String, nullable=False)
    received_at = Column(DateTime(timezone=True), default=_now)
