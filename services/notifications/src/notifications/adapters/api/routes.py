"""Routes of notifications under ``/api/v1/notifications``.

Everything except the VAPID public key needs a user of an account (see
:func:`~notifications.adapters.api.deps.notification_user`) and acts on that user only.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit import CurrentAccountId
from mowdeck_service_kit.entitlements import feature_gate
from mowdeck_service_kit.errors import NotFound

from notifications.adapters.api.deps import NotificationUser, ServicesDep
from notifications.adapters.api.schemas import (
    ApnsDeviceIn,
    ApnsDeviceList,
    ApnsDeviceOut,
    Inbox,
    InboxItemOut,
    NtfyIn,
    NtfyOut,
    PreferencesBody,
    PreferencesOut,
    PushSubscriptionIn,
    PushSubscriptionList,
    PushSubscriptionOut,
    TestSent,
    VapidPublicKey,
)
from notifications.domain.models import Preferences
from notifications.module import PUSH_FEATURE

router = APIRouter(prefix="/notifications", tags=["notifications"])

PushInPlan = Annotated[Principal, Depends(feature_gate(PUSH_FEATURE))]
"""The plan feature ``notifications.push`` (403 ``feature-not-in-plan``, ADR 0028)."""


def _preferences_out(request: Request, prefs: Preferences) -> PreferencesOut:
    return PreferencesOut.of(
        prefs,
        channels=request.app.state.notification_channels.names(),
        languages=request.app.state.message_catalog.languages,
    )


@router.get("/push/vapid-public-key")
async def vapid_public_key(request: Request) -> VapidPublicKey:
    """The ``applicationServerKey`` (public, no token); 404 when Web Push is not configured."""
    key: str | None = getattr(request.app.state, "vapid_public_key", None)
    if key is None or request.app.state.notification_channels.get("webpush") is None:
        raise NotFound("web push is not configured")
    return VapidPublicKey(public_key=key)


@router.get("/preferences")
async def get_preferences(
    request: Request,
    principal: NotificationUser,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> PreferencesOut:
    """The caller's preferences (defaults until saved: subscribed to nothing)."""
    prefs = await services.users.preferences(principal.subject, account_id)
    return _preferences_out(request, prefs)


