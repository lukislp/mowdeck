"""Use cases: checkout, customer portal, subscription summary, invoices, and webhooks.

Billing only handles money; what an account may do is decided by Entitlements (ADR 0028). A
webhook that changes a subscription sets the account's plan there with ``entitlements.plan.set``
(``source=billing``) and publishes ``billing.subscription.changed``.

Webhooks are processed exactly once: every provider event id is recorded in ``webhook_events``
and claimed before it is handled; a repeated delivery of a processed event only answers 200. A
failure (Entitlements not answering) releases the claim and answers 500, so the provider
retries. Delivery order is not guaranteed, so an event older than the last one applied to a
subscription is stored as ``stale`` and changes nothing.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    AccountCreated,
    PlanSetRequest,
    PlanStatus,
    SubscriptionChanged,
    SubscriptionStatus,
)
from mowdeck_service_kit import get_logger

from billing.application.ports import (
    Clock,
    EntitlementsGateway,
    EventPublisher,
    PaymentProvider,
    PlanCatalog,
    Store,
)
from billing.domain.errors import ConflictError, InvalidInput, NotFoundError, ProviderError
from billing.domain.models import (
    BillingAccount,
    Customer,
    EventOutcome,
    Invoice,
    OfferedPlan,
    PlanPrice,
    ProviderEvent,
    ProviderSubscription,
    Subscription,
    WebhookEvent,
)
from billing.domain.prices import PriceTable
from billing.domain.rules import plan_decision

_log = get_logger("billing.service")

SUBSCRIPTION_EVENTS = frozenset(
    {
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    }
)
TRIAL_WILL_END = "customer.subscription.trial_will_end"
CHECKOUT_COMPLETED = "checkout.session.completed"
INVOICE_EVENTS = frozenset({"invoice.paid", "invoice.payment_failed"})
HANDLED_EVENTS = SUBSCRIPTION_EVENTS | INVOICE_EVENTS | {TRIAL_WILL_END, CHECKOUT_COMPLETED}
"""The provider event types Billing handles (the webhook endpoint should send only these)."""


@dataclass(frozen=True, slots=True)
class BillingConfig:
    """The settings the use cases need (built from ``Settings`` in ``main``)."""

    prices: PriceTable = field(default_factory=PriceTable)
    grace_days: int = 14
    automatic_tax: bool = False
    app_url: str = ""
    price_cache_seconds: float = 600.0
    invoice_limit: int = 50
    claim_lease_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class StatusPlan:
    """A plan on sale: the Entitlements plan (when known) with its provider price."""

    key: str
    price: PlanPrice
    name: str | None = None
    description: str | None = None
    trial_days: int = 0


@dataclass(frozen=True, slots=True)
class BillingStatus:
    """``GET /status``: what this installation sells."""

    provider: str
    currency: str
    automatic_tax: bool
    plans: list[StatusPlan]


@dataclass(frozen=True, slots=True)
class SubscriptionSummary:
    """``GET /subscription``: the account's current subscription (if any)."""

    provider: str
    has_customer: bool
    subscription: Subscription | None


@dataclass(frozen=True, slots=True)
class WebhookResult:
    """What a webhook delivery did."""

    event_id: str
    type: str
    outcome: EventOutcome | None
    duplicate: bool = False


@dataclass(slots=True)
class _PriceCache:
    at: datetime | None = None
    prices: dict[str, PlanPrice] = field(default_factory=dict)


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


