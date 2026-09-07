# Payment Auth API Contract (Subtask 1.2.1)

Zero-Deposit Card Auth & ACH Mandate Capture — backend API contract for
the frontend Stripe Elements integration. This is the frontend-facing
reference; implementation lives in `src/api/payment_auth_router.py` and
`src/api/stripe_webhook_router.py`, whose request/response Pydantic models
also render automatically at `/docs` (FastAPI's own OpenAPI UI).

**Status: backend only.** The actual embedded Stripe Elements modal is a
separate frontend task and is NOT considered complete by this backend
work. `src/api/payment_auth_router.py`'s `/test-harness` page exists only
for manual Stripe test-mode verification during backend development — it
is explicitly labeled non-production, available only in local/dev/test
environments, and returns 404 whenever `ENVIRONMENT=production`; it is not
the production frontend.

## Authentication

No client-portal login/session system exists anywhere in this repo yet.
Every endpoint below authenticates via a signed, expiring
**onboarding_token** (`src/services/payment_auth_token.py`, HS256 JWT,
default 30-minute TTL) instead of trusting a bare `client_id`/`company_id`/
`offer_code` in the request. `client_id`, `company_id`, and `offer_code`
are read **exclusively** from the token's signed claims — never from a
request body/query field — so a request can never operate on, or claim a
different offer for, a company it wasn't issued a token for.

This token is explicitly **temporary integration-testing scaffolding**,
not a permanent auth mechanism. The future authenticated onboarding portal
is expected to supply its own tenant context (a real session) once it
exists, most likely replacing this token outright rather than minting one
itself in production. `scripts/dev_mint_payment_auth_token.py` mints one
for manual/dev/test use only.

## Offer gating

Every endpoint checks `payment_auth_offer_config.zero_deposit_enabled` for
the token's `offer_code` before making any Stripe call. **Every offer
ships disabled by default** — an operator flips a specific row to `TRUE`
only for a client-confirmed offer. This flow is never a universal
zero-upfront rule; self-serve Respond/bundle signups charge at signup via
a separate Stripe Checkout flow untouched by this subtask.

## Endpoints

### 1. `POST /api/v1/onboarding/payment-auth/setup-intents`

Creates one card SetupIntent and one ACH (`us_bank_account`) SetupIntent —
always two, never one, since a single SetupIntent cannot capture both.

Request:
```json
{ "onboarding_token": "<jwt>" }
```

Response `200`:
```json
{
  "card_client_secret": "seti_..._secret_...",
  "ach_client_secret": "seti_..._secret_...",
  "publishable_key": "pk_test_..."
}
```

Frontend usage:
```js
const stripe = Stripe(publishable_key);
const elements = stripe.elements();
const cardElement = elements.create('card');
cardElement.mount('#card-element');
// ... mount an ACH/US bank account Element similarly ...
// confirm both in ONE onboarding step:
const cardResult = await stripe.confirmCardSetup(card_client_secret, { payment_method: { card: cardElement } });
const achResult = await stripe.confirmUsBankAccountSetup(ach_client_secret);
```

Errors: `401` invalid/expired token · `403` offer not enabled · `503`
Stripe not configured · `502` Stripe API error (also durably logged as a
`payment_auth_failed` event).

### 2. `POST /api/v1/onboarding/payment-auth/confirm`

Request:
```json
{
  "onboarding_token": "<jwt>",
  "card_setup_intent_id": "seti_...",
  "ach_setup_intent_id": "seti_..."
}
```

Only the two `setup_intent.id` values Stripe.js returns are sent — **never**
a PaymentMethod id or a client-asserted success flag. The server always
re-fetches both SetupIntents from Stripe and verifies `customer`/
`payment_method.type`/`status` itself before persisting anything.

Response `200`:
```json
{ "status": "completed" | "ach_pending" | "already_completed" }
```

- `completed` — both rails verified `succeeded`; the $1 authorization hold
  has been explicitly cancelled; `payment_auth_completed` event logged.
- `ach_pending` — ACH microdeposit/instant verification is still in
  progress (this is expected, not an error). Poll endpoint 3, or wait for
  the webhook (endpoint 4) to finish it server-side.
- `already_completed` — this company's payment auth was already recorded;
  no Stripe re-verification performed.

Errors: `400` SetupIntent ownership/type mismatch · `401` invalid/expired
token · `402` card declined (Stripe error code in the body; also logged as
`payment_auth_failed`) · `403` offer not enabled · `404` no Stripe
customer on file yet (call endpoint 1 first) · `409` card SetupIntent not
yet `succeeded` · `502` Stripe API error.

### 3. `GET /api/v1/onboarding/payment-auth/status?onboarding_token=<jwt>`

Response `200`:
```json
{
  "payment_auth_completed": false,
  "has_card_payment_method": true,
  "has_ach_payment_method": false,
  "offer_code": "OWNER_GROWTH"
}
```

Poll this after an `ach_pending` confirm response to learn when the
webhook has finished the ACH rail. Errors: `401` invalid/expired token ·
`404` unknown company.

### 4. `POST /api/v1/webhooks/stripe` (Stripe-called only — not the frontend)

Signature-verified via `stripe.Webhook.construct_event`; unsigned/invalid
requests are rejected `401`. Every event id is recorded in
`stripe_webhook_events` before processing (a redelivered event is a
no-op). Listens for `setup_intent.succeeded`/`setup_intent.setup_failed`;
`payment_auth_completed` is only written once **both** the card and ACH
SetupIntents are independently confirmed `succeeded`.

## Data stored

On `companies`: `stripe_customer_id` (plaintext — not a secret),
`card_payment_method_id_encrypted` / `ach_payment_method_id_encrypted` /
`ach_mandate_id_encrypted` (Fernet ciphertext via
`src/core/token_crypto.py`, never plaintext Stripe IDs),
`payment_auth_offer_code`, `payment_auth_hold_payment_intent_id` (so the
$1 hold can be explicitly cancelled), `payment_auth_completed_at`.
Payment-method capture never itself flips any billing/entitlement row —
that is a separate, later settlement-pipeline ticket.

`ach_mandate_id_encrypted` stores Stripe's own `SetupIntent.mandate` field
— documented by Stripe as "ID of the multi use Mandate generated by the
SetupIntent" — the real, reusable ACH authorization/mandate evidence, not
the ACH PaymentMethod id and not an invented attribute.
`record_payment_auth_completed()` (`src/services/payment_auth.py`) logs a
WARNING if Stripe returns no mandate id for a SetupIntent it just verified
`succeeded` — this is never silently swallowed into an unexplained NULL.

## Idempotency

Every outbound Stripe mutation (`Customer.create`, both `SetupIntent.create`
calls, the $1 `PaymentIntent.create`) carries a deterministic
`idempotency_key` derived from `(company_id, purpose)`, so a retried
request (network blip, double-click) cannot create a duplicate resource.
Inbound Stripe webhook deliveries are separately de-duplicated via the
`stripe_webhook_events` table.

## Manual Stripe test-mode verification

```bash
# 1. Set real Stripe TEST-mode keys + a token secret in .env.local:
#    STRIPE_SECRET_KEY=sk_test_...
#    STRIPE_PUBLISHABLE_KEY=pk_test_...
#    STRIPE_WEBHOOK_SECRET=whsec_...           (from `stripe listen`)
#    PAYMENT_AUTH_ONBOARDING_TOKEN_SECRET=<any random string>

# 2. Seed one payment_auth_offer_config row with zero_deposit_enabled = TRUE
#    for a test offer_code (every offer ships disabled by default).

# 3. Mint a token and open the non-production test-harness page:
PYTHONPATH=. python scripts/dev_mint_payment_auth_token.py <client_id> <company_id> <offer_code>

# 4. Forward webhooks in another terminal:
stripe listen --forward-to localhost:8000/api/v1/webhooks/stripe

# 5. Use Stripe's test card 4242 4242 4242 4242 and a test bank account.
#    Confirm in the Stripe test-mode dashboard: both SetupIntents
#    succeeded, a $1 PaymentIntent requires_capture then canceled (never
#    captured/succeeded), zero actual charges on the account.
```
