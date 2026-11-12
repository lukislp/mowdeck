# ADR 0083: Payments ledger and operator dashboard

## Status

Accepted (2026-11-12)

## Context

The person who runs an installation that sells subscriptions ([ADR 0031](0031-billing-with-stripe-adapter.md)) has no overview of the money inside MowDeck: revenue, refunds (among them the refunds of erased accounts, [ADR 0082](0082-account-deletion-inside-the-app.md)), failed payments, disputes and account deletions are visible only in the Stripe dashboard, if at all. The operator console has Accounts, Plans and System; it needs a Payments tab, for operator admins only (`can_manage_accounts` on `platform:main`), and only where Billing runs.

Billing keeps rows per account (customers, subscriptions, invoice references, the event ledger), and the erasure of an account deletes all of them. Statistics computed from those rows would lose every erased account's history: revenue of past months would shrink after each erasure. Asking Stripe live for every dashboard view would make the dashboard depend on Stripe's availability and rate limits and send every operator view to a third party.

Requirements from the maintainer (2026-11-12): Billing's own data recorded from the webhooks it already receives; no account id, customer id, name or e-mail in what is recorded; idempotent ingestion although Stripe retries and reorders; statistics of erased accounts stay as anonymous facts; no backfill.

## Decision

**A payments ledger in Billing.** The table `payment_facts` holds anonymous facts written from webhooks, in the transaction that applies the event (under the customer lock of ADR 0082 where the event has a customer):

| Fact | From | Holds |
| --- | --- | --- |
| `invoice_paid` | `invoice.paid` of a known customer, amount above 0 | amount paid, tax (`total_taxes`, null without Stripe Tax), currency, plan, billing period, paid at |
| `payment_failed` | `invoice.payment_failed` of a known customer | amount due, currency, plan, attempt number (one fact per attempt) |
| `refund` | `refund.created`, `refund.updated`, `refund.failed` | amount, currency, status, reason `erasure` (the metadata of ADR 0082) or `other`, created |
| `refund` | settling a manual refund (ADR 0082, section 5) | the amount paid back by hand as a succeeded `erasure` refund |
| `dispute` | `charge.dispute.created`, `.updated`, `.closed` | amount, currency, status, reason code, evidence due date, opened, closed |
| `subscription_started` | `customer.subscription.*` once the status is trialing, active, past_due, unpaid or paused | plan, start date |
| `subscription_ended` | `customer.subscription.*` with status `canceled` | plan, end date |

