"""Login from a new device (ADR 0065): Identity's event in, one inbox item for that user out."""

from typing import Any
from uuid import uuid4

from mowdeck_contracts.v1 import (
    IDENTITY_SESSION_NEW_DEVICE,
    EventEnvelope,
    NewDeviceLogin,
    NotificationKind,
)
from mowdeck_service_kit.bus import publish_event
from notifications_support import T0, Harness, Person, harness

from notifications.domain.models import ACCOUNT_KINDS, RULE_KINDS, SEVERITY


async def send(h: Harness, person: Person, **fields: Any) -> NewDeviceLogin:
    payload = NewDeviceLogin(
        user_id=fields.pop("user_id", person.user_id),
        account_id=fields.pop("account_id", person.account_id),
        session_id=fields.pop("session_id", uuid4()),
        device=fields.pop("device", "Firefox on Windows"),
        client=fields.pop("client", "browser"),
        browser=fields.pop("browser", "Firefox"),
        os=fields.pop("os", "Windows"),
        ip_prefix=fields.pop("ip_prefix", "8.8.4.x"),
        network=fields.pop("network", "public"),
        method=fields.pop("method", "password"),
        at=fields.pop("at", T0),
        **fields,
    )
    await publish_event(
        h.bus,
        EventEnvelope[NewDeviceLogin](
            type=IDENTITY_SESSION_NEW_DEVICE,
            producer="identity",
            account_id=payload.account_id,
            payload=payload,
        ),
    )
    return payload


async def test_a_new_device_login_reaches_only_that_user_also_in_quiet_hours() -> None:
    async with harness() as h:
        owner = await h.owner()
        other = await h.member(owner, perms=["notifications.manage"])
        await h.subscribe_all(
            owner,
            timezone="Europe/Berlin",
            quiet_hours={"start": "13:30", "end": "15:00"},  # T0 is 14:00 in Berlin
            language="de",
        )
        alert = await send(h, owner)
        await send(h, owner, session_id=alert.session_id)  # a redelivery
        items = await h.inbox(owner)
        others = await h.inbox(other)
        raised = h.bus.messages("event.notifications.raised")
        sent = h.bus.messages("event.notifications.sent")
        await h.services.outbox.run_once()
    assert [i["kind"] for i in items] == ["security_new_login"]
    item = items[0]
    assert item["severity"] == "warning"
    assert item["title"] == "Neue Anmeldung bei MowDeck"
    assert "Firefox auf Windows" in item["body"] and "8.8.4.x" in item["body"]
    assert item["url"].endswith(f"/settings/security?revoke={alert.session_id}")
    assert item["mower_id"] is None
    assert others == []
    assert raised == []  # never published to the account (Home Assistant tokens)
    assert len(sent) == 1


async def test_the_text_names_the_app_and_the_home_network() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.subscribe_all(owner, language="en")
        await send(
            h,
            owner,
            device="MowDeck app on iOS",
            client="app",
            browser=None,
            os="iOS",
            ip_prefix="192.168.1.x",
            network="local",
        )
        await send(
            h, owner, device="curl", client="script", browser="curl", os=None, ip_prefix=None
        )
        bodies = sorted(i["body"] for i in await h.inbox(owner))
    assert bodies[0].startswith("Signed in from curl (unknown address).")
    assert bodies[1].startswith(
        "Signed in from the MowDeck app on iOS (home network, 192.168.1.x)."
    )


async def test_a_user_without_saved_preferences_still_gets_it_in_the_inbox() -> None:
    async with harness() as h:
        owner = await h.owner()
        await send(h, owner)
        items = await h.inbox(owner)
    assert [i["kind"] for i in items] == ["security_new_login"]


async def test_an_event_with_mismatching_accounts_is_dropped() -> None:
    async with harness() as h:
        owner = await h.owner()
        stranger = await h.owner()
        payload = NewDeviceLogin(
            user_id=owner.user_id,
            account_id=owner.account_id,
            session_id=uuid4(),
            device="Firefox on Windows",
            method="password",
            at=T0,
        )
        await publish_event(
            h.bus,
            EventEnvelope[NewDeviceLogin](
                type=IDENTITY_SESSION_NEW_DEVICE,
                producer="identity",
                account_id=stranger.account_id,
                payload=payload,
            ),
        )
        assert await h.inbox(owner) == []


def test_the_kind_is_not_a_preference_kind() -> None:
    kind = NotificationKind.SECURITY_NEW_LOGIN
    assert kind in ACCOUNT_KINDS and kind not in RULE_KINDS
    assert SEVERITY[kind].value == "warning"
