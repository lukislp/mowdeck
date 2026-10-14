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
======================  ======================  ============================================

``can_manage_billing`` on the token's account: the owner (or a member granted
``billing.manage``); admins get 403, principals outside the account 404.
"""

from fastapi import APIRouter, Header, Request

from billing.adapters.api.deps import BillingManager, RequestBase, ServiceDep
from billing.adapters.api.schemas import (
    BillingStatusOut,
    CheckoutIn,
    InvoiceOut,
    RedirectOut,
    SubscriptionSummaryOut,
    WebhookOut,
)

router = APIRouter(prefix="/billing", tags=["billing"])


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
