"""Startup and shutdown, settings, the module descriptor, the bus handlers of the view."""

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import (
    IDENTITY_USER_DELETED,
    MODULE_DESCRIPTOR_PATH,
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    EmergencyStop,
    EventEnvelope,
    ManualDrive,
    PrincipalKind,
    PrincipalRef,
    UserChanged,
    utc_now,
)
from mowdeck_contracts.v1.subjects import (
    command_result_subject,
    command_subject,
    device_subject,
    telemetry_subject,
)
from mowdeck_service_kit.bus import InMemoryBus, publish_event
from mowdeck_service_kit.testing import FakeEntitlements, lifespan_client
from vision_support import StaticFactory, harness, make_settings, wait_for

from vision.adapters.bus.view import BusMowerView
from vision.adapters.memory import MemoryStore
from vision.domain.errors import InvalidInput
from vision.domain.models import UserProvider
from vision.main import build_app, default_principal_resolver, hosted_default, secret_box
from vision.module import DESCRIPTOR
from vision.settings import Settings

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)


def test_vision_app_settings_defaults() -> None:
    settings = Settings.model_validate({"environment": "test"})
    assert settings.vision_default_enabled is False
    assert settings.vision_default_model == "anthropic/claude-sonnet-5-5"
    assert settings.vision_proposal_ttl_seconds == 300
    assert settings.vision_max_rounds == 3
    assert settings.vision_frame_rate_hz == 5.0
    assert secret_box(settings) is None
    assert hosted_default(settings).enabled is False
    with pytest.raises(InvalidInput):
        hosted_default(make_settings(vision_default_model="nope"))
    with pytest.raises(ValueError, match="SECRETS_KEY"):
        secret_box(make_settings(vision_secrets_key="short"))
    assert hosted_default(make_settings(vision_default_api_key="k")).api_key == "k"
    assert default_principal_resolver({}) is None


def test_vision_app_needs_a_database_without_a_store() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        build_app(make_settings(), authorizer=InMemoryAuthorizer())


async def test_vision_app_startup_descriptor_and_shutdown() -> None:
    bus = InMemoryBus()
    app = build_app(
        make_settings(vision_sweep_seconds=1),
        bus=bus,
        store=MemoryStore(),
        factory=StaticFactory(),
        authorizer=InMemoryAuthorizer(),
        entitlements=FakeEntitlements(),
        warm_litellm=True,
    )
    async with lifespan_client(app) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}
        assert (await client.get("/readyz")).status_code == 200
        descriptor = (await client.get(MODULE_DESCRIPTOR_PATH)).json()
        assert descriptor == DESCRIPTOR.model_dump(mode="json")
        assert {f["key"] for f in descriptor["features"]} == {"ai_recover", "ai_recover_hosted"}
        assert descriptor["events"][0]["type"] == "vision.proposal.created"
        assert descriptor["permissions"] == []
        assert len(app.state.subscriptions) == 7  # incl. vision-auto and the authz cache
        await asyncio.sleep(0.05)  # the sweeper ran once
    assert not bus.is_connected


async def test_vision_app_view_handlers_and_drops() -> None:
    async with harness() as h:
        owner = h.owner()
        rig = await h.rig(owner)
        view = h.service.view
        assert isinstance(view, BusMowerView)
        mapped = view.mower_map(rig.mower_id)
        assert mapped is not None and mapped[0] == owner.account_id
        watch = view.watch(rig.mower_id)
        stop = CommandEnvelope.create(
            mower_id=rig.mower_id,
            account_id=owner.account_id,
            issued_by=ISSUER,
            command=EmergencyStop(),
            ttl=timedelta(seconds=5),
        )
        await h.bus.publish(command_subject(rig.mower_id), stop)
        kinds = set()
        for _ in range(20):
            event = await watch.next(0.5)
            if event is None:
                break
            kinds.add(event.kind)
            if "emergency_stop" in kinds and "result" in kinds:
                break
        assert {"emergency_stop", "result"} <= kinds
        watch.close()
        for i in range(3):
            frame = CommandEnvelope.create(
                mower_id=rig.mower_id,
                account_id=owner.account_id,
                issued_by=ISSUER,
                command=ManualDrive(linear=0, angular=0),
                ttl=timedelta(seconds=1),
                correlation_id="drive:00000000-0000-4000-8000-000000000001",
            )
            await h.bus.publish(command_subject(rig.mower_id), frame)
            assert i >= 0
        assert [k for k, _ in view.recent_commands(rig.mower_id)] == [
            "emergency_stop",
            "manual_drive",
        ]
        before = len(h.bus.dropped)
        other = uuid4()
        await h.bus.publish(telemetry_subject(other), rig.sim.snapshot())
        await h.bus.publish(command_subject(other), stop)
        await h.bus.publish(
            command_result_subject(other),
            CommandResult(command_id=uuid4(), mower_id=rig.mower_id, status=CommandStatus.ACCEPTED),
        )
        await h.bus.publish(device_subject(other, "map"), b"{}")
        await h.bus.publish(telemetry_subject(rig.mower_id), b"{}")
        await h.bus.publish("telemetry.mower.nope", rig.sim.snapshot())
        await h.bus.publish(command_result_subject(rig.mower_id), b"{}")
        # the three bad states are dropped by the view and by the automatic analysis
        assert len(h.bus.dropped) == before + 10
        old = rig.sim.snapshot().model_copy(update={"observed_at": utc_now() - timedelta(hours=1)})
        current = view.state(rig.mower_id)
        assert current is not None
        view.on_state(old)
        assert view.state(rig.mower_id) is not None


async def test_vision_app_deleted_users_lose_their_provider() -> None:
    async with harness() as h:
        user_id, account_id = uuid4(), uuid4()
        now = utc_now()
        await h.store.put_provider(
            UserProvider(user_id, account_id, "ollama/llava", True, now, now, header_names=[])
        )
        payload: dict[str, Any] = UserChanged(
            user_id=user_id, account_id=account_id, username="gone"
        ).model_dump(mode="json")
        await publish_event(
            h.bus,
            EventEnvelope[dict[str, Any]](
                type=IDENTITY_USER_DELETED,
                producer="identity",
                account_id=account_id,
                payload=payload,
            ),
        )
        await wait_for(lambda: not h.store.providers)
        await publish_event(
            h.bus,
            EventEnvelope[dict[str, Any]](
                type="identity.user.created", producer="identity", payload=payload
            ),
        )
        before = len(h.bus.dropped)
        await publish_event(
            h.bus,
            EventEnvelope[dict[str, Any]](
                type=IDENTITY_USER_DELETED, producer="identity", payload={"x": 1}
            ),
        )
        assert len(h.bus.dropped) == before + 1


@pytest.mark.timeout(30)
async def test_vision_idle_returns_while_a_finished_task_waits_for_its_done_callback() -> None:
    """Regression: ``idle`` spun forever (and blocked the event loop) when it ran between the end
    of a background task and the done callback that removes it from the tracked set."""
    async with harness() as h:

        async def quick() -> None:
            return None

        h.service.spawn(quick(), "vision-test-quick")
        [task] = [t for t in asyncio.all_tasks() if t.get_name() == "vision-test-quick"]
        # The task's only step runs in this turn and queues its done callback behind us.
        await asyncio.sleep(0)
        assert task.done(), "precondition: the task finished"
        await h.service.idle()
