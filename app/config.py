"""
Central configuration + PINNED cost constants.

Why "pinned": prices must never silently drift mid-test-suite. Every constant
here is a plain literal (no env override) so that tests assert against exact
values and a price change is a deliberate, visible diff in this file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Plans & quotas
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    name: str
    api_call_quota: int          # calls / month
    ai_token_quota: int          # tokens / month
    stripe_price_id: str | None  # None for Free (no Stripe subscription needed)


PLANS: dict[str, Plan] = {
    "free": Plan(
        name="free",
        api_call_quota=1_000,
        ai_token_quota=100_000,
        stripe_price_id=None,
    ),
    "pro": Plan(
        name="pro",
        api_call_quota=50_000,
        ai_token_quota=5_000_000,
        stripe_price_id=os.getenv("STRIPE_PRO_PRICE_ID", "price_pro_test_placeholder"),
    ),
}

DEFAULT_PLAN = "free"


# ---------------------------------------------------------------------------
# Usage types
# ---------------------------------------------------------------------------

USAGE_TYPE_API_CALL = "api_call"
USAGE_TYPE_AI_TOKEN_INPUT = "ai_token_input"          # normal (uncached) input tokens
USAGE_TYPE_AI_TOKEN_INPUT_CACHED = "ai_token_input_cached"
USAGE_TYPE_AI_TOKEN_OUTPUT = "ai_token_output"        # includes reasoning tokens

USAGE_TYPES = {
    USAGE_TYPE_API_CALL,
    USAGE_TYPE_AI_TOKEN_INPUT,
    USAGE_TYPE_AI_TOKEN_INPUT_CACHED,
    USAGE_TYPE_AI_TOKEN_OUTPUT,
}

# Which usage types count against the "ai_token_quota" bucket vs "api_call_quota".
TOKEN_USAGE_TYPES = {
    USAGE_TYPE_AI_TOKEN_INPUT,
    USAGE_TYPE_AI_TOKEN_INPUT_CACHED,
    USAGE_TYPE_AI_TOKEN_OUTPUT,
}
CALL_USAGE_TYPES = {USAGE_TYPE_API_CALL}


# ---------------------------------------------------------------------------
# Pinned pricing (USD per 1,000,000 units). Deliberately simple, deliberately
# fixed -- these are the numbers the cost-math tests are pinned against.
#
# The two AI-token gotchas this models:
#   1. Cached input tokens are cheaper than fresh input tokens (they reuse
#      a prior prompt prefix, so the provider charges less to serve them).
#   2. "Reasoning" tokens are NOT a separate, additive line item -- they are
#      part of the output token count and billed at the output rate. A
#      naive implementation that adds a third "reasoning" charge on top of
#      output cost double-counts and overcharges the customer.
# ---------------------------------------------------------------------------

PRICE_PER_MILLION_API_CALL_USD = 1_000.0          # $1,000 / 1M calls => $0.001/call
PRICE_PER_MILLION_INPUT_TOKEN_USD = 3.00           # fresh input tokens
PRICE_PER_MILLION_CACHED_INPUT_TOKEN_USD = 0.30    # cached input tokens (10x cheaper)
PRICE_PER_MILLION_OUTPUT_TOKEN_USD = 15.00         # output tokens (reasoning included)

PRICING_PER_MILLION_USD: dict[str, float] = {
    USAGE_TYPE_API_CALL: PRICE_PER_MILLION_API_CALL_USD,
    USAGE_TYPE_AI_TOKEN_INPUT: PRICE_PER_MILLION_INPUT_TOKEN_USD,
    USAGE_TYPE_AI_TOKEN_INPUT_CACHED: PRICE_PER_MILLION_CACHED_INPUT_TOKEN_USD,
    USAGE_TYPE_AI_TOKEN_OUTPUT: PRICE_PER_MILLION_OUTPUT_TOKEN_USD,
}


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "sk_test_placeholder")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "whsec_test_placeholder")
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "http://localhost:8000/billing/success")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "http://localhost:8000/billing/cancel")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./usage_billing.db")
