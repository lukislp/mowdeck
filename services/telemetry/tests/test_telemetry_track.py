"""``tracks.positions.query``: the position track for the Planner's coverage."""

from uuid import uuid4

from mowdeck_contracts.v1 import MowerActivity, PositionTrackReply, PositionTrackRequest
from mowdeck_contracts.v1.subjects import TELEMETRY_POSITIONS_QUERY
from telemetry_support import T0, harness, make_state, seconds


async def test_telemetry_track_query_returns_the_encoded_track() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        for i in range(5):
            await h.publish_state(
                make_state(
                    mower_id,
                    owner.account_id,
                    at=T0 + seconds(i),
                    activity=MowerActivity.MOWING,
                    position=(48.1 + i / 100_000, 11.5),
                )
            )
        request = PositionTrackRequest(
            account_id=owner.account_id,
            mower_id=mower_id,
            start=T0 - seconds(1),
            end=T0 + seconds(60),
            limit=3,
        )
        message = await h.bus.request(TELEMETRY_POSITIONS_QUERY, request)
        reply = message.model(PositionTrackReply)
        foreign = await h.bus.request(
            TELEMETRY_POSITIONS_QUERY,
            request.model_copy(update={"account_id": uuid4(), "limit": 100}),
        )
        too_long = await h.bus.request(
            TELEMETRY_POSITIONS_QUERY,
            request.model_copy(update={"start": T0 - seconds(400 * 86_400)}),
        )
    assert reply.error is None
    assert reply.count == 3 and reply.truncated
    assert reply.points() == [(48.10002, 11.5), (48.10003, 11.5), (48.10004, 11.5)]
    assert reply.first_at == T0 + seconds(2) and reply.last_at == T0 + seconds(4)
    assert foreign.model(PositionTrackReply).count == 0
    assert "at most" in (too_long.model(PositionTrackReply).error or "")


async def test_telemetry_track_query_drops_malformed_requests() -> None:
    async with harness() as h:
        try:
            await h.bus.request(TELEMETRY_POSITIONS_QUERY, b"{}", timeout=0.2)
        except Exception as exc:  # the request is dropped, so it times out
            assert "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()
        else:  # pragma: no cover - a reply would be a bug
            raise AssertionError("malformed request was answered")
