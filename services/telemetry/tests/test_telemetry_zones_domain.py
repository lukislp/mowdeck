"""Virtual zones, the pure part (ADR 0070): ring validation, the point test, the hysteresis of
the current zone and the time per zone and day."""

import math
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import GeoReference
from mowdeck_control.geo import to_wgs84

from telemetry.domain.errors import InvalidZoneError
from telemetry.domain.zones import (
    MARGIN_PLAIN_M,
    MARGIN_RTK_FIXED_M,
    LatLon,
    Observation,
    Presence,
    VirtualZone,
    ZoneShape,
    advance,
    choose_zone,
    clean_name,
    distance_m,
    margin_for,
    normalize_ring,
    reevaluate,
    self_intersects,
    shape_of,
    split_by_day,
)

ORIGIN = GeoReference(origin_lat=52.52045, origin_lon=13.40495)
T0 = datetime(2026, 10, 30, 10, 0, tzinfo=UTC)
MOWER, ACCOUNT = uuid4(), uuid4()


def geo(x: float, y: float) -> LatLon:
    """A point ``x`` m east and ``y`` m north of the origin."""
    return to_wgs84(ORIGIN, x, y)


def rect(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    return [list(geo(x0, y0)), list(geo(x1, y0)), list(geo(x1, y1)), list(geo(x0, y1))]


def zone(ring: list[list[float]], name: str = "Lawn") -> VirtualZone:
    points, area = normalize_ring(ring)
    return VirtualZone(
        id=uuid4(),
        account_id=ACCOUNT,
        mower_id=MOWER,
        name=name,
        ring=points,
        area_m2=area,
        created_at=T0,
        updated_at=T0,
    )


def at(x: float, y: float) -> tuple[float, float]:
    return geo(x, y)


# --- validation -------------------------------------------------------------------------------


def test_a_rectangle_is_valid_with_its_area_open_or_closed() -> None:
    ring = rect(0, 0, 10, 8)
    points, area = normalize_ring(ring)
    assert len(points) == 4
    assert area == pytest.approx(80.0, rel=1e-4)
    closed, closed_area = normalize_ring([*ring, ring[0]])
    assert closed == points and closed_area == area


def test_double_taps_collapse_into_one_corner() -> None:
    ring = rect(0, 0, 10, 8)
    twice = [ring[0], ring[1], [ring[1][0] + 1e-8, ring[1][1]], ring[2], ring[3]]
    points, _ = normalize_ring(twice)
    assert len(points) == 4


@pytest.mark.parametrize(
    ("ring", "reason"),
    [
        (rect(0, 0, 10, 8)[:2], "at least 3"),
        ([list(geo(0, 0)), list(geo(10, 0)), list(geo(20, 0))], "at least"),
        # a bow tie: the edges cross
        ([list(geo(0, 0)), list(geo(10, 10)), list(geo(10, 0)), list(geo(0, 10))], "cross"),
        (rect(0, 0, 0.5, 0.5), "at least 1"),
        (rect(0, 0, 900, 900), "at most 100000"),
        (rect(0, 0, 1200, 5), "wide"),
        ([[91.0, 13.0], [52.0, 13.0], [52.0, 13.1]], "latitude"),
        ([[52.0, 13.0], [52.0, math.nan], [52.1, 13.1]], "number"),
        ([[52.0, 13.0, 1.0], [52.0, 13.1], [52.1, 13.1]], "pair"),
    ],
)
def test_invalid_rings_are_refused_with_a_reason(ring: list[list[float]], reason: str) -> None:
    with pytest.raises(InvalidZoneError) as raised:
        normalize_ring(ring)
    assert raised.value.field == "ring"
    assert reason in raised.value.message


def test_too_many_corners_are_refused() -> None:
    circle = [
        list(geo(20 * math.cos(a / 70 * 6.283), 20 * math.sin(a / 70 * 6.283))) for a in range(70)
    ]
    with pytest.raises(InvalidZoneError, match="at most 64 corners"):
        normalize_ring(circle)
    sixty_four = [
        list(geo(20 * math.cos(a / 64 * 6.283), 20 * math.sin(a / 64 * 6.283))) for a in range(64)
    ]
    assert len(normalize_ring(sixty_four)[0]) == 64


def test_self_intersection_detects_touching_and_crossing_edges() -> None:
    square = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    assert not self_intersects(square)
    assert self_intersects([(0.0, 0.0), (10.0, 10.0), (10.0, 0.0), (0.0, 10.0)])
    # a corner that touches the opposite edge (a pinched ring)
    assert self_intersects([(0.0, 0.0), (10.0, 0.0), (5.0, 0.0), (5.0, 10.0)])


def test_names_are_trimmed_and_limited() -> None:
    assert clean_name("  Front   lawn ") == "Front lawn"
    with pytest.raises(InvalidZoneError):
        clean_name("   ")
    with pytest.raises(InvalidZoneError):
        clean_name("x" * 61)


def test_distance_is_metres() -> None:
    assert distance_m(geo(0, 0), geo(30, 40)) == pytest.approx(50.0, abs=1e-3)


# --- the point test and the hysteresis ---------------------------------------------------------


def test_locate_inside_outside_and_far() -> None:
    shape = shape_of(zone(rect(0, 0, 10, 10)))
    inside, edge = shape.locate(*at(5, 5))
    assert inside and edge == pytest.approx(5.0, abs=1e-3)
    outside, edge = shape.locate(*at(12, 5))
    assert not outside and edge == pytest.approx(2.0, abs=1e-3)
    far, edge = shape.locate(*at(5000, 5))
    assert not far and edge > 1000


def test_margin_follows_the_fix_quality() -> None:
    assert margin_for("fixed", "rtk") == MARGIN_RTK_FIXED_M
    assert margin_for("float", "rtk") == 1.0
    assert margin_for("none", "gnss") == MARGIN_PLAIN_M
    assert margin_for("unknown", "unknown") == MARGIN_PLAIN_M
    assert margin_for("none", "dock") == 0.5


def shapes(*zones: VirtualZone) -> list[ZoneShape]:
    return [shape_of(z) for z in zones]


def test_a_mower_on_a_shared_border_does_not_flap() -> None:
    left, right = zone(rect(0, 0, 10, 10), "Left"), zone(rect(10, 0, 20, 10), "Right")
    both = shapes(left, right)
    margin = 1.0
    assert choose_zone(None, both, *at(5, 5), margin) == left.id
    # scatter across the border, less than the margin: stays left
    for x in (9.6, 10.4, 9.9, 10.9, 10.2):
        assert choose_zone(left.id, both, *at(x, 5), margin) == left.id
    # clearly in the right zone
    assert choose_zone(left.id, both, *at(11.5, 5), margin) == right.id
    # and the same band the other way
    for x in (10.4, 9.6, 9.2):
        assert choose_zone(right.id, both, *at(x, 5), margin) == right.id
    assert choose_zone(right.id, both, *at(8.5, 5), margin) == left.id


def test_leaving_needs_the_margin_too() -> None:
    lawn = zone(rect(0, 0, 10, 10))
    one = shapes(lawn)
    assert choose_zone(lawn.id, one, *at(10.5, 5), 1.0) == lawn.id
    assert choose_zone(lawn.id, one, *at(11.5, 5), 1.0) is None
    # entering from outside: inside at all is enough when no zone holds the mower
    assert choose_zone(None, one, *at(9.8, 5), 1.0) == lawn.id


def test_a_nested_zone_wins_when_deep_inside_and_hands_back_with_the_margin() -> None:
    lawn, bed = zone(rect(0, 0, 30, 30), "Lawn"), zone(rect(10, 10, 16, 16), "Bed")
    both = shapes(lawn, bed)
    assert choose_zone(None, both, *at(13, 13), 1.0) == bed.id
    assert choose_zone(lawn.id, both, *at(10.4, 13), 1.0) == lawn.id  # just inside the bed
    assert choose_zone(lawn.id, both, *at(12, 13), 1.0) == bed.id
    assert choose_zone(bed.id, both, *at(9.6, 13), 1.0) == bed.id  # just outside the bed
    assert choose_zone(bed.id, both, *at(8.5, 13), 1.0) == lawn.id


def test_no_zones_means_no_zone() -> None:
    assert choose_zone(None, [], *at(0, 0), 1.0) is None


# --- time per zone ----------------------------------------------------------------------------


def test_split_by_day_cuts_at_utc_midnight() -> None:
    start = datetime(2026, 10, 30, 23, 59, 30, tzinfo=UTC)
    assert split_by_day(start, start + timedelta(seconds=90)) == [
        (date(2026, 10, 30), 30.0),
        (date(2026, 10, 31), 60.0),
    ]
    assert split_by_day(start, start) == []


def seen(
    t: datetime,
    xy: tuple[float, float] | None,
    *,
    mowing: bool = False,
    connected: bool = True,
    beat: datetime | None = None,
    fix: str = "fixed",
) -> Observation:
    lat, lon = at(*xy) if xy else (None, None)
    return Observation(
        observed_at=t,
        seen_at=beat or t,
        connected=connected,
        mowing=mowing,
        lat=lat,
        lon=lon,
        rtk_fix=fix,
        position_source="rtk" if fix != "none" else "gnss",
    )


def run(observations: list[Observation], zones: list[ZoneShape]) -> tuple[Presence | None, list]:  # type: ignore[type-arg]
    presence: Presence | None = None
    times = []
    for o in observations:
        step = advance(
            presence,
            o,
            zones,
            mower_id=MOWER,
            account_id=ACCOUNT,
            max_gap=timedelta(minutes=10),
        )
        if step is None:
            continue
        presence = step.presence
        times.extend(step.times)
    return presence, times


def totals(times: list, zone_id: UUID) -> tuple[float, float, int]:  # type: ignore[type-arg]
    mine = [t for t in times if t.zone_id == zone_id]
    return (
        sum(t.seconds for t in mine),
        sum(t.mowing_seconds for t in mine),
        sum(t.entries for t in mine),
    )


def test_time_counts_for_the_zone_the_mower_was_in() -> None:
    lawn = zone(rect(0, 0, 10, 10))
    s = shapes(lawn)
    presence, times = run(
        [
            seen(T0, (5, 5), mowing=True),
            seen(T0 + timedelta(seconds=30), (6, 5), mowing=True),
            seen(T0 + timedelta(seconds=60), (20, 5)),  # left: the 30 s before still count
            seen(T0 + timedelta(seconds=90), (25, 5)),
        ],
        s,
    )
    assert presence is not None and presence.zone_id is None
    assert totals(times, lawn.id) == (60.0, 60.0, 1)
    assert presence.since == T0 + timedelta(seconds=60)


def test_heartbeats_count_time_without_moving_the_zone() -> None:
    lawn = zone(rect(0, 0, 10, 10))
    presence, times = run(
        [
            seen(T0, (5, 5)),
            seen(T0, (5, 5), beat=T0 + timedelta(seconds=10)),
            seen(T0, (5, 5), beat=T0 + timedelta(seconds=20)),
        ],
        shapes(lawn),
    )
    assert presence is not None and presence.zone_id == lawn.id
    assert totals(times, lawn.id) == (20.0, 0.0, 1)


def test_long_silences_and_link_losses_count_nothing() -> None:
    lawn = zone(rect(0, 0, 10, 10))
    _, times = run(
        [
            seen(T0, (5, 5)),
            seen(T0 + timedelta(minutes=30), (5, 5)),  # silent for 30 minutes
            seen(T0 + timedelta(minutes=31), (5, 5), connected=False),  # the link went down
            seen(T0 + timedelta(minutes=40), (5, 5)),  # back: nothing across the loss
            seen(T0 + timedelta(minutes=41), (5, 5)),
        ],
        shapes(lawn),
    )
    assert totals(times, lawn.id) == (60.0, 0.0, 1)


def test_older_reports_are_ignored() -> None:
    lawn = zone(rect(0, 0, 10, 10))
    s = shapes(lawn)
    first = advance(
        None, seen(T0, (5, 5)), s, mower_id=MOWER, account_id=ACCOUNT, max_gap=timedelta(hours=1)
    )
    assert first is not None
    older = advance(
        first.presence,
        seen(T0 - timedelta(seconds=5), (50, 50)),
        s,
        mower_id=MOWER,
        account_id=ACCOUNT,
        max_gap=timedelta(hours=1),
    )
    assert older is None


def test_a_deleted_zone_is_left_and_a_plain_fix_uses_the_wide_band() -> None:
    lawn = zone(rect(0, 0, 10, 10))
    presence, _ = run([seen(T0, (5, 5))], shapes(lawn))
    assert presence is not None and presence.zone_id == lawn.id
    # plain GNSS 2 m outside: within the 3 m band, still in the lawn
    step = advance(
        presence,
        seen(T0 + timedelta(seconds=5), (12, 5), fix="none"),
        shapes(lawn),
        mower_id=MOWER,
        account_id=ACCOUNT,
        max_gap=timedelta(minutes=10),
    )
    assert step is not None and step.presence.zone_id == lawn.id and not step.changed
    gone = advance(
        step.presence,
        seen(T0 + timedelta(seconds=10), (5, 5)),
        [],
        mower_id=MOWER,
        account_id=ACCOUNT,
        max_gap=timedelta(minutes=10),
    )
    assert gone is not None and gone.presence.zone_id is None and gone.changed
    assert gone.times == []


def test_reevaluate_after_drawing_uses_the_last_position() -> None:
    lawn = zone(rect(0, 0, 10, 10))
    outside = Presence(
        mower_id=MOWER,
        account_id=ACCOUNT,
        zone_id=None,
        since=T0,
        observed_at=T0,
        seen_at=T0,
        lat=at(5, 5)[0],
        lon=at(5, 5)[1],
    )
    now = T0 + timedelta(minutes=5)
    inside = reevaluate(outside, shapes(lawn), now=now)
    assert inside is not None and inside.zone_id == lawn.id and inside.since == now
    assert reevaluate(inside, shapes(lawn), now=now + timedelta(minutes=1)) is inside
    assert reevaluate(None, shapes(lawn), now=now) is None
