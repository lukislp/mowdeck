"""Subscriptions of Entitlements.

* ``entitlements.check`` (core NATS request, queue ``entitlements``, internal): may an account
  use a feature, or have one more of a limit? Exactly one replica answers; counted per outcome
  (``mowdeck_entitlements_checks_total``).
* ``entitlements.plan.set`` (core NATS request, queue ``entitlements``, internal): the Billing
  service sets the plan of an account (``source=billing``). Core NATS is reachable by services
  only, so no principal is checked; an invalid request is answered with ``invalid_request``.
* ``event.identity.account.created`` (JetStream ``EVENTS``, durable ``entitlements-accounts``):
  gives the new account the default plan (a trial when the plan has trial days).

Handler rules: return to acknowledge, raise to get a redelivery, raise ``DropMessage`` to
discard a poison message.
"""

from mowdeck_contracts.v1 import (
    IDENTITY_ACCOUNT_CREATED,
    AccountCreated,
    EntitlementCheckRequest,
    EventEnvelope,
    PlanSetError,
    PlanSetReply,
    PlanSetRequest,
)
from mowdeck_contracts.v1.subjects import (
    ENTITLEMENTS_CHECK,
    ENTITLEMENTS_PLAN_SET,
    event_subject_for_type,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, MessageHandler, Subscription
from prometheus_client import CollectorRegistry, Counter
from pydantic import ValidationError

from entitlements.application.services import Services

_log = get_logger("entitlements.bus")

CHECK_QUEUE = "entitlements"
"""Queue group of ``entitlements.check``: exactly one replica answers."""
ACCOUNTS_CONSUMER = "entitlements-accounts"
"""Durable consumer (and deliver group) of ``identity.account.created``."""


class CheckMetrics:
    """``mowdeck_entitlements_checks_total{kind, outcome}``."""

    def __init__(self, registry: CollectorRegistry | None) -> None:
        self._checks = Counter(
            "mowdeck_entitlements_checks",
            "Answered entitlements.check requests.",
            ["kind", "outcome"],
            registry=registry,
        )

    def count(self, kind: str, outcome: str) -> None:
        """Count one answer."""
        self._checks.labels(kind=kind, outcome=outcome).inc()


def check_handler(services: Services, metrics: CheckMetrics | None = None) -> MessageHandler:
    """Answer ``entitlements.check`` with the decision and the account's snapshot."""

    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(EntitlementCheckRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid check request: {exc.error_count()} errors") from exc
        reply = await services.entitlements.check(request)
        await message.respond(reply)
        kind = "limit" if request.limit else ("feature" if request.feature else "plan")
        outcome = "allowed" if reply.allowed else (reply.reason or "denied")
        if metrics is not None:
            metrics.count(kind, str(outcome))
        _log.debug(
            "entitlement checked",
            account_id=str(request.account_id),
            kind=kind,
            feature=request.feature,
            limit=request.limit.key if request.limit else None,
            allowed=reply.allowed,
        )

    return handle


def plan_set_handler(services: Services) -> MessageHandler:
    """Answer ``entitlements.plan.set`` (Billing): store the plan, reply with the outcome."""

    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(PlanSetRequest)
        except ValidationError as exc:
            await message.respond(
                PlanSetReply(
                    ok=False,
                    error=PlanSetError.INVALID_REQUEST,
                    detail=f"{exc.error_count()} validation errors",
                )
            )
            return
        reply = await services.entitlements.set_from_billing(request)
        await message.respond(reply)

    return handle


def account_handler(services: Services) -> MessageHandler:
    """Give a new account the default plan."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[AccountCreated])
        except ValidationError as exc:
            raise DropMessage(f"invalid account.created event: {exc.error_count()} errors") from exc
        inserted = await services.entitlements.account_created(event.payload)
        _log.info("account plan ready", account_id=str(event.payload.account_id), inserted=inserted)

    return handle


async def subscribe_all(
    bus: EventBus, services: Services, registry: CollectorRegistry | None = None
) -> list[Subscription]:
    """Subscribe every handler of the service and return the subscriptions."""
    return [
        await bus.subscribe(
            ENTITLEMENTS_CHECK, check_handler(services, CheckMetrics(registry)), queue=CHECK_QUEUE
        ),
        await bus.subscribe(ENTITLEMENTS_PLAN_SET, plan_set_handler(services), queue=CHECK_QUEUE),
        await bus.subscribe(
            event_subject_for_type(IDENTITY_ACCOUNT_CREATED),
            account_handler(services),
            durable=ACCOUNTS_CONSUMER,
            queue=ACCOUNTS_CONSUMER,
        ),
    ]