class BillingService:
    """Checkout, portal, summaries and webhook handling."""

    def __init__(
        self,
        store: Store,
        provider: PaymentProvider,
        entitlements: EntitlementsGateway,
        catalog: PlanCatalog,
        events: EventPublisher,
        config: BillingConfig,
        clock: Clock,
    ) -> None:
        self.store = store
        self.provider = provider
        self.entitlements = entitlements
        self.catalog = catalog
        self.events = events
        self.config = config
        self.clock = clock
        self._cache = _PriceCache()
        self._cache_lock = asyncio.Lock()

    # --- accounts ------------------------------------------------------------------------

    async def account_created(self, event: AccountCreated) -> bool:
        """Remember name and kind of a new account (idempotent)."""
        async with self.store.transaction() as tx:
            return await tx.save_account_if_absent(
                BillingAccount(
                    account_id=event.account_id,
                    name=event.name,
                    kind=event.kind,
                    created_at=self.clock(),
                )
            )

    # --- status --------------------------------------------------------------------------

    async def _offered(self) -> dict[str, OfferedPlan]:
        try:
            return {plan.key: plan for plan in await self.catalog.offered()}
        except Exception:
            _log.warning("the plan catalogue of Entitlements is not reachable", exc_info=True)
            return {}

    async def prices(self) -> dict[str, PlanPrice]:
        """The provider prices of every plan on sale, cached ``price_cache_seconds``.

        A price that cannot be fetched is listed without amount (the page still offers it).
        """
        now = self.clock()
        async with self._cache_lock:
            cached = self._cache
            if (
                cached.at is not None
                and (now - cached.at).total_seconds() < self.config.price_cache_seconds
            ):
                return cached.prices
            prices: dict[str, PlanPrice] = {}
            for plan, price_id in self.config.prices.sold.items():
                try:
                    prices[plan] = await self.provider.fetch_price(plan, price_id)
                except Exception:
                    _log.warning("fetching a price failed", plan=plan, exc_info=True)
                    prices[plan] = PlanPrice(plan_key=plan, price_id=price_id)
            self._cache = _PriceCache(at=now, prices=prices)
            return prices

    async def status(self) -> BillingStatus:
        """The plans on sale with their prices (public: a pricing view needs no login).

        Plans that Entitlements knows but no longer offers are left out; when Entitlements
        cannot be reached every priced plan is listed without name.
        """
        offered = await self._offered()
        plans: list[StatusPlan] = []
        for key, price in (await self.prices()).items():
            plan = offered.get(key)
            if offered and plan is None:
                continue
            plans.append(
                StatusPlan(
                    key=key,
                    price=price,
                    name=plan.name if plan else None,
                    description=plan.description if plan else None,
                    trial_days=plan.trial_days if plan else 0,
                )
            )
        return BillingStatus(
            provider=self.provider.name,
            currency=self.config.prices.currency,
            automatic_tax=self.config.automatic_tax,
            plans=plans,
        )

    # --- checkout and portal -------------------------------------------------------------

    def _base_url(self, request_base: str) -> str:
        return (self.config.app_url or request_base).rstrip("/")

    async def _customer(self, account: BillingAccount) -> str:
        async with self.store.transaction() as tx:
            existing = await tx.get_customer(account.account_id, self.provider.name)
        if existing is not None:
            return existing.provider_customer_id
        customer_id = await self.provider.create_customer(account)
        async with self.store.transaction() as tx:
            await tx.save_customer(
                Customer(
                    account_id=account.account_id,
                    provider=self.provider.name,
                    provider_customer_id=customer_id,
                    created_at=self.clock(),
                )
            )
            stored = await tx.get_customer(account.account_id, self.provider.name)
        # A concurrent checkout may have stored another one first: use the stored one.
        return stored.provider_customer_id if stored else customer_id

    async def start_checkout(self, account_id: UUID, plan_key: str, request_base: str) -> str:
        """A hosted checkout for ``plan_key``; the URL to redirect the owner to.

        Refused when the plan is not sold here (422) or the account already has an open
        subscription (409: plan changes go through the customer portal). The plan's trial days
        apply to accounts that never had a subscription.
        """
        price_id = self.config.prices.price_for(plan_key)
        if price_id is None:
            raise InvalidInput(f"plan {plan_key!r} is not sold in this installation")
        async with self.store.transaction() as tx:
            account = await tx.get_account(account_id) or BillingAccount(account_id=account_id)
            history = await tx.subscriptions_of(account_id)
        if any(s.is_open for s in history):
            raise ConflictError("the account has a subscription; change it in the portal")
        trial_days = 0
        if not history:
            plan = (await self._offered()).get(plan_key)
            trial_days = plan.trial_days if plan else 0
        customer_id = await self._customer(account)
        base = self._base_url(request_base)
        url = await self.provider.create_checkout(
            account,
            customer_id,
            plan_key,
            price_id,
            success_url=f"{base}/account?billing=success#plan",
            cancel_url=f"{base}/account?billing=cancel#plan",
            trial_days=trial_days,
        )
        _log.info("checkout started", account_id=str(account_id), plan=plan_key, trial=trial_days)
        return url

    async def open_portal(self, account_id: UUID, request_base: str) -> str:
        """The customer portal of the account (404 before the first checkout)."""
        async with self.store.transaction() as tx:
            customer = await tx.get_customer(account_id, self.provider.name)
        if customer is None:
            raise NotFoundError("the account has no customer at the payment provider yet")
        return await self.provider.create_portal(
            customer.provider_customer_id, f"{self._base_url(request_base)}/account#plan"
        )

    # --- reading -------------------------------------------------------------------------

    async def summary(self, account_id: UUID) -> SubscriptionSummary:
        """The current subscription: the newest open one, else the newest one."""
        async with self.store.transaction() as tx:
            customer = await tx.get_customer(account_id, self.provider.name)
            subscriptions = await tx.subscriptions_of(account_id)
        current = next((s for s in subscriptions if s.is_open), None)
        if current is None and subscriptions:
            current = subscriptions[0]
        return SubscriptionSummary(self.provider.name, customer is not None, current)

    async def invoices(self, account_id: UUID) -> list[Invoice]:
        """The newest invoices of the account."""
        async with self.store.transaction() as tx:
            return await tx.invoices_of(account_id, self.config.invoice_limit)

    # --- webhooks ------------------------------------------------------------------------

    async def handle_webhook(self, payload: bytes, signature: str | None) -> WebhookResult:
        """Verify, record, claim and apply one provider event (exactly once)."""
        event = self.provider.parse_webhook(payload, signature)
        provider = self.provider.name
        now = self.clock()
        async with self.store.transaction() as tx:
            await tx.record_event(WebhookEvent(provider, event.id, event.type, received_at=now))
            claimed = await tx.claim_event(provider, event.id, now, self.config.claim_lease_seconds)
            stored = None if claimed else await tx.get_event(provider, event.id)
        if not claimed:
            _log.info("webhook event already handled", event_id=event.id, type=event.type)
            return WebhookResult(
                event.id, event.type, stored.outcome if stored else None, duplicate=True
            )
        try:
            outcome, account_id = await self._apply(event)
        except Exception:
            _log.exception("handling a webhook event failed", event_id=event.id, type=event.type)
            async with self.store.transaction() as tx:
                await tx.finish_event(provider, event.id, EventOutcome.FAILED, None, None)
            raise
        async with self.store.transaction() as tx:
            await tx.finish_event(provider, event.id, outcome, self.clock(), account_id)
        _log.info(
            "webhook event handled",
            event_id=event.id,
            type=event.type,
            outcome=outcome.value,
            account_id=str(account_id) if account_id else None,
        )
        return WebhookResult(event.id, event.type, outcome)

    async def _account_of_customer(self, customer_id: str | None) -> UUID | None:
        if not customer_id:
            return None
        async with self.store.transaction() as tx:
            customer = await tx.find_customer(self.provider.name, customer_id)
        return customer.account_id if customer else None

    async def _remember_customer(self, account_id: UUID, customer_id: str | None) -> None:
        if not customer_id:
            return
        async with self.store.transaction() as tx:
            if await tx.get_customer(account_id, self.provider.name) is None:
                await tx.save_customer(
                    Customer(account_id, self.provider.name, customer_id, self.clock())
                )

    async def _apply(self, event: ProviderEvent) -> tuple[EventOutcome, UUID | None]:
        if event.type == CHECKOUT_COMPLETED and event.checkout is not None:
            checkout = event.checkout
            account_id = checkout.account_id or await self._account_of_customer(
                checkout.customer_id
            )
            if account_id is None:
                return EventOutcome.UNKNOWN_ACCOUNT, None
            await self._remember_customer(account_id, checkout.customer_id)
            if checkout.subscription_id is None:
                return EventOutcome.APPLIED, account_id
            subscription = await self.provider.fetch_subscription(checkout.subscription_id)
            return await self._apply_subscription(subscription, event, account_id)
        if event.type in SUBSCRIPTION_EVENTS and event.subscription is not None:
            return await self._apply_subscription(event.subscription, event, None)
        if event.type == TRIAL_WILL_END and event.subscription is not None:
            return await self._trial_will_end(event.subscription, event)
        if event.type in INVOICE_EVENTS and event.invoice is not None:
            return await self._apply_invoice(event)
        return EventOutcome.IGNORED, None

    async def _apply_subscription(
        self, data: ProviderSubscription, event: ProviderEvent, hint: UUID | None
    ) -> tuple[EventOutcome, UUID | None]:
        provider = self.provider.name
        now = self.clock()
        account_id = data.account_id or hint or await self._account_of_customer(data.customer_id)
        if account_id is None:
            return EventOutcome.UNKNOWN_ACCOUNT, None
        async with self.store.transaction() as tx:
            existing = await tx.get_subscription(provider, data.id)
            others = [s for s in await tx.subscriptions_of(account_id) if s.id != data.id]
            account = await tx.get_account(account_id)
        if existing and existing.last_event_at and event.created < existing.last_event_at:
            return EventOutcome.STALE, account_id
        plan_key = (
            self.config.prices.plan_for(data.price_id)
            or data.plan_key
            or (existing.plan_key if existing else None)
        )
        decision = plan_decision(
            data.status,
            now=now,
            grace_days=self.config.grace_days,
            current_period_start=data.current_period_start,
            current_period_end=data.current_period_end,
            ended_at=data.ended_at,
            trial_end=data.trial_end,
        )
        # A late event of an old subscription must not restrict an account that has a newer
        # running one.
        superseded = (
            decision is not None
            and decision.status.is_inactive
            and any(
                s.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING) for s in others
            )
        )
        outcome = EventOutcome.APPLIED
        plan_status: PlanStatus | None = None
        if decision is not None and not superseded:
            if plan_key is None:
                _log.error("a subscription has no known plan", subscription=data.id)
                outcome = EventOutcome.REJECTED
            else:
                reply = await self.entitlements.set_plan(
                    PlanSetRequest(
                        account_id=account_id,
                        plan=plan_key,
                        status=decision.status,
                        trial_until=decision.trial_until,
                        grace_until=decision.grace_until,
                        reason=f"{provider} {event.type} ({data.status.value})",
                        account_name=account.name if account else None,
                        reference=data.id,
                    )
                )
                if reply.ok:
                    plan_status = decision.status
                else:
                    _log.error(
                        "entitlements refused the plan",
                        plan=plan_key,
                        error=reply.error,
                        detail=reply.detail,
                    )
                    outcome = EventOutcome.REJECTED
        await self._remember_customer(account_id, data.customer_id)
        stored = Subscription(
            id=existing.id if existing else uuid4(),
            account_id=account_id,
            provider=provider,
            provider_subscription_id=data.id,
            plan_key=plan_key,
            status=data.status,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            current_period_end=data.current_period_end,
            trial_end=data.trial_end,
            cancel_at_period_end=data.cancel_at_period_end,
            plan_status=plan_status or (existing.plan_status if existing else None),
            grace_until=decision.grace_until if decision and plan_status else None,
            last_event_at=_later(existing.last_event_at if existing else None, event.created),
        )
        async with self.store.transaction() as tx:
            await tx.save_subscription(stored)
        await self.events.subscription_changed(
            SubscriptionChanged(
                account_id=account_id,
                provider=provider,
                plan=plan_key,
                status=data.status,
                plan_status=plan_status,
                current_period_end=data.current_period_end,
                trial_end=data.trial_end,
                grace_until=stored.grace_until,
                cancel_at_period_end=data.cancel_at_period_end,
                event_type=event.type,
            )
        )
        return outcome, account_id

    async def _trial_will_end(
        self, data: ProviderSubscription, event: ProviderEvent
    ) -> tuple[EventOutcome, UUID | None]:
        account_id = data.account_id or await self._account_of_customer(data.customer_id)
        if account_id is None:
            return EventOutcome.UNKNOWN_ACCOUNT, None
        plan_key = self.config.prices.plan_for(data.price_id) or data.plan_key
        await self.events.subscription_changed(
            SubscriptionChanged(
                account_id=account_id,
                provider=self.provider.name,
                plan=plan_key,
                status=data.status,
                current_period_end=data.current_period_end,
                trial_end=data.trial_end,
                cancel_at_period_end=data.cancel_at_period_end,
                event_type=event.type,
            )
        )
        return EventOutcome.APPLIED, account_id

    async def _apply_invoice(self, event: ProviderEvent) -> tuple[EventOutcome, UUID | None]:
        data = event.invoice
        assert data is not None  # noqa: S101 - checked by the caller
        account_id = await self._account_of_customer(data.customer_id)
        if account_id is None:
            return EventOutcome.UNKNOWN_ACCOUNT, None
        now = self.clock()
        async with self.store.transaction() as tx:
            await tx.save_invoice(
                Invoice(
                    id=uuid4(),
                    account_id=account_id,
                    provider=self.provider.name,
                    provider_invoice_id=data.id,
                    status=data.status,
                    currency=data.currency,
                    amount_due=data.amount_due,
                    amount_paid=data.amount_paid,
                    issued_at=data.created,
                    updated_at=now,
                    number=data.number,
                    provider_subscription_id=data.subscription_id,
                    hosted_url=data.hosted_url,
                    pdf_url=data.pdf_url,
                )
            )
        return EventOutcome.APPLIED, account_id

    async def cancel(self, account_id: UUID) -> list[str]:
        """Cancel every open subscription of an account now (account deletion); the ids."""
        async with self.store.transaction() as tx:
            open_ones = [s for s in await tx.subscriptions_of(account_id) if s.is_open]
        cancelled: list[str] = []
        for subscription in open_ones:
            try:
                await self.provider.cancel_subscription(subscription.provider_subscription_id)
            except Exception as exc:
                raise ProviderError("cancelling at the payment provider failed") from exc
            cancelled.append(subscription.provider_subscription_id)
        return cancelled
