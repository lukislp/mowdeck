# billing

Subscriptions and payments: Stripe Checkout, the Stripe customer portal and signed webhooks that set the account plan in Entitlements ([ADR 0031](../../docs/adr/0031-billing-with-stripe-adapter.md)).

Licence: AGPL-3.0-only (see the repository `LICENSE`). Generated from `templates/service`; the layer rules and the definition of done are in `docs/dev/service-template.md`.

## Purpose

Billing handles money, Entitlements decides what an account may do ([ADR 0028](../../docs/adr/0028-entitlements-plans-and-enforcement.md)). Billing keeps one customer per account at the payment provider, starts hosted checkouts and opens the hosted customer portal, and turns the provider's webhooks into plan statuses that it sets in Entitlements over `entitlements.plan.set` (`source=billing`). No card data ever passes through MowDeck. It scales with webhooks: replicas share the exactly-once ledger in PostgreSQL.

**Optional per installation.** `billing.enabled=false` (the default) means the service is not started at all: Compose runs it only with the profile `billing`, Helm only with `services.billing.enabled=true`. There is no switch inside the service. The web app probes `GET /api/v1/billing/status` and hides every billing element on a network error, a 404 (no route in Traefik) or a non-JSON answer. Enable Billing together with `ENTITLEMENTS_BILLING_ENABLED=true`, otherwise every account keeps the plan `unlimited` whatever it pays for. The deployment that enables Billing also tells Identity that Billing must confirm an account erasure ([Account erasure](#account-erasure-adr-0082)); the container refuses to start where it did not.

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
| GET | `/api/v1/billing/operator/manual-refunds` | `can_manage_accounts` on `platform:main` | refunds of erased accounts to pay back by hand, oldest first: id, payment, invoice, amount (smallest unit), currency, reason, created, settled; open only, `?settled=true` adds the settled ones (ADR 0082) |
| POST | `/api/v1/billing/operator/manual-refunds/{id}/settle` | `can_manage_accounts` on `platform:main` | mark one as paid back (settling again keeps the first time); 404 unknown id |
| GET | `/healthz`, `/readyz`, `/metrics` | none | Liveness, readiness (bus, database, OpenFGA), Prometheus metrics (with `mowdeck_billing_manual_refunds_open`: open manual refunds, alert on `> 0`) |
| GET | `/.well-known/mowdeck-module` | none | Module descriptor: no permissions, the event `billing.subscription.changed` |

The operator routes use the gate of the web app's operator area: `can_manage_accounts` on `platform:main` (operator admins, `python -m identity.cli grant-platform-admin`); everybody else gets 403.

`can_manage_billing` on the token's account: the owner, or a member granted `billing.manage` (declared by Identity). Admins get 403 (billing is owner only in the roles table), principals outside the account 404. Every account endpoint acts on the token's account, never on a path value.

Checkout: `mode=subscription` with the plan's price, `client_reference_id`, `metadata` and `subscription_data.metadata` carry `account_id` and `plan`; `subscription_data.trial_period_days` is the plan's trial (from Entitlements) for an account that never had a subscription; business accounts (and accounts of unknown kind, created before Billing ran) give a tax id and a billing address (`tax_id_collection`, `billing_address_collection=required`); `automatic_tax` with `MOWDECK_BILLING_AUTOMATIC_TAX`; with an existing customer `customer_update` saves name and address. Return links: `<app>/account?billing=success#plan`, `?billing=cancel#plan`, the portal returns to `<app>/account#plan`.

## Webhooks

Handled event types (configure exactly these on the Stripe webhook endpoint): `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `customer.subscription.trial_will_end`, `invoice.paid`, `invoice.payment_failed`, `charge.refunded`, `refund.updated`, `refund.failed`. The refund events store nothing: they are `unknown_account` for an erased account (and a warning when a refund of an erasure failed), `ignored` otherwise. Others are recorded as `ignored`.

1. The `Stripe-Signature` header is verified against the raw body with `MOWDECK_BILLING_STRIPE_WEBHOOK_SECRET` (300 s tolerance) before anything is parsed.
2. The event id is recorded in `webhook_events` and claimed (one delivery at a time, a lease of 120 s). A processed event answers 200 with `duplicate: true` and changes nothing.
3. **Whose event it is** is decided by the customer row, never by the event alone: Billing creates the provider's customer of an account before the first checkout, so an event whose customer it does not know is nobody's and is dropped (`unknown_account`), also when its metadata names an account. The event is applied under a lock on that customer row.
4. Subscription events set the plan in Entitlements and publish `billing.subscription.changed`; `checkout.session.completed` fetches the subscription and applies it; invoice events store the invoice reference; `trial_will_end` only publishes. An event older than the last one applied to its subscription is `stale` (Stripe does not guarantee the order).
5. When Entitlements does not answer, the claim is released and the webhook answers 500: Stripe delivers it again.

Outcomes in the ledger: `applied`, `ignored`, `stale`, `unknown_account`, `rejected` (Entitlements refused, e.g. an unknown plan; not retried), `failed`.

## Account erasure (ADR 0082)

Identity asks on `account.erase.billing` when a deleted account is purged, **before** it asks Entitlements ([ADR 0082](../../docs/adr/0082-account-deletion-inside-the-app.md), section 5). The use case is the kit's `AccountErasure` over `application/erasure.py`:

1. **At the provider,** through `PaymentProvider` only, for every subscription of the account that is still open: the **refund** of the unused time paid on each of its paid invoices that bill time containing now: the period's invoice and an upgrade billed on its own (`paid_invoices_covering`, `refunds_of`, `refund`; the rule is `domain/refunds.py`), then `cancel_subscription` (now, no final invoice, no proration, so no credit is added); after all subscriptions, per customer, its **credit balance** is paid back (`customer_credit_balance`, `recent_payments`, `refunds_of`, `refund`; the rule is `domain/credit.py`) and zeroed (`clear_credit_balance`), then `delete_customer`. All are idempotent (a refund is found again by its metadata and made under an idempotency key; already ended, already deleted, unknown to the provider: done). Stripe: `POST /v1/refunds` on the invoice's PaymentIntent with `Idempotency-Key: mowdeck-erasure-refund-<account>-<subscription>-<invoice>` and the metadata `reason=account_erasure`, `DELETE /v1/subscriptions/{id}` with `invoice_now=false&prorate=false`, the credit refunds with `Idempotency-Key: mowdeck-erasure-credit-<account>-<payment>` and the metadata `reason=account_erasure_credit_balance`, `POST /v1/customers/{id}/balance_transactions` (a debit of the credit, key `mowdeck-erasure-credit-clear-<account>-<customer>-<amount><currency>`), `DELETE /v1/customers/{id}`. The invoices, payments and refunds Stripe must keep stay at Stripe.
2. **The rows,** afterwards and in one transaction (`Tx.purge_account`), the customer rows first:

   | Table | Of an erased account |
   | --- | --- |
   | `accounts` | deleted (name and kind) |
   | `customers` | deleted, after the provider confirmed |
   | `subscriptions` | deleted |
   | `invoices` | deleted: Billing keeps no invoice copies |
   | `webhook_events` | rows with the account's id deleted. Rows without an account (unknown customers, ignored types) stay: an event id, a type, times and an outcome, no payload |

3. What is left is counted over every table with an `account_id` column (`Tx.account_rows`); a row that remains fails the erasure.

**The refund**, per invoice = (paid on it for time after now - refunded already) x unused part, at the moment of the purge, rounded down to the currency's smallest unit; what was paid is shared among the invoice's positive lines, and the recurring and proration lines whose period contains now count, each over its own period. Metered usage is not refunded (it bills consumption that has already happened), nor are one-off items; 0 for a trial, a 100 % coupon or a period that is over. `pending` counts as made (bank refunds stay pending for days). A refund that is `failed` or `canceled` fails the erasure before its subscription ends (`RefundFailed`); a refund Stripe refuses for good (`RefundImpossible`) or that failed 3 times (`MAX_FAILED_REFUNDS`) is skipped and recorded in `manual_refunds` (payment, invoice, amount, reason; no account) before the customer is deleted (in a transaction of their own; the row deletion inserts them again, a no-op by key, and a retry keeps a recorded skip instead of refunding it), with the warning `refund of an erased account skipped: recorded to pay it out by hand`. A refund of an erasure that fails after the erasure (`refund.failed` / `refund.updated`) is recorded too, once per payment and invoice. **The credit balance** (Stripe's negative `balance`, from a downgrade with proration for example) is read after the subscriptions ended, plus what an earlier attempt of the erasure cleared already, and paid back as refunds over the customer's payments of the last 366 days, newest first, each capped by what is still refundable of it (refunds already made, this erasure's period refunds included); the part no payment can take, and all of a credit in another currency than the payments, is recorded in `manual_refunds` (reason `credit balance: ...`, against the newest payment or the customer). Then the balance is zeroed (skipped when it is 0 already) and the customer deleted. A zero balance or one the customer owes: nothing; an erased customer is never charged. Failures, retries, skips and records follow the period refunds. Operators list and settle the records over `/operator/manual-refunds` ([Billing operations](../../docs/ops/billing.md#paying-back-manual-refunds)); settled ones are deleted after 30 days by the service's sweep (every 5 minutes), which also sets the gauge `mowdeck_billing_manual_refunds_open`.

**A provider error fails the erasure:** `ok=false`, no row deleted (the rows are what names the customer for the next attempt), and Identity asks again with its backoff. `ProviderUnavailable` (a rate limit, a timeout, a 5xx) can succeed later by itself, `ProviderError` needs the operator (a key without the permission, a customer at a provider that is no longer configured). Rows of the fake provider are deleted without a call (`LOCAL_PROVIDERS`). Billing keeps nothing of an account in memory.

**Events after the erasure** (`customer.subscription.deleted`, `customer.deleted`, an invoice, a repeated delivery) name a customer Billing no longer knows: acknowledged with 200, outcome `unknown_account` or `ignored`, nothing written, no plan set. An event that is being applied while the erasure runs holds the customer row (`Tx.lock_customer`, `SELECT ... FOR UPDATE`, held while Entitlements is asked); the erasure deletes the customer rows first and so waits for it.

**Optional and not forgotten.** Identity asks Billing only where the deployment names it (`MOWDECK_IDENTITY_OPTIONAL_SERVICES`: Compose passes `COMPOSE_PROFILES`, the chart `billing` when enabled). Billing gets the same value as `MOWDECK_BILLING_OPTIONAL_SERVICES` and `ensure_identity_asks` (`main.py`) refuses to start without `billing` in it: otherwise an erased account would keep its customer and a running subscription at the provider. `docker compose --profile billing` alone does not set `COMPOSE_PROFILES`; set it in `.env`.

## Events and subjects

| Direction | Subject | Kind | Payload | Notes |
| --- | --- | --- | --- | --- |
| served | `account.erase.billing` | core NATS (internal), queue `billing-erasure` | `AccountEraseRequest{account_id, requested_at, reason}` / `AccountEraseReply{ok, deleted, error?}` | from Identity when a deleted account is purged; `ok=false` while the provider did not confirm or rows remain |
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
| `MOWDECK_BILLING_OPTIONAL_SERVICES` | empty | The optional parts the installation runs, as Identity gets them (Compose: `COMPOSE_PROFILES`; Helm: `billing`). `python -m billing.main` refuses to start unless `billing` is among them |

Compose passes them as `BILLING_*` from `infra/compose/.env.example`; Helm as `config.BILLING_*` and `secrets.BILLING_STRIPE_*`.

## Data model

`accounts` (account id, name, kind), `customers` (account, provider, provider customer id; unique per account and provider and per provider id), `subscriptions` (account, plan key, provider subscription id, status, period end, trial end, cancel at period end, plan status set, grace end, last event time), `invoices` (provider invoice id, amounts, currency, status, hosted and PDF links), `webhook_events` (provider and event id as primary key, received, attempts, claimed, processed, outcome, account). Every table has an `account_id` column; `webhook_events` stores no payload. `manual_refunds` (id, unique key per payment and invoice, payment, invoice, amount, currency, reason, created, settled) is the one table without an `account_id`: it outlives the erasure until the repayment is made (Art. 17(3)(b) GDPR) and names nobody. Migrations: `0001` (empty), `0002` (the schema), `0003` (`manual_refunds`); a drift test compares them with the metadata.

## Layers

| Package | Content | Rule |
| --- | --- | --- |
| `domain/` | models, the price file, the status rules, the erasure rule (`erasure.py`: which providers keep nothing outside), the refund rule of an erasure (`refunds.py`: amount, retries, when to give up), the credit balance rule (`credit.py`: spread over the recent payments, leftover, zeroing) | pure Python, no I/O |
| `application/` | `BillingService` (status, checkout, portal, summary, invoices, webhooks), `erasure.py` (`AccountPurge` and the kit's `AccountErasure`), `manual_refunds.py` (record, list, settle, sweep of the refunds paid by hand), ports (`PaymentProvider`, store, Entitlements gateway, plan catalogue, publisher) | depends on `domain` and ports only |
| `adapters/api/` | FastAPI routers, schemas, the `can_manage_billing` gate and the operator gate | translate HTTP to use cases |
| `adapters/bus/` | `entitlements.plan.set` gateway, `identity.account.created`, `account.erase.billing` (`erasure.py`), the event publisher | translate messages |
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

Without a Stripe account: `BILLING_PROVIDER=fake` and any `BILLING_STRIPE_WEBHOOK_SECRET`. Start a checkout for the account once ("Upgrade" on the account page; it creates the customer the events belong to), then send a signed fixture webhook from inside the container:

```bash
docker compose -f infra/compose/docker-compose.yml --profile billing exec billing \
  python -m billing.cli send-fixture --account-id <account uuid> --status past_due
```

Against Stripe test mode: [Billing operations](../../docs/ops/billing.md) (Stripe CLI `stripe listen --forward-to localhost/api/v1/billing/webhooks/stripe`). Container image (build context is the repository root): `docker build -f services/billing/Dockerfile -t mowdeck/billing:dev .`

## Tests

`uv run pytest services/billing` runs the domain, API, webhook, erasure and store tests with the memory store, the fake provider, the in-memory OpenFGA fake, an in-memory bus with a fake Entitlements and a manual clock. Integration tests run when their URL is set: PostgreSQL (`MOWDECK_TEST_DATABASE_URL`: migrations up and down, drift, the store contract, the webhook path and the erasure on PostgreSQL, including the row lock between an event and an erasure) and stripe-mock (`MOWDECK_TEST_STRIPE_MOCK_URL`, e.g. `docker run -d -p 12111:12111 stripe/stripe-mock:v0.206.0`: customer, Checkout, portal, subscription, the cancellation and the customer deletion of the erasure, price, the invoice, payment and refund calls of the erasure refund, and checkout through the API). stripe-mock validates the requests against Stripe's OpenAPI document but keeps no state and sends no webhooks, so the webhook path is covered by signed fixtures (`adapters/providers/fixtures.py`).