@router.put("/preferences")
async def put_preferences(
    request: Request,
    body: PreferencesBody,
    principal: NotificationUser,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> PreferencesOut:
    """Replace the caller's preferences; a mower the caller cannot view is 404."""
    prefs = await services.users.put_preferences(principal.subject, account_id, body.to_input())
    return _preferences_out(request, prefs)


@router.get("/push-subscriptions")
async def list_push_subscriptions(
    principal: NotificationUser, account_id: CurrentAccountId, services: ServicesDep
) -> PushSubscriptionList:
    """The caller's browser subscriptions."""
    items = await services.users.push_subscriptions(principal.subject, account_id)
    return PushSubscriptionList(items=[PushSubscriptionOut.of(s) for s in items])


@router.post("/push-subscriptions", status_code=status.HTTP_201_CREATED)
async def add_push_subscription(
    body: PushSubscriptionIn,
    principal: NotificationUser,
    account_id: CurrentAccountId,
    services: ServicesDep,
    _plan: PushInPlan,
    user_agent: Annotated[str | None, Header()] = None,
) -> PushSubscriptionOut:
    """Store the browser's ``PushSubscription.toJSON()`` for the caller (plan feature
    ``notifications.push``)."""
    stored = await services.users.add_push_subscription(
        principal.subject,
        account_id,
        endpoint=body.endpoint,
        p256dh=body.keys.p256dh,
        auth=body.keys.auth,
        user_agent=user_agent,
    )
    return PushSubscriptionOut.of(stored)


@router.delete("/push-subscriptions/{subscription_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_push_subscription(
    subscription_id: UUID,
    principal: NotificationUser,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> Response:
    """Delete one of the caller's subscriptions (404 for any other id)."""
    await services.users.delete_push_subscription(principal.subject, account_id, subscription_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me/push/apns")
async def list_apns_devices(
    principal: NotificationUser, account_id: CurrentAccountId, services: ServicesDep
) -> ApnsDeviceList:
    """The caller's iOS devices, oldest first."""
    items = await services.users.apns_devices(principal.subject, account_id)
    return ApnsDeviceList(items=[ApnsDeviceOut.of(d) for d in items])


@router.post("/me/push/apns", status_code=status.HTTP_201_CREATED)
async def add_apns_device(
    body: ApnsDeviceIn,
    principal: NotificationUser,
    account_id: CurrentAccountId,
    services: ServicesDep,
    _plan: PushInPlan,
) -> ApnsDeviceOut:
    """Register the iOS app's APNs device token for the caller (plan feature
    ``notifications.push``); the same token again replaces the earlier owner and name."""
    stored = await services.users.add_apns_device(
        principal.subject, account_id, token=body.token, device_name=body.device_name
    )
    return ApnsDeviceOut.of(stored)


@router.delete("/me/push/apns/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_apns_device(
    device_id: UUID,
    principal: NotificationUser,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> Response:
    """Delete one of the caller's iOS devices (the app calls it on logout; 404 for any other
    id)."""
    await services.users.delete_apns_device(principal.subject, account_id, device_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/ntfy")
async def get_ntfy(
    principal: NotificationUser, account_id: CurrentAccountId, services: ServicesDep
) -> NtfyOut:
    """The caller's ntfy target (404 when none is configured)."""
    return NtfyOut.of(await services.users.ntfy_target(principal.subject, account_id))


@router.put("/ntfy")
async def put_ntfy(
    body: NtfyIn, principal: NotificationUser, account_id: CurrentAccountId, services: ServicesDep
) -> NtfyOut:
    """Set the caller's ntfy target (the server must be on the allow list)."""
    target = await services.users.put_ntfy_target(
        principal.subject,
        account_id,
        server_url=body.server_url,
        topic=body.topic,
        token=body.token,
    )
    return NtfyOut.of(target)


@router.delete("/ntfy", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ntfy(
    principal: NotificationUser, account_id: CurrentAccountId, services: ServicesDep
) -> Response:
    """Remove the caller's ntfy target."""
    await services.users.delete_ntfy_target(principal.subject, account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/test", status_code=status.HTTP_202_ACCEPTED)
async def send_test(
    principal: NotificationUser, account_id: CurrentAccountId, services: ServicesDep
) -> TestSent:
    """Send a test notification over every configured channel of the caller (rate limited)."""
    notified = await services.users.send_test(principal.subject, account_id)
    return TestSent(
        notification_id=notified.item.id,
        channels=notified.channels,
        title=notified.item.title,
        body=notified.item.body,
    )


@router.get("/inbox")
async def get_inbox(
    principal: NotificationUser,
    account_id: CurrentAccountId,
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    unread: Annotated[bool, Query(description="Only unread notifications.")] = False,
) -> Inbox:
    """The caller's newest notifications, newest first."""
    items, unread_count = await services.users.inbox(
        principal.subject, account_id, limit, unread_only=unread
    )
    return Inbox(items=[InboxItemOut.of(i) for i in items], unread=unread_count)


@router.post("/inbox/{notification_id}/read")
async def mark_read(
    notification_id: UUID,
    principal: NotificationUser,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> InboxItemOut:
    """Mark one of the caller's notifications read (idempotent; 404 for any other id)."""
    item = await services.users.mark_read(principal.subject, account_id, notification_id)
    return InboxItemOut.of(item)


@router.post("/inbox/{notification_id}/ack")
async def acknowledge(
    notification_id: UUID,
    principal: NotificationUser,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> InboxItemOut:
    """Acknowledge one of the caller's notifications: read plus acknowledged (idempotent; 404 for
    any other id). For a theft alarm, repeats of it (same kind and mower) are silenced for the
    caller for an hour (``NOTIFICATIONS_ACK_SILENCE_SECONDS``)."""
    item = await services.users.acknowledge(principal.subject, account_id, notification_id)
    return InboxItemOut.of(item)
