# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A token of a deleted user or an erased account writes nothing again (ADR 0082).

Notifications admits ``PUT /preferences`` on the token's ``notifications.manage`` claim alone
and verifies tokens offline. A client whose user was just deleted, with an access token that is
valid for minutes more, could store preferences (or a push subscription) again after the
service dropped the user's rows. Identity publishes ``identity.access.revoked`` first; the token
verification of the kit (the real resolver of ``mowdeck-auth``, attached to the bus by
``create_app``) refuses the token from then on. Notifications has no code for it.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from mowdeck_contracts.v1 import (
    IDENTITY_ACCESS_REVOKED,
    IDENTITY_USER_DELETED,
    AccessRevoked,
    AccessScope,
    EventEnvelope,
    UserChanged,
)
from mowdeck_contracts.v1.subjects import access_revoked_subject
from mowdeck_service_kit.bus import publish_event
from notifications_support import API, AUTH, P256DH, Harness, Person, harness

PERMS = ["mower.view", "notifications.manage"]
LIFETIME = timedelta(minutes=11)


async def revoke(h: Harness, scope: AccessScope, target: UUID, account_id: UUID) -> None:
    """What Identity publishes when access ends: a deny list entry on the subject of the id."""
    now = datetime.now(UTC)
    entry = AccessRevoked(scope=scope, id=target, issued_before=now, expires_at=now + LIFETIME)
    await publish_event(
        h.bus,
        EventEnvelope[AccessRevoked](
            type=IDENTITY_ACCESS_REVOKED, producer="identity", account_id=account_id, payload=entry
        ),
        subject=access_revoked_subject(target),
    )


async def user_deleted(h: Harness, person: Person) -> None:
    await publish_event(
        h.bus,
        EventEnvelope[UserChanged](
            type=IDENTITY_USER_DELETED,
            version=2,
            producer="identity",
            account_id=person.account_id,
            payload=UserChanged(
                user_id=person.user_id, account_id=person.account_id, reason="self"
            ),
        ),
    )


async def write_preferences(h: Harness, person: Person) -> int:
    """``PUT /preferences`` as the client sends it; the status, whatever it is."""
    body = {"enabled_kinds": ["battery_low"], "all_mowers": True, "channels": ["log"]}
    response = await h.client.put(f"{API}/preferences", json=body, headers=person.headers)
    return response.status_code


async def push_subscription(h: Harness, person: Person) -> int:
    response = await h.client.post(
        f"{API}/push-subscriptions",
        headers=person.headers,
        json={
            "endpoint": "https://push.example.test/send/after-deletion",
            "keys": {"p256dh": P256DH, "auth": AUTH},
        },
    )
    return response.status_code


async def test_a_deleted_users_token_cannot_write_preferences_again() -> None:
    async with harness() as h:
        owner = await h.owner()
        member = await h.member(owner, perms=PERMS)
        assert await write_preferences(h, member) == 200
        assert await h.store.preferences(member.user_id, member.account_id) is not None

        # "Delete me" in Identity: the deny list entry, then the event.
        await revoke(h, AccessScope.SUBJECT, member.user_id, member.account_id)
        await user_deleted(h, member)
        assert await h.store.preferences(member.user_id, member.account_id) is None

        # The client still holds its access token, valid for minutes.
        written = await write_preferences(h, member)
        subscribed = await push_subscription(h, member)
        read = await h.client.get(f"{API}/preferences", headers=member.headers)

        assert written == 401
        assert subscribed == 401
        assert read.status_code == 401
        assert await h.store.preferences(member.user_id, member.account_id) is None
        assert not [s for s in h.store.data.push.values() if s.user_id == member.user_id]
        # Nobody else is signed out.
        assert await write_preferences(h, owner) == 200


async def test_without_the_deny_list_entry_the_same_token_would_have_written_again() -> None:
    """The gap this closes: the event alone removes the rows, the token brings them back."""
    async with harness() as h:
        owner = await h.owner()
        member = await h.member(owner, perms=PERMS)
        assert await write_preferences(h, member) == 200
        await user_deleted(h, member)
        assert await h.store.preferences(member.user_id, member.account_id) is None

        assert await write_preferences(h, member) == 200
        assert await h.store.preferences(member.user_id, member.account_id) is not None


async def test_an_erased_accounts_tokens_are_refused_and_other_accounts_are_not() -> None:
    async with harness() as h:
        owner, stranger = await h.owner(), await h.owner()
        member = await h.member(owner, perms=PERMS)

        await revoke(h, AccessScope.ACCOUNT, owner.account_id, owner.account_id)

        assert await write_preferences(h, owner) == 401
        assert await write_preferences(h, member) == 401
        assert await write_preferences(h, stranger) == 200


async def test_a_token_issued_after_the_revocation_is_not_refused() -> None:
    """The entry names tokens issued until the access ended, not the id for ever."""
    async with harness() as h:
        owner = await h.owner()
        entry_at = datetime.now(UTC) - timedelta(minutes=5)
        await publish_event(
            h.bus,
            EventEnvelope[AccessRevoked](
                type=IDENTITY_ACCESS_REVOKED,
                producer="identity",
                account_id=owner.account_id,
                payload=AccessRevoked(
                    scope=AccessScope.SUBJECT,
                    id=owner.user_id,
                    issued_before=entry_at,
                    expires_at=entry_at + LIFETIME,
                ),
            ),
            subject=access_revoked_subject(owner.user_id),
        )

        assert await write_preferences(h, owner) == 200  # minted now, after the entry
