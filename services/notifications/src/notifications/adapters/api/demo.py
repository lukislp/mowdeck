"""What Notifications refuses in the public demo (ADR 0068), and the sandbox purge (ADR 0076).

The demo keeps every notification in the in-app inbox and sends nothing out: no Web Push
subscription (its endpoint is a third-party push service), no APNs device or Live Activity, no
ntfy topic and no test message. Preferences, the inbox, read and acknowledge work.

``DELETE /notifications/demo/accounts/{account_id}`` is for Identity's sandbox cleanup only
(404 outside demo mode, 403 without the seed secret) and hidden from the API documents.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from mowdeck_service_kit.demo import DemoPolicy, DemoPurgeOut, require_demo_seed, rule
from mowdeck_service_kit.errors import ServiceUnavailable

from notifications.adapters.api.deps import ServicesDep

_P = "/api/v1/notifications"
_OUTBOUND = (
    "The demo sends nothing outside the app: notifications stay in the inbox. "
    "Push, ntfy and the iOS app work in your own MowDeck."
)

DEMO_POLICY = DemoPolicy(
    blocked=(
        rule(rf"{_P}/push-subscriptions(/[^/]+)?", _OUTBOUND),
        rule(rf"{_P}/me/push/apns(/[^/]+)?", _OUTBOUND),
        rule(rf"{_P}/me/live-activities(/.*)?", _OUTBOUND),
        rule(rf"{_P}/ntfy", _OUTBOUND),
        rule(rf"{_P}/test", _OUTBOUND),
    )
)

router = APIRouter(prefix="/notifications/demo", tags=["notifications: demo"])


@router.delete("/accounts/{account_id}", include_in_schema=False)
async def demo_purge(
    account_id: UUID,
    _: Annotated[None, Depends(require_demo_seed)],
    services: ServicesDep,
) -> DemoPurgeOut:
    """Delete everything Notifications holds of an expired demo sandbox account: preferences,
    inbox, images, deliveries, devices, targets, silences, watches and the mower directory.
    Idempotent."""
    if services.demo is None:
        raise ServiceUnavailable("notifications is starting")
    return DemoPurgeOut(deleted=await services.demo.purge_account(account_id))
