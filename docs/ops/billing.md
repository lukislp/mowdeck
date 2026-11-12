# Billing operations

Billing (Stripe Checkout, the customer portal and signed webhooks that set plans in Entitlements) is optional per installation and **off by default**: without it the Billing service is not started, every account keeps the plan `unlimited` and the web app shows nothing billing related. This page is the operator's checklist for selling plans; the design is [ADR 0031](../adr/0031-billing-with-stripe-adapter.md), the service reference is `services/billing/README.md`.

## Operator checklist

Billing needs data only the operator has. Nothing here is legal or tax advice; have the texts checked.

| Step | What | Where |
| --- | --- | --- |
| 1 | Stripe account; start in **test mode** (keys `sk_test_...`), switch to live keys only after a full test run | [dashboard.stripe.com](https://dashboard.stripe.com) |
| 2 | One product per plan (`home`, `fleet`) with a **recurring** price (e.g. monthly, in EUR); note the price ids `price_...` | Stripe: Product catalogue |
| 3 | Price ids into MowDeck: `BILLING_PRICE_IDS=home=price_...,fleet=price_...` (or `infra/billing/prices.yaml`) | `.env` / Helm `config.BILLING_PRICE_IDS` |
| 4 | Customer portal: enable it and choose what customers may do (update payment method, view invoices, switch between your plan prices, cancel at period end) | Stripe: Settings, Billing, Customer portal |
| 5 | Webhook endpoint `https://<your host>/api/v1/billing/webhooks/stripe` with the events `checkout.session.completed`, `customer.subscription.created`, `customer.subscription.updated`, `customer.subscription.deleted`, `customer.subscription.trial_will_end`, `invoice.paid`, `invoice.payment_failed`; copy its signing secret `whsec_...` | Stripe: Developers, Webhooks |
| 6 | Secrets: `BILLING_STRIPE_SECRET_KEY` (a restricted key with write access to Customers, Checkout Sessions, Customer portal, Subscriptions and read access to Prices is enough) and `BILLING_STRIPE_WEBHOOK_SECRET`; in production as files (`MOWDECK_BILLING_STRIPE_SECRET_KEY_FILE`) or Helm `secrets.BILLING_STRIPE_*` | `.env` / Helm |
| 7 | Stripe Tax for EU VAT: confirm the head office address, choose the product tax code and the tax behaviour (inclusive or exclusive), add your registrations (your country, and the **EU OSS** registration once cross-border B2C sales exceed EUR 10,000 a year); then `BILLING_AUTOMATIC_TAX=true`. Without a registration Stripe calculates 0 tax. B2B customers with a valid EU VAT id get the reverse charge | Stripe: Tax; your tax office (OSS registration at the BZSt in Germany) |
| 8 | Invoices: company name, address and VAT id on invoices (Stripe: Settings, Business details and Invoice template); business customers enter their tax id in Checkout (MowDeck asks business accounts for it) | Stripe |
| 9 | Impressum (company, address, contact, register entry, VAT id, responsible person) | `apps/web/static/legal/imprint.md` |
| 10 | AGB / terms (subscription term and renewal, prices incl. VAT, payment, cancellation, the consumer's right of withdrawal, liability, governing law) | `apps/web/static/legal/terms.md` |
| 11 | Datenschutz / privacy policy (controller, purposes and legal bases, processors: hosting and **Stripe** as payment processor, retention, rights, supervisory authority) | `apps/web/static/legal/privacy.md` |
| 12 | AVV / data processing agreements: with your hosting provider and with Stripe (Stripe's DPA is part of its services agreement); for business customers who process their staff's data in MowDeck, offer your own AVV | contracts |
| 13 | Show the legal pages: `WEB_LEGAL_ENABLED=true` links Imprint, Terms and Privacy from the login page (the shipped files are placeholders with a visible banner until you replace them) | `.env` / Helm `config.WEB_LEGAL_ENABLED` |
| 14 | Enable: `COMPOSE_PROFILES=billing` in `.env` (the variable, not only `--profile billing` on the command line: see [Deleted accounts](#deleted-accounts)) and `ENTITLEMENTS_BILLING_ENABLED=true` (Helm: `services.billing.enabled=true`, `config.ENTITLEMENTS_BILLING_ENABLED="true"`), `BILLING_APP_URL=https://<your host>`; optionally add `http://billing:8000` to Identity's module URLs (`MOWDECK_IDENTITY_MODULE_URLS`) so its descriptor is listed | `.env` / Helm |

The legal pages are served from the web image (`/legal/<page>.md`); to replace them, build the image with your files or mount them read-only over `/usr/share/nginx/html/legal/{imprint,terms,privacy}.md` of the `web` container (served with `no-cache`, never precached by the service worker).

## Enable in Compose

```bash
# infra/compose/.env
COMPOSE_PROFILES=billing
ENTITLEMENTS_BILLING_ENABLED=true
BILLING_PROVIDER=stripe
BILLING_STRIPE_SECRET_KEY=sk_test_...
BILLING_STRIPE_WEBHOOK_SECRET=whsec_...
BILLING_PRICE_IDS=home=price_...,fleet=price_...
BILLING_APP_URL=https://mowdeck.example.org

docker compose -f infra/compose/docker-compose.yml up -d --build --wait
curl https://mowdeck.example.org/api/v1/billing/status
```

`GET /api/v1/billing/status` lists the plans on sale with the prices fetched from Stripe. Without the profile the route does not exist (404) and the web app hides the billing section. `docker compose config --services` shows `billing` and `billing-migrate` only with the profile.

## Test with Stripe test mode (the owner's first run)

1. Install the [Stripe CLI](https://docs.stripe.com/stripe-cli) and `stripe login`.
2. Forward webhooks to the local stack and copy the printed secret into `BILLING_STRIPE_WEBHOOK_SECRET` (it stays the same across restarts; `stripe listen --print-secret` prints it again):

   ```bash
   stripe listen --forward-to localhost/api/v1/billing/webhooks/stripe \
     --events checkout.session.completed,customer.subscription.created,customer.subscription.updated,customer.subscription.deleted,customer.subscription.trial_will_end,invoice.paid,invoice.payment_failed
   ```

3. Start the stack with the profile, log in as an account owner, open the account page: the plans appear with their prices. "Upgrade" opens Stripe Checkout; pay with the test card `4242 4242 4242 4242` (any future date, any CVC). Back on the account page the plan shows `trial` (or `active` without a trial) with source `billing`; the operator console's account history shows `service:billing:sub_...`.
4. "Manage subscription" opens the customer portal; cancel there and watch `customer.subscription.updated` (cancel at period end) arrive.
5. Payment problems: use the test card `4000 0000 0000 0341` (attaches, then fails), or `stripe trigger invoice.payment_failed`; the subscription goes `past_due` and the account `grace` (viewing and emergency stop only). Paying again (update the card in the portal) restores `active`. `customer.subscription.trial_will_end` cannot be triggered: use a subscription with a short trial or Stripe test clocks.
6. Replays: `stripe events resend evt_...` delivers an event again; the answer says `"duplicate": true` and nothing changes.

## Deleted accounts

When an account is deleted for good ([ADR 0082](../adr/0082-account-deletion-inside-the-app.md), after the grace period of 7 days), Identity asks Billing to erase it, before it asks Entitlements:

1. Every open subscription of the account ends at Stripe **at once**, not at the period end, without a final invoice and without a proration. **Nothing is refunded**; refund in the Stripe Dashboard if your terms promise it, before the grace period ends.
2. The Stripe customer is deleted, with its name, e-mail address, addresses, tax ids and payment methods.
3. Billing deletes its rows of the account: the customer, the subscriptions, the invoice references and the ledger rows of its events. It keeps no invoice copies.

**What stays at Stripe:** the invoices and payments of the deleted customer, which Stripe keeps as long as it must. Say so in your privacy policy (Stripe as processor, retention of invoices).

A Stripe error (a rate limit, an outage, a key without the permission to delete customers) fails the erasure: Billing deletes nothing, Identity keeps the account locked and asks again with a growing wait, and `python -m identity.cli erasures` shows `billing` as the service it waits for. A restricted key needs write access to Customers and Subscriptions. Stripe sends `customer.subscription.deleted` and `customer.deleted` afterwards; Billing answers 200 and stores nothing of them (outcome `unknown_account` or `ignored`).

**Identity must know that Billing runs,** or it would erase an account everywhere else and leave the customer and a running subscription at Stripe. The deployment tells it: Compose passes `COMPOSE_PROFILES` to Identity and to Billing, the chart sets both from `services.billing.enabled`. `docker compose --profile billing up` without `COMPOSE_PROFILES=billing` in `.env` does not tell Identity, so Billing refuses to start then and says why in its log. After adding the profile, recreate the stack (`docker compose up -d --wait`): Identity has to restart to read it.

## Without a Stripe account

- **Fake provider** (local demo, nothing charged): `BILLING_PROVIDER=fake`, any `BILLING_STRIPE_WEBHOOK_SECRET`, price ids of your choice. Checkout and the portal return links back to the account page; send signed, Stripe shaped webhooks with

  ```bash
  docker compose -f infra/compose/docker-compose.yml --profile billing exec billing \
    python -m billing.cli send-fixture --account-id <account uuid> --status past_due
  ```

  (`--status active|trialing|past_due|unpaid|canceled|...`; the account id is in `GET /api/v1/identity/me`). Start a checkout for the account once before ("Upgrade" on the account page): Billing applies an event only to a customer it knows, and the checkout is what creates the customer. An event for an unknown customer answers `unknown_account`.
- **stripe-mock** (`stripe/stripe-mock`, pinned in `.github/workflows/ci.yml` for the python job): `BILLING_STRIPE_API_BASE=http://stripe-mock:12111` with any `sk_test_...` key validates every request against Stripe's API definition, but it keeps no state and sends no webhooks.

## Operations

- Logs: `webhook event handled` (event id, type, outcome, account), `webhook event already handled` (a replay), `checkout started`. The ledger is the `webhook_events` table of the `billing` database (outcomes `applied`, `ignored`, `stale`, `unknown_account`, `rejected`, `failed`).
- Stripe retries failed deliveries for up to three days; a 500 from MowDeck (Entitlements not reachable) is retried automatically. `rejected` (e.g. a price whose plan is unknown to Entitlements) is not retried: fix the price ids and resend the event from the Stripe dashboard.
- The webhook route is rate limited per client address in Traefik (120 per minute, burst 60); Stripe retries a 429. In Kubernetes attach the same middleware to the `-auth` Ingress (`ingress.auth.annotations`), which carries the webhook path when Billing is enabled.
- Disabling: remove the profile (and set `ENTITLEMENTS_BILLING_ENABLED=false` to give every account `unlimited` again). Subscriptions keep running at Stripe until they are cancelled there.