A refund and a dispute name no customer and most erasure refunds arrive after the customer row is gone, so every refund and dispute event is recorded. A cancellation of a customer Billing no longer knows (the erasure ended the subscription before Stripe's event came) still records `subscription_ended` when the subscription carries MowDeck's checkout metadata; nothing else of such an event is written. This assumes that the Stripe account serves this installation; refunds and disputes of other products on the same account would be counted.

**What a fact does not hold.** No account id, no customer id, no name, no e-mail, no provider object id. A test pins the columns.

**Deduplication without the provider's id.** A fact has a `ref`: the hex HMAC-SHA256 of `provider:kind:object id` (the invoice, refund, dispute or subscription id; invoice id and attempt for a failure; the record id for a manual refund) with the installation's ledger key (`MOWDECK_BILLING_LEDGER_KEY`). The unique key `(kind, ref)` makes a repeated delivery, or the same object under a new event id, land on the same row. A later event updates status, amount, reason, closing time and due date only when it is not older than the event that last changed the fact (`event_at`); an out-of-order older event and the same event twice change nothing. Every other field is written once.

**Why a keyed hash and not the raw id, and not a cleanup in the erasure.** Stripe ids are pseudonymous personal data: with access to the Stripe account they lead to a person. The two options were (a) store the raw id and clear it in the erasure transaction, or (b) never store it. (a) needs a link from the account to its ledger rows, which is exactly what the ledger must not have (refunds and disputes carry no customer), and it would put the ledger into the erasure path of ADR 0082. (b) with a plain hash is reversible by hashing every id in the Stripe account. So (b) with a keyed hash: without the key nobody can test an id against the ledger, and the key never leaves the service. With the key the operator could still test a known Stripe id, so for the controller the ref is pseudonymous; therefore it is cleared after its job is done: **180 days after a fact last changed** (`REF_HORIZON`; a refund settles within days, a dispute closes within about three months, Stripe retries a webhook for three days), the periodic sweep sets `ref` to null. From then on a fact is a plain statistic (an amount, a currency, a plan, a month) that nothing links to a payment. The erasure does not touch the ledger and needs no change.

**The ledger key is required** (decision of 2026-11-12, after review). `MOWDECK_BILLING_LEDGER_KEY` (or `_FILE`) must hold at least 32 characters; Billing refuses to start without it, like without the Stripe secret key, and says how to set it. An earlier draft derived the key from the webhook signing secret when none was set; that tied the ledger to a secret that is rotated for other reasons (a rotation would count every open refund and dispute twice) and is gone. Compose: `.devcontainer/generate-env.sh` writes one (`openssl rand -base64 32`) like the other keys, `infra/compose/.env.example` documents it; Helm: `secrets.BILLING_LEDGER_KEY` is `required` when Billing is enabled, or the key `billing-ledger-key` of an `existingSecret`.

**Statistics of erased accounts stay** as these anonymous facts. Bookkeeping records (invoices, payments, refunds) stay with Stripe, as ADR 0082 already says; the ledger is no bookkeeping and no source for it.

**No backfill.** The statistics start with the deploy that adds the ledger (migration `0004`). The summary says since when (`since`: the oldest fact).

**Aggregation in Billing, one summary endpoint.** `GET /api/v1/billing/operator/payments?months=12` (1 to 36) answers every section at once. One request and one transaction give all sections the same moment, one authorization check guards it, and the sections are small aggregates (a few dozen rows), so per-section endpoints would add round trips and gates without saving work. The rules are pure functions in `billing.domain.statistics`:

- **Months** are local calendar months in the zone `MOWDECK_BILLING_REPORT_TIMEZONE` (an IANA name, default `Europe/Berlin`, validated at start; decision of 2026-11-12), computed with `zoneinfo` so that daylight saving time never moves an instant into another month (00:30 on the 1st in Berlin belongs to the new month, in summer and in winter); a window of `n` months ends with the current one. The answer names the zone (`time_zone`). A paid invoice counts in the month it was paid, a refund in the month it was created, a dispute in the month it was opened.
- **Money** is grouped by currency and never added across currencies; amounts are in the provider's smallest unit, and the answer lists each currency's exponent (Stripe's rules: 2, 0 for zero-decimal currencies, 3 for BHD/JOD/KWD/OMR/TND, 2 for ISK and UGX) so the web app formats with the same rules.
- **Net revenue** of a month is gross (paid invoices, tax included) minus the refunds created in that month that take money back (`pending`, `requires_action`, `succeeded`). Disputes are shown on their own and not deducted.
- **MRR** is the list price of every `active` or `past_due` subscription normalised to a month: a yearly price ÷ 12, a price every `n` months ÷ `n`, weekly × 52/12, daily × 365/12; exact fractions per currency, rounded half up once. Trials count nothing; discounts and taxes are not in it. Subscriptions whose plan has no known price are reported as `unpriced`.
- **Subscription counts** per plan and status (trialing, active, past_due) come from Billing's live subscription rows, not from the ledger. **Open and overdue invoices** come from the live invoice rows: MowDeck's subscriptions charge automatically, so an invoice is due when issued and an `open` one is overdue.

`GET /api/v1/billing/operator/prices` lists every price of the price file as Stripe has it (the cache of `GET /status`), also for plans Entitlements does not offer; the Plans tab shows the price per plan and warns about an offered plan without a price, a price Stripe did not answer for, and a price whose plan is unknown. Prices stay maintained in Stripe.

**Account deletions from Identity.** `GET /api/v1/identity/platform/deletions?months=12&tz=Europe/Berlin` counts per local month of `tz` the deletions scheduled (audit action `account.deletion_requested`; audit rows stay, scrubbed, after an erasure), restored (`account.restored`) and completed (`account_erasures.completed_at`), plus how many accounts are in the grace period and being erased now. Counts only. The zone is a validated query parameter (422 for an unknown one, `Europe/Berlin` without it), and the web app passes the `time_zone` of Billing's summary: one setting decides the months of the whole dashboard, Identity gets no setting of its own that could disagree, and Identity stays a calculator for whatever zone it is asked about. A setting of its own was the other option; it would need the same value configured twice in Compose and Helm.

**Web.** The Payments tab of the operator console appears only where the Billing probe answers. Charts reuse the SVG bar strip of the health and reports pages (no chart library), each with a text summary and a table behind it. The manual refunds of ADR 0082 are listed there; "mark as paid back" asks for confirmation first and writes the ledger fact.

## Consequences

- Revenue, refunds, failures, disputes and deletions are visible in the console without Stripe access, and survive account erasures as anonymous numbers.
- The ledger grows by a few rows per paying account and month; it is never pruned. Should it ever grow large, the facts older than the longest window (36 months) can be summarised or deleted without losing anything the dashboard shows.
- Changing `MOWDECK_BILLING_LEDGER_KEY` changes the refs: a refund or dispute still open at that moment is recorded a second time when it next changes. Keep the key (back it up with the other secrets).
- Installations that already run Billing must set `BILLING_LEDGER_KEY` before upgrading, or Billing does not start (the upgrade notes in [Billing operations](../ops/billing.md) and [Helm](../ops/helm.md)). No production installation runs Billing yet, so there is no migration beyond that.
- The Stripe webhook endpoint must send `refund.created` and the three `charge.dispute.*` events in addition (the event list in [Billing operations](../ops/billing.md)).
- Month buckets follow the configured zone; changing it later moves facts near midnight on the first of a month into another month in every view (nothing stored changes).
- The figures are statistics, not accounting: taxes, fees and payouts are in Stripe's reports.
