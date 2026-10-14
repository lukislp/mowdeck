"""Subscriptions of Billing.

* ``event.identity.account.created`` (JetStream ``EVENTS``, durable and deliver group
  ``billing-accounts``): remembers name and kind of the account (business accounts give a tax id
  and a billing address in Checkout).

Handler rules: return to acknowledge, raise to get a redelivery, raise ``DropMessage`` to
discard a poison message.
"""

from mowdeck_contracts.v1 import IDENTITY_ACCOUNT_CREATED, AccountCreated, EventEnvelope
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, MessageHandler, Subscription
from pydantic import ValidationError

from billing.application.billing import BillingService

_log = get_logger("billing.bus")

ACCOUNTS_CONSUMER = "billing-accounts"
"""Durable consumer (and deliver group) of ``identity.account.created``."""


def account_handler(service: BillingService) -> MessageHandler:
    """Remember a new account."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[AccountCreated])
        except ValidationError as exc:
            raise DropMessage(f"invalid account.created event: {exc.error_count()} errors") from exc
        inserted = await service.account_created(event.payload)
        _log.info("account known", account_id=str(event.payload.account_id), inserted=inserted)

    return handle


async def subscribe_all(bus: EventBus, service: BillingService) -> list[Subscription]:
    """Subscribe every handler of the service and return the subscriptions."""
    return [
        await bus.subscribe(
            event_subject_for_type(IDENTITY_ACCOUNT_CREATED),
            account_handler(service),
            durable=ACCOUNTS_CONSUMER,
            queue=ACCOUNTS_CONSUMER,
        )
    ]
