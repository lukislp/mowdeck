"""The snapshot directory fed from ``device.mower.>``."""

from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import MowerMapSnapshot
from mowdeck_contracts.v1.subjects import device_subject
from mowdeck_control import default_map
from mowdeck_service_kit.bus import BusMessage, DropMessage
from planner_support import LUBA_LIKE, T0, harness

from planner.adapters.bus.subscriptions import snapshot_handler
from planner.adapters.memory import MemoryStore
from planner.application.ingest import Ingest


async def test_planner_keeps_the_newest_map_and_capabilities() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.register(owner)
        await h.publish_map(mower_id, owner.account_id)
        await h.publish_capabilities(mower_id, owner.account_id, LUBA_LIKE)
        facts = h.store.data.facts[mower_id]
        assert facts.map is not None and facts.capabilities is not None
        assert facts.capabilities["custom_paths"] is False
        ingest = Ingest(h.store)
        older = MowerMapSnapshot(
            mower_id=mower_id,
            account_id=owner.account_id,
            observed_at=T0 - timedelta(minutes=1),
            map=default_map().model_copy(update={"areas": []}),
        )
        assert await ingest.on_map(older) is False
        assert len(h.store.data.facts[mower_id].map["areas"]) == 2  # type: ignore[index]


@pytest.mark.parametrize(
    ("subject", "payload", "reason"),
    [
        ("device.mower.nope.map", b"{}", "unexpected subject"),
        (None, b"{}", "invalid map snapshot"),
        ("other", None, "does not match"),
    ],
)
async def test_planner_drops_bad_snapshots(
    subject: str | None, payload: bytes | None, reason: str
) -> None:
    mower_id = uuid4()
    handler = snapshot_handler(Ingest(MemoryStore()))
    if payload is None:
        snapshot = MowerMapSnapshot(
            mower_id=uuid4(), account_id=uuid4(), observed_at=T0, map=default_map()
        )
        payload = snapshot.model_dump_json().encode()
        subject = device_subject(mower_id, "map")
    message = BusMessage(subject=subject or device_subject(mower_id, "map"), data=payload)
    with pytest.raises(DropMessage, match=reason):
        await handler(message)


async def test_planner_ignores_the_mow_path_snapshot() -> None:
    store = MemoryStore()
    handler = snapshot_handler(Ingest(store))
    mower_id = uuid4()
    await handler(BusMessage(subject=device_subject(mower_id, "path"), data=b"{}"))
    assert mower_id not in store.data.facts
