# billing

Subscriptions and payments: Stripe Checkout, the Stripe customer portal and signed webhooks that set the account plan in Entitlements ([ADR 0031](../../docs/adr/0031-billing-with-stripe-adapter.md)).

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; the layer rules and the definition of done are in `docs/dev/service-template.md`.

## Purpose

Billing handles money, Entitlements decides what an account may do ([ADR 0028](../../docs/adr/0028-entitlements-plans-and-enforcement.md)). Billing keeps one customer per account at the payment provider, starts hosted checkouts and opens the hosted customer portal, and turns the provider's webhooks into plan statuses that it sets in Entitlements over `entitlements.plan.set` (`source=billing`). No card data ever passes through MowDeck. It scales with webhooks: replicas share the exactly-once ledger in PostgreSQL.

**Optional per installation.** `billing.enabled=false` (the default) means the service is not started at all: Compose runs it only with the profile `billing`, Helm only with `services.billing.enabled=true`. There is no switch inside the service. The web app probes `GET /api/v1/billing/status` and hides every billing element on a network error, a 404 (no route in Traefik) or a non-JSON answer. Enable Billing together with `ENTITLEMENTS_BILLING_ENABLED=true`, otherwise every account keeps the plan `unlimited` whatever it pays for.

**Providers.** `PaymentProvider` (`application/ports.py`) is the extension point: `StripeProvider` (Stripe Python SDK 16, API version `2026-09-30.endive`, `api_base` for stripe-mock), `FakeProvider` (no network, for tests and demos; it speaks Stripe's webhook format and checks the same signature). Paddle or invoices fit behind the same interface.

**Status mapping** (Stripe subscription status to the plan status in Entitlements):

| Stripe | Entitlements |
| --- | --- |
| `trialing` | `trial` until `trial_end` |
| `active` | `active` |
| `past_due`, `unpaid` | `grace` until the end of the last paid period + `MOWDECK_BILLING_GRACE_DAYS` (`unpaid`: `expired` once that has passed) |
| `canceled`, `incomplete_expired`, `paused` | `grace` until the end + grace days while that lies ahead, else `expired` |
| `incomplete` | unchanged (the first payment is pending) |

"End of the last paid period": a failed renewal has already moved `current_period_end` to the end of the unpaid period, so `past_due` and `unpaid` count from `current_period_start`; ended subscriptions count from `ended_at` (else `current_period_end`). In `grace` and `expired` only viewing and the emergency stop remain; no mower is ever stopped by a payment problem. A late event of an old subscription never restricts an account that has a newer running one.

## Interfaces

All routes are mounted under `/api/v1` (OpenAPI at `/api/v1/openapi.json`). Traefik routes `/api/v1/billing` to this service; the webhook path has its own rate limited router (`billing-webhook-ratelimit`, 120 per minute, burst 60).

| Method | Path | Who | Description |
| --- | --- | --- | --- |
| GET | `/api/v1/billing/status` | public | `enabled` (always true when reachable), `provider`, `currency`, `automatic_tax`, `plans` on sale: key, name, description and trial days from Entitlements, the provider price (cached `MOWDECK_BILLING_PRICE_CACHE_SECONDS`) |
| GET | `/api/v1/billing/subscription` | `can_manage_billing` | `{provider, has_customer, subscription}`: plan, Stripe status, plan status, period end, trial end, grace end, cancel at period end |
| POST | `/api/v1/billing/checkout` | `can_manage_billing` | `{plan}` -> `{url}` of a Stripe Checkout session; 422 plan not sold, 409 the account already has an open subscription (plan changes go through the portal) |
| POST | `/api/v1/billing/portal` | `can_manage_billing` | `{url}` of the customer portal; 404 before the first checkout |
| GET | `/api/v1/billing/invoices` | `can_manage_billing` | invoice references, newest first: number, status, amounts in cents, links to the hosted page and the PDF |
| POST | `/api/v1/billing/webhooks/stripe` | `Stripe-Signature` only | provider events, raw body; 400 invalid signature, 200 handled or repeated, 500 handling failed (Stripe retries) |
| GET | `/healthz`, `/readyz`, `/metrics` | none | Liveness, readiness (bus, database, OpenFGA), Prometheus metrics |
| GET | `/.well-known/mowdeck-module` | none | Module descriptor: no permissions, the event `billing.subscription.changed` |

`can_manage_billing` on the token's account: the owner, or a member granted `billing.manage` (declared by Identity). Admins get 403 (billing is owner only in the roles table), principals outside the account 404. Every account endpoint acts on the token's account, never on a path value.

Checkout: `mode=subscription` with the plan's price, `client_reference_id`, `metadata` and `subscription_data.metadata` carry `account_id` and `plan`; `subscription_data.trial_period_days` is the plan's trial (from Entitlements) for an account that never had a subscription; business accounts (and accounts of unknown kind, created before Billing ran) give a tax id and a billing address (`tax_id_collection`, `billing_address_collection=required`); `automatic_tax` with `MOWDECK_BILLING_AUTOMATIC_TAX`; with an existing customer `customer_update` saves name and address. Return links: `<app>/account?billing=success#plan`, `?billing=cancel#plan`, the portal returns to `<app>/account#plan`.

## Webhooks

Handled event types (configure exactly these on the Stripe webhook endpoint): `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `customer.subscription.trial_will_end`, `invoice.paid`, `invoice.payment_failed`. Others are recorded as `ignored`.

1. The `Stripe-Signature` header is verified against the raw body with `MOWDECK_BILLING_STRIPE_WEBHOOK_SECRET` (300 s tolerance) before anything is parsed.
2. The event id is recorded in `webhook_events` and claimed (one delivery at a time, a lease of 120 s). A processed event answers 200 with `duplicate: true` and changes nothing.
3. Subscription events set the plan in Entitlements and publish `billing.subscription.changed`; `checkout.session.completed` remembers the customer and fetches the subscription; invoice events store the invoice reference; `trial_will_end` only publishes. An event older than the last one applied to its subscription is `stale` (Stripe does not guarantee the order).
4. When Entitlements does not answer, the claim is released and the webhook answers 500: Stripe delivers it again.

Outcomes in the ledger: `applied`, `ignored`, `stale`, `unknown_account`, `rejected` (Entitlements refused, e.g. an unknown plan; not retried), `failed`.

## Events and subjects

| Direction | Subject | Kind | Payload | Notes |
| --- | --- | --- | --- | --- |
| request | `entitlements.plan.set` | core NATS (internal) | `PlanSetRequest{account_id, plan, status, trial_until?, grace_until?, reason, account_name?, reference?}` / `PlanSetReply{ok, plan, status, changed, error?}` | answered by Entitlements; repeating it changes nothing |
| produced | `event.billing.subscription.changed` | JetStream `EVENTS` | `EventEnvelope[SubscriptionChanged]` | every applied subscription event; Notifications may turn it into a `billing` notification later |
| consumed | `event.identity.account.created` | JetStream, durable and deliver group `billing-accounts` | `EventEnvelope[AccountCreated]` | remembers name and kind (household or business) |
| consumed | `event.identity.grants.changed` | JetStream, every instance | `EventEnvelope[GrantsChanged]` | drops cached authorization decisions |

## Settings

All settings of `mowdeck-service-kit` apply (`MOWDECK_*`, any value also from `MOWDECK_<NAME>_FILE`). OpenFGA: `MOWDECK_OPENFGA_*` of `mowdeck-authz`; token verification: `MOWDECK_AUTH_*` of `mowdeck-auth`.

| Variable | Default | Description |
| --- | --- | --- |
| `MOWDECK_SERVICE_NAME` | `billing` | Service name in logs, metrics and traces |
| `MOWDECK_DATABASE_URL` | required | PostgreSQL of the `billing` database |
| `MOWDECK_BILLING_PROVIDER` | `stripe` | `stripe` or `fake` (nothing charged, Stripe shaped webhooks) |
| `MOWDECK_BILLING_STRIPE_SECRET_KEY` | none | Stripe secret key (`sk_test_...`, or a restricted key); required for `stripe`; usually `_FILE` |
| `MOWDECK_BILLING_STRIPE_WEBHOOK_SECRET` | none | Signing secret of the webhook endpoint (`whsec_...`); without it every webhook is 400 |
| `MOWDECK_BILLING_STRIPE_API_BASE` | Stripe | Other API base, e.g. `http://stripe-mock:12111` |
| `MOWDECK_BILLING_AUTOMATIC_TAX` | `false` | Stripe Tax in Checkout (`automatic_tax`) |
| `MOWDECK_BILLING_PRICES_FILE` | `/etc/mowdeck/billing/prices.yaml` | The price file (plan key to Stripe price id, currency); the image ships `infra/billing/prices.yaml` |
| `MOWDECK_BILLING_PRICE_IDS` | empty | Overrides: `home=price_123,fleet=price_456` |
| `MOWDECK_BILLING_GRACE_DAYS` | `14` | Grace days after the last paid period |
| `MOWDECK_BILLING_APP_URL` | request origin | Public web app URL for the return links (set it in production) |
| `MOWDECK_BILLING_ENTITLEMENTS_URL` | `http://entitlements:8000` | Entitlements for the public plan list (names, trial days); empty: none |
| `MOWDECK_BILLING_ENTITLEMENTS_TIMEOUT_SECONDS` | `5` | Timeout of `entitlements.plan.set` |
| `MOWDECK_BILLING_PRICE_CACHE_SECONDS` | `600` | Reuse of fetched prices |

Compose passes them as `BILLING_*` from `infra/compose/.env.example`; Helm as `config.BILLING_*` and `secrets.BILLING_STRIPE_*`.

## Data model

`accounts` (account id, name, kind), `customers` (account, provider, provider customer id; unique per account and provider and per provider id), `subscriptions` (account, plan key, provider subscription id, status, period end, trial end, cancel at period end, plan status set, grace end, last event time), `invoices` (provider invoice id, amounts, currency, status, hosted and PDF links), `webhook_events` (provider and event id as primary key, received, attempts, claimed, processed, outcome, account). Migrations: `0001` (empty), `0002` (the schema); a drift test compares them with the metadata.

## Layers

| Package | Content | Rule |
| --- | --- | --- |
| `domain/` | models, the price file, the status rules | pure Python, no I/O |
| `application/` | `BillingService` (status, checkout, portal, summary, invoices, webhooks, cancel), ports (`PaymentProvider`, store, Entitlements gateway, plan catalogue, publisher) | depends on `domain` and ports only |
| `adapters/api/` | FastAPI routers, schemas, the `can_manage_billing` gate | translate HTTP to use cases |
| `adapters/bus/` | `entitlements.plan.set` gateway, `identity.account.created`, the event publisher | translate messages |
| `adapters/db/` | SQLAlchemy Core store, Alembic migrations; `adapters/memory.py` for tests | implement the store port |
| `adapters/providers/` | `StripeProvider`, `FakeProvider`, the Stripe object mapping, signed fixtures | implement `PaymentProvider` |
| `adapters/plans.py` | the plan catalogue over Entitlements' public `GET /plans` | implement `PlanCatalog` |
| `main.py`, `cli.py` | wiring (`build_app`), `python -m billing.cli send-fixture` | |

## Local start

```bash
# in infra/compose/.env: COMPOSE_PROFILES=billing, ENTITLEMENTS_BILLING_ENABLED=true and the BILLING_* keys
docker compose -f infra/compose/docker-compose.yml --profile billing up -d --build --wait
curl http://localhost/api/v1/billing/status
```

Without a Stripe account: `BILLING_PROVIDER=fake` and any `BILLING_STRIPE_WEBHOOK_SECRET`, then send a signed fixture webhook from inside the container:

```bash
docker compose -f infra/compose/docker-compose.yml --profile billing exec billing \
  python -m billing.cli send-fixture --account-id <account uuid> --status past_due
```

Against Stripe test mode: [Billing operations](../../docs/ops/billing.md) (Stripe CLI `stripe listen --forward-to localhost/api/v1/billing/webhooks/stripe`). Container image (build context is the repository root): `docker build -f services/billing/Dockerfile -t mowdeck/billing:dev .`

## Tests

`uv run pytest services/billing` runs the domain, API, webhook and store tests with the memory store, the fake provider, the in-memory OpenFGA fake, an in-memory bus with a fake Entitlements and a manual clock. Integration tests run when their URL is set: PostgreSQL (`MOWDECK_TEST_DATABASE_URL`: migrations up and down, drift, the store contract, the webhook path on PostgreSQL) and stripe-mock (`MOWDECK_TEST_STRIPE_MOCK_URL`, e.g. `docker run -d -p 12111:12111 stripe/stripe-mock:v0.206.0`: customer, Checkout, portal, subscription, price, and checkout through the API). stripe-mock validates the requests against Stripe's OpenAPI document but keeps no state and sends no webhooks, so the webhook path is covered by signed fixtures (`adapters/providers/fixtures.py`).
