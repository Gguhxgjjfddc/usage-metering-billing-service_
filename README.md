# Usage Metering & Billing Service

Answers, for a multi-tenant SaaS: **how much has this customer used, what
does it cost, and have they hit their limit?** Meters usage idempotently,
enforces plan quotas with honest status codes, computes AI-token cost
correctly (cached input + reasoning gotchas), and syncs plan/status from
Stripe (test mode) via signature-verified, idempotent webhooks.

## Architecture

```
billable action ──► MeterService.record(tenant, type, qty, idempotencyKey)
                       │ dedupe (tenant_id, idempotency_key) UNIQUE → usage_events
                       ▼
                     check quota(plan) ── over? ─► 402 (free) / 429 (paid)

GET /usage ◄── rollup(usage_events, current month) → {used, limit, cost}

Stripe Checkout ─► subscription
Stripe ──signed webhook──► /webhooks/stripe ─► verify + dedupe(event.id) ─► update plan/status
```

**Data model** (`app/models.py`): `Tenant` → `Subscription` (Stripe mirror) +
`UsageEvent` (billable actions, unique per `(tenant_id, idempotency_key)`) +
`ProcessedWebhookEvent` (Stripe event-id dedupe). Every table is scoped by
`tenant_id`, so tenants are data-isolated.

**Plans** (`app/config.py`, pinned constants):
| Plan | API calls / mo | AI tokens / mo |
|------|---------------:|---------------:|
| Free | 1,000          | 100,000        |
| Pro  | 50,000         | 5,000,000      |

**Pricing** (per 1M units, pinned in `app/config.py`):
| Usage type            | Price / 1M |
|------------------------|-----------:|
| API call               | $1,000.00  |
| AI input token (fresh) | $3.00      |
| AI input token (cached)| $0.30      |
| AI output token (incl. reasoning) | $15.00 |

**The two AI-token gotchas**, handled in `app/cost.py`:
1. **Cached input is cheaper.** Cached and fresh input tokens are separate
   `usage_type`s billed at separate (pinned) rates.
2. **Reasoning tokens are not a separate line item.** There is no
   `reasoning` usage type — reasoning tokens are recorded as part of
   `ai_token_output` and billed once, at the output rate. A naive
   implementation that adds a third "reasoning" charge on top of output
   cost double-counts and overcharges the customer; this service
   structurally can't do that because the type doesn't exist.

## Setup

```bash
python -m venv venv && source venv/bin/activate   # or venv\Scripts\activate on Windows
pip install -r requirements.txt

cp .env.example .env
# fill in STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, STRIPE_PRO_PRICE_ID

uvicorn app.main:app --reload
```

API docs: http://localhost:8000/docs

### Stripe test-mode setup

1. Create a test-mode **recurring Price** in the Stripe Dashboard (Product
   catalog → add a monthly price) → copy its `price_...` id into
   `STRIPE_PRO_PRICE_ID`.
2. Install the [Stripe CLI](https://stripe.com/docs/stripe-cli) and run:
   ```bash
   stripe listen --forward-to localhost:8000/webhooks/stripe
   ```
   Copy the `whsec_...` it prints into `STRIPE_WEBHOOK_SECRET`.
3. Trigger events locally without touching real money:
   ```bash
   stripe trigger checkout.session.completed
   stripe trigger customer.subscription.updated
   stripe trigger customer.subscription.deleted
   ```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/tenants` | create a tenant |
| GET  | `/tenants/{id}` | fetch a tenant |
| POST | `/usage/record` | record one billable usage event (idempotent) |
| GET  | `/usage?tenant_id=...` | current-month rollup: `{used, limit, cost}` per bucket |
| POST | `/billing/checkout` | start a Stripe Checkout subscription session |
| POST | `/webhooks/stripe` | Stripe webhook receiver (signature-verified) |

### Example: metering a dummy billable action

```bash
curl -X POST localhost:8000/tenants \
  -H "Content-Type: application/json" \
  -d '{"name":"Acme Inc","email":"acme@example.com"}'
# => {"id": "<tenant_id>", ...}

curl -X POST localhost:8000/usage/record \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"<tenant_id>","usage_type":"api_call","quantity":1,"idempotency_key":"req-001"}'

# Retry the exact same request -- does NOT double-count:
curl -X POST localhost:8000/usage/record \
  -H "Content-Type: application/json" \
  -d '{"tenant_id":"<tenant_id>","usage_type":"api_call","quantity":1,"idempotency_key":"req-001"}'
# => {"deduped": true, ...} and /usage still shows 1 call used.

curl localhost:8000/usage?tenant_id=<tenant_id>
```

## Tests

```bash
pytest -v
```

Covers the required Definition-of-Done proofs:
- `tests/test_metering_idempotency.py` — a retried request never double-counts (including a 10x retry storm and a per-tenant isolation check).
- `tests/test_quota_enforcement.py` — usage exactly at the limit succeeds, one unit over is refused; free plan → `402`, paid plan → `429`; a retry after quota exhaustion still dedupes instead of re-raising.
- `tests/test_cost_math.py` — pinned per-million prices; cached input strictly cheaper than fresh input; reasoning tokens billed once (no separate line item); a full rollup's `used`/`limit`/`cost` add up exactly; a deduped retry doesn't inflate cost.
- `tests/test_stripe_webhooks.py` — a forged signature is rejected; a missing signature is rejected; a validly-signed `checkout.session.completed` event updates the tenant's plan; the **same** event delivered twice is processed once and ignored the second time; `customer.subscription.deleted` downgrades the tenant to Free.

## Demo script

1. Call `/usage/record` in a loop until the tenant hits its Free-plan quota → see the clean `402` refusal at the boundary.
2. Replay the last call with the *same* `idempotency_key` → `/usage` shows usage did not double-count.
3. Run `stripe trigger checkout.session.completed` (with `client_reference_id`/`metadata.tenant_id` set to a real tenant) → the webhook flips the tenant to Pro.
4. POST a forged payload to `/webhooks/stripe` with a garbage `Stripe-Signature` header → `400 Bad Request`.
5. Finish on `GET /usage` showing `used`/`limit`/`cost` adding up exactly, and `pytest -v` green.

## Known simplifications (see Stretch in the brief)

- No overage/metered billing beyond the hard quota block, no invoices, no
  real-time 80%/100% alerts, no mid-cycle proration, no nightly
  reconciliation job against Stripe's view. These are called out as
  stretch goals in the assignment and are natural next steps once the
  core is solid.
