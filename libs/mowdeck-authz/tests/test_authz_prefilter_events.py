"""Token pre-filter (perms as an upper bound) and cache invalidation by grants.changed events."""

from uuid import uuid4

from mowdeck_authz import Relation, RelationshipTuple, account, user
from mowdeck_authz.events import (
    GRANTS_CHANGED_SUBJECT,
    apply_grants_changed,
    subscribe_cache_invalidation,
)
from mowdeck_authz.fastapi import install_authz, prefilter_denies
from mowdeck_authz.testing import AllowAll, InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    IDENTITY_GRANTS_CHANGED,
    EventEnvelope,
    GrantsChanged,
    PrincipalKind,
)
from mowdeck_service_kit.bus import InMemoryBus, publish_event
from mowdeck_service_kit.testing import lifespan_client, make_principal
from test_authz_fastapi import Story


def test_prefilter_only_denies_keys_missing_from_the_token() -> None:
    viewer = make_principal(permissions=["mower.view"])
    assert not prefilter_denies(viewer, Relation.CAN_VIEW)
    assert not prefilter_denies(viewer, Relation.CAN_EMERGENCY_STOP)
    assert prefilter_denies(viewer, Relation.CAN_CONTROL)
    assert prefilter_denies(viewer, Relation.CAN_MANAGE_USERS)
    assert not prefilter_denies(viewer, Relation.MEMBER)  # no key: always asked
    assert not prefilter_denies(viewer, Relation.CAN_MANAGE_ACCOUNT)


async def test_prefilter_denies_without_asking_and_keeps_403_versus_404() -> None:
    story = Story()
    story.anna = make_principal(
        account_id=story.account_a, kind=PrincipalKind.SUBUSER, permissions=["mower.view"]
    )
    story.authz.add(
        RelationshipTuple(user(story.anna.subject), "member", account(story.account_a)),
        RelationshipTuple(user(story.anna.subject), "grant_mower_view", account(story.account_a)),
        RelationshipTuple(
            user(story.anna.subject), "grant_mower_control", account(story.account_a)
        ),
    )
    app = story.app()
    install_authz(app, story.authz, token_prefilter=True)
    story.act_as(story.anna)
    async with lifespan_client(app) as client:
        start = await client.post(f"/api/v1/mowers/{story.m1}/start")
        create = await client.post("/api/v1/mowers")
    # FGA would allow can_control, but the token does not list mower.control: denied, and
    # the visible check (mower.view is listed) still turns the answer into 403, not 404.
    assert start.status_code == 403
    assert create.status_code == 403
    asked = [str(c.relation) for c in story.authz.checks]
    assert "can_control" not in asked
    assert "can_manage_mowers" not in asked
    assert asked == ["can_view"]


async def test_prefilter_with_every_relation_filtered_asks_nothing() -> None:
    story = Story()
    authz = AllowAll()
    app = story.app(authz)
    install_authz(app, authz, token_prefilter=True)
    story.act_as(make_principal(account_id=story.account_a, permissions=[]))
    async with lifespan_client(app) as client:
        response = await client.post(f"/api/v1/mowers/{story.m1}/start")
    assert response.status_code == 404
    assert authz.checks == []


class RecordingCache:
    def __init__(self) -> None:
        self.subjects: list[str] = []
        self.cleared = 0

    def invalidate(self) -> None:
        self.cleared += 1

    def invalidate_subject(self, subject: object) -> None:
        self.subjects.append(str(subject))


async def test_grants_changed_events_invalidate_the_cache() -> None:
    bus, cache = InMemoryBus(), RecordingCache()
    subscription = await subscribe_cache_invalidation(bus, cache)
    who = f"user:{uuid4()}"
    await publish_event(
        bus,
        EventEnvelope[GrantsChanged](
            type=IDENTITY_GRANTS_CHANGED,
            producer="identity",
            payload=GrantsChanged(subjects=[who]),
        ),
    )
    await publish_event(
        bus,
        EventEnvelope[GrantsChanged](
            type=IDENTITY_GRANTS_CHANGED,
            producer="identity",
            payload=GrantsChanged(all_subjects=True),
        ),
    )
    await bus.publish(GRANTS_CHANGED_SUBJECT, b"{not json")
    assert cache.subjects == [who]
    assert cache.cleared == 1
    assert len(bus.dropped) == 1
    await subscription.unsubscribe()


def test_apply_grants_changed_on_the_fake() -> None:
    authz = InMemoryAuthorizer()
    apply_grants_changed(authz, GrantsChanged(subjects=[f"api_token:{uuid4()}"]))
    apply_grants_changed(authz, GrantsChanged(all_subjects=True))
