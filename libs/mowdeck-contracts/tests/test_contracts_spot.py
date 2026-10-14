"""Spot plans in StartMow and the position track request."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    PositionTrackReply,
    PositionTrackRequest,
    PrincipalKind,
    PrincipalRef,
    SpotMowPlan,
    SpotNativeJob,
    SpotSegment,
    StartMow,
    decode_polyline,
    encode_polyline,
    spot_plan_hash,
)
from mowdeck_contracts.v1.positions import TRACK_PRECISION
from mowdeck_contracts.v1.subjects import TELEMETRY_POSITIONS_QUERY, stream_for_subject
from pydantic import ValidationError


def _plan() -> SpotMowPlan:
    return SpotMowPlan.build(
        [
            SpotSegment.from_points(
                [(-1, 1), (0.5, 0.5)], blades_on=False, speed_mps=0.6, kind="transfer"
            ),
            SpotSegment.from_points(
                [(0.5, 0.5), (11.5, 0.5), (11.5, 0.85)], blades_on=True, speed_mps=0.4
            ),
        ],
        ["stripes"],
        estimated_duration_s=123.45,
        covered_area_m2=10.123,
    )


@pytest.mark.parametrize(
    "points",
    [
        [(0.0, 0.0), (1.0, 2.0)],
        [(-12.34, 56.78), (-12.34, 56.79), (1000.0, -999.99)],
        [(0.004, 0.006), (0.0, 0.0)],
    ],
)
def test_polyline_round_trips_at_centimetre_precision(points: list[tuple[float, float]]) -> None:
    decoded = decode_polyline(encode_polyline(points))
    assert decoded == [(round(x, 2), round(y, 2)) for x, y in points]


def test_polyline_round_trips_wgs84() -> None:
    points = [(48.1371539, 11.5761249), (48.1371601, 11.5761302)]
    assert decode_polyline(encode_polyline(points, TRACK_PRECISION), TRACK_PRECISION) == points


@pytest.mark.parametrize("bad", ["\x01", "_", "~~~~~~~~~~~~~~~~"])
def test_malformed_polylines_are_refused(bad: str) -> None:
    with pytest.raises(ValueError, match="polyline"):
        decode_polyline(bad)


def test_polyline_needs_finite_coordinates() -> None:
    with pytest.raises(ValueError, match="finite"):
        encode_polyline([(float("inf"), 0.0)])


def test_spot_plan_hash_matches_the_content() -> None:
    plan = _plan()
    assert plan.plan_hash == spot_plan_hash(plan.pattern_chain, plan.segments)
    assert plan.estimated_duration_s == 123.5
    assert plan.covered_area_m2 == 10.12
    assert plan.point_count == 5
    assert plan.segments[1].points()[-1] == (11.5, 0.85)
    tampered = plan.model_dump(mode="json")
    tampered["segments"][1]["blades_on"] = False
    with pytest.raises(ValidationError, match="plan_hash"):
        SpotMowPlan.model_validate(tampered)


def test_transfers_must_have_the_blades_off() -> None:
    with pytest.raises(ValidationError, match="blades off"):
        SpotSegment.from_points([(0, 0), (1, 1)], blades_on=True, speed_mps=0.5, kind="transfer")
    with pytest.raises(ValidationError, match="two points"):
        SpotSegment(blades_on=True, speed_mps=0.5, path=encode_polyline([(1000, 1000)]))


def test_start_mow_takes_exactly_one_target() -> None:
    plan = _plan()
    command = StartMow(spot_plan=plan)
    assert command.area_ids == []
    with pytest.raises(ValidationError, match="exactly one"):
        StartMow(area_ids=["front"], spot_plan=plan)
    with pytest.raises(ValidationError, match="exactly one"):
        StartMow(plan_ref="x", spot_plan=plan)
    native = StartMow(
        spot_plan=SpotMowPlan.build(
            plan.segments,
            plan.pattern_chain,
            native=SpotNativeJob(area_ids=["front"], pattern={"name": "stripes"}),
        )
    )
    assert native.spot_plan is not None and native.spot_plan.native is not None


def test_spot_plan_survives_an_envelope_round_trip() -> None:
    envelope = CommandEnvelope.create(
        mower_id=uuid4(),
        account_id=uuid4(),
        issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        command=StartMow(spot_plan=_plan()),
    )
    again = CommandEnvelope.model_validate_json(envelope.model_dump_json())
    assert isinstance(again.command, StartMow)
    assert again.command.spot_plan == envelope.command.spot_plan  # type: ignore[union-attr]


def test_position_track_request_and_reply() -> None:
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    request = PositionTrackRequest(
        account_id=uuid4(), mower_id=uuid4(), start=now - timedelta(hours=1), end=now
    )
    assert request.limit == 20_000
    with pytest.raises(ValidationError, match="before end"):
        PositionTrackRequest(account_id=uuid4(), mower_id=uuid4(), start=now, end=now)
    points = [(48.1, 11.5), (48.1000001, 11.5000002)]
    reply = PositionTrackReply(
        mower_id=request.mower_id, count=2, track=encode_polyline(points, TRACK_PRECISION)
    )
    assert reply.points() == points
    assert PositionTrackReply(mower_id=request.mower_id, error="nope").points() == []


def test_track_query_subject_is_not_captured_by_a_stream() -> None:
    assert stream_for_subject(TELEMETRY_POSITIONS_QUERY) is None
