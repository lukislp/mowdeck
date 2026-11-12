# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Routes of billing under ``/api/v1/billing``.

======================  ======================  ============================================
Route                   Who                     What
======================  ======================  ============================================
GET  /status            public                  enabled, provider, currency, plans on sale
GET  /subscription      ``can_manage_billing``  the account's subscription
POST /checkout          ``can_manage_billing``  start a hosted checkout for a plan (URL)
POST /portal            ``can_manage_billing``  open the hosted customer portal (URL)
GET  /invoices          ``can_manage_billing``  the account's invoice references
POST /webhooks/stripe   signature only          provider events (raw body, rate limited)
GET  /operator/         ``can_manage_accounts`` refunds of erased accounts to pay back by
  manual-refunds        on ``platform:main``    hand (``?settled=true``: with settled ones)
POST /operator/         ``can_manage_accounts`` mark one as paid back (404 unknown id)
  manual-refunds/{id}/  on ``platform:main``
  settle
GET  /operator/         ``can_manage_accounts`` the payments dashboard of the last
  payments              on ``platform:main``    ``months`` (1 to 36, default 12; ADR 0083)
GET  /operator/prices   ``can_manage_accounts`` every price of the price file as Stripe has
                        on ``platform:main``    it (read only)
======================  ======================  ============================================

The dashboard is one summary endpoint rather than one per section: one request and one
transaction give every section the same moment, one authorization check guards it, and the
sections are small aggregates (a few dozen rows), so nothing is gained by loading them apart.

``can_manage_billing`` on the token's account: the owner (or a member granted
``billing.manage``); admins get 403, principals outside the account 404.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from mowdeck_service_kit import get_logger

from billing.adapters.api.deps import BillingManager, Operator, RequestBase, ServiceDep
from billing.adapters.api.schemas import (
    BillingStatusOut,
    CheckoutIn,
    InvoiceOut,
    ManualRefundOut,
    OperatorPriceOut,
    OperatorPricesOut,
    PaymentsSummaryOut,
    RedirectOut,
    SubscriptionSummaryOut,
    WebhookOut,
)
from billing.application.payments import DEFAULT_MONTHS, MAX_MONTHS
from billing.domain.errors import NotFoundError

router = APIRouter(prefix="/billing", tags=["billing"])
_log = get_logger("billing.api")


@router.get("/status")
async def status(service: ServiceDep) -> BillingStatusOut:
    """Billing runs here: the provider, the currency and the plans on sale with prices.

    Public (a pricing view needs no login). The web app probes it: a network error or 404
    (the service is not deployed, billing is disabled) hides every billing element.
    """
    return BillingStatusOut.of(await service.status())


@router.get("/subscription")
async def subscription(principal: BillingManager, service: ServiceDep) -> SubscriptionSummaryOut:
    """The subscription of the caller's account (``subscription: null`` before checkout)."""
    assert principal.account_id is not None  # noqa: S101 - guaranteed by the gate
    return SubscriptionSummaryOut.of(await service.summary(principal.account_id))


@router.post("/checkout")
async def checkout(
    body: CheckoutIn, principal: BillingManager, base: RequestBase, service: ServiceDep
) -> RedirectOut:
    """Start a hosted checkout for ``plan``; redirect the browser to ``url``.

    422 when the plan is not sold here, 409 when the account already has a subscription (plan
    changes and cancellation happen in the customer portal).
    """
    assert principal.account_id is not None  # noqa: S101 - guaranteed by the gate
    return RedirectOut(url=await service.start_checkout(principal.account_id, body.plan, base))


@router.post("/portal")
async def portal(principal: BillingManager, base: RequestBase, service: ServiceDep) -> RedirectOut:
    """Open the provider's customer portal (payment method, invoices, plan change, cancel).

    404 before the account's first checkout.
    """
    assert principal.account_id is not None  # noqa: S101 - guaranteed by the gate
    return RedirectOut(url=await service.open_portal(principal.account_id, base))


@router.get("/invoices")
async def invoices(principal: BillingManager, service: ServiceDep) -> list[InvoiceOut]:
    """The newest invoices of the caller's account (links to the provider's pages)."""
    assert principal.account_id is not None  # noqa: S101 - guaranteed by the gate
    return [InvoiceOut.of(i) for i in await service.invoices(principal.account_id)]


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    service: ServiceDep,
    stripe_signature: str | None = Header(default=None),
) -> WebhookOut:
    """Stripe events. No token: only the ``Stripe-Signature`` over the raw body counts.

    400 for an invalid signature, 200 for a handled or repeated event (each event id is
    processed once), 500 when handling failed (Stripe retries).
    """
    result = await service.handle_webhook(await request.body(), stripe_signature)
    return WebhookOut(
        event_id=result.event_id,
        outcome=result.outcome.value if result.outcome else None,
        duplicate=result.duplicate,
    )


@router.get("/operator/manual-refunds")
async def manual_refunds(
    principal: Operator, service: ServiceDep, settled: bool = False
) -> list[ManualRefundOut]:
    """Refunds of erased accounts that Billing could not make and the operator pays back by
    hand (ADR 0082), oldest first: the payment and invoice at the provider, amount, currency
    and why. Open ones only, with ``settled=true`` also those settled in the last 30 days. No
    record names an account."""
    del principal
    return [ManualRefundOut.of(r) for r in await service.manual_refunds.listing(settled=settled)]


@router.post("/operator/manual-refunds/{refund_id}/settle")
async def settle_manual_refund(
    refund_id: UUID, principal: Operator, service: ServiceDep
) -> ManualRefundOut:
    """Mark a manual refund as paid back. Settling again keeps the first time; 404 for an
    unknown id (or one deleted 30 days after it was settled)."""
    settled = await service.manual_refunds.settle(refund_id)
    if settled is None:
        raise NotFoundError(f"no manual refund {refund_id}")
    _log.info(
        "manual refund settled by an operator",
        manual_refund=str(refund_id),
        operator=str(principal.subject),
    )
    return ManualRefundOut.of(settled)


@router.get("/operator/payments")
async def payments(
    principal: Operator,
    service: ServiceDep,
    months: Annotated[int, Query(ge=1, le=MAX_MONTHS)] = DEFAULT_MONTHS,
) -> PaymentsSummaryOut:
    """The payments dashboard: revenue per month and currency (gross, refunds, net), the
    subscriptions per plan and status with the MRR, new and ended subscriptions, refunds by
    reason, failed payments, open and overdue invoices and disputes. Months are calendar months
    in the installation's report zone (``time_zone``: MOWDECK_BILLING_REPORT_TIMEZONE), amounts
    in the smallest currency unit and never added across currencies. Built from
    Billing's own data (the payments ledger written from webhooks since the deploy that added
    it, the subscription and invoice rows); Stripe is not asked. No answer names an account."""
    del principal
    return PaymentsSummaryOut.of(await service.payments.summary(months))


@router.get("/operator/prices")
async def prices(principal: Operator, service: ServiceDep) -> OperatorPricesOut:
    """Every price of the price file as Stripe has it (the same cache as ``GET /status``), also
    for plans Entitlements does not offer. Read only: prices are maintained in Stripe."""
    del principal
    listed = await service.prices()
    return OperatorPricesOut(
        currency=service.config.prices.currency,
        prices=[OperatorPriceOut.of(price) for price in listed.values()],
    )
