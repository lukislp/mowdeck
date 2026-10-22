"""Mower, principal, module descriptor and problem models."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    ConnectionKind,
    GeoPosition,
    ManualDrive,
    ModuleDescriptor,
    MowerActivity,
    MowerState,
    PositionSource,
    Principal,
    PrincipalKind,
    ProblemDetails,
    ProblemFieldError,
    RtkFixStatus,
    SetBladeHeight,
    SetLight,
    SetSpeed,
    StartMow,
)
from pydantic import ValidationError


@pytest.fixture
def principal() -> Principal:
    return Principal(
        subject=uuid4(),
        account_id=uuid4(),
        kind=PrincipalKind.USER,
        display_name="Anna",
        roles=["operator"],
        permissions=["mower.view", "mower.control"],
        plan="unlimited",
        token_id=uuid4(),
    )


def _state(**overrides: Any) -> MowerState:
    data: dict[str, Any] = {
        "mower_id": uuid4(),
        "account_id": uuid4(),
        "observed_at": datetime(2026, 10, 8, 12, 0, tzinfo=UTC),
        "activity": "mowing",
        "battery_percent": 80,
        "connection": "simulated",
        **overrides,
    }
    return MowerState.model_validate(data)


def test_mower_state_defaults_and_round_trip() -> None:
    state = _state(position={"lat": 48.1, "lon": 11.5, "heading": 359.9}, raw={"x": 1})
    assert state.activity is MowerActivity.MOWING
    assert state.connection is ConnectionKind.SIMULATED
    assert state.rtk_fix is RtkFixStatus.UNKNOWN
    assert state.position_source is PositionSource.UNKNOWN
    assert state.charging is False
    assert state.light_on is None
    assert MowerState.model_validate_json(state.model_dump_json()) == state
    lit = _state(light_on=True)
    assert MowerState.model_validate_json(lit.model_dump_json()).light_on is True


@pytest.mark.parametrize("battery", [-1, 101])
def test_battery_range(battery: int) -> None:
    with pytest.raises(ValidationError):
        _state(battery_percent=battery)


@pytest.mark.parametrize(
    "position",
    [{"lat": 91, "lon": 0}, {"lat": 0, "lon": -181}, {"lat": 0, "lon": 0, "heading": 360}],
)
def test_position_ranges(position: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        GeoPosition.model_validate(position)


@pytest.mark.parametrize("source", ["rtk", "gnss", "dock", "unknown"])
def test_position_source_values(source: str) -> None:
    state = _state(position={"lat": 48.1, "lon": 11.5}, position_source=source)
    assert state.position_source is PositionSource(source)
    assert MowerState.model_validate_json(state.model_dump_json()) == state


@pytest.mark.parametrize("source", ["rtk", "gnss", "dock"])
def test_position_source_needs_a_position(source: str) -> None:
    with pytest.raises(ValidationError, match="position_source must be unknown"):
        _state(position_source=source)
    assert _state(position_source="unknown").position is None


def test_unknown_position_source_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _state(position={"lat": 48.1, "lon": 11.5}, position_source="vision")


def test_unknown_activity_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _state(activity="dancing")


@pytest.mark.parametrize(
    "data",
    [{}, {"area_ids": ["a"], "plan_ref": "p"}, {"area_ids": []}],
)
def test_start_mow_needs_exactly_one_target(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        StartMow.model_validate(data)


def test_start_mow_targets() -> None:
    assert StartMow(area_ids=["a1", "a2"]).plan_ref is None
    assert StartMow(plan_ref="plan-7", pattern={"angle": 45}).area_ids == []


@pytest.mark.parametrize("ttl", [99, 2001])
def test_manual_drive_ttl_range(ttl: int) -> None:
    with pytest.raises(ValidationError):
        ManualDrive(linear=0, angular=0, ttl_ms=ttl)


@pytest.mark.parametrize(("linear", "angular"), [(1.01, 0), (0, -1.01)])
def test_manual_drive_axis_range(linear: float, angular: float) -> None:
    with pytest.raises(ValidationError):
        ManualDrive(linear=linear, angular=angular)


def test_manual_drive_defaults_to_one_second_deadman() -> None:
    sample = ManualDrive(linear=1, angular=-1)
    assert sample.ttl_ms == 1000
    assert sample.blade_on is False
    assert ManualDrive(linear=0, angular=0, ttl_ms=100).ttl_ms == 100
    assert ManualDrive(linear=0, angular=0, ttl_ms=2000).ttl_ms == 2000


def test_blade_height_and_speed_ranges() -> None:
    assert SetBladeHeight(mm=50).mm == 50
    assert SetSpeed(mps=0.8).mps == 0.8
    with pytest.raises(ValidationError):
        SetBladeHeight(mm=5)
    with pytest.raises(ValidationError):
        SetSpeed(mps=0)


def test_set_light_needs_a_boolean() -> None:
    assert SetLight(on=True).model_dump() == {"kind": "set_light", "on": True}
    assert SetLight(on=False).on is False
    with pytest.raises(ValidationError):
        SetLight.model_validate({"kind": "set_light"})
    with pytest.raises(ValidationError):
        SetLight.model_validate({"kind": "set_light", "on": "maybe"})


def test_principal_helpers(principal: Principal) -> None:
    assert principal.has_permission("mower.control")
    assert not principal.has_permission("users.manage")
    ref = principal.ref()
    assert ref.subject == principal.subject
    assert ref.kind is PrincipalKind.USER
    assert Principal.model_validate_json(principal.model_dump_json()) == principal


def test_service_principal_without_account() -> None:
    service = Principal(subject=uuid4(), kind=PrincipalKind.SERVICE)
    assert service.account_id is None
    assert service.permissions == []


def _descriptor(**overrides: Any) -> dict[str, Any]:
    return {
        "name": "reports",
        "version": "1.2.0",
        "permissions": [{"key": "report.view", "description": "View reports"}],
        "features": [{"key": "reports", "description": "Reports"}],
        "events": [{"type": "report.created", "schema_ref": "schemas/v1/ReportCreated.json"}],
        "ui_areas": [
            {"route": "/reports", "menu_label": "Reports", "required_permission": "report.view"}
        ],
        **overrides,
    }


def test_module_descriptor_valid() -> None:
    descriptor = ModuleDescriptor.model_validate(_descriptor())
    assert descriptor.events[0].version == 1
    assert ModuleDescriptor.model_validate_json(descriptor.model_dump_json()) == descriptor


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "Reports"},
        {"version": "1.2"},
        {"permissions": [{"key": "report", "description": "x"}]},
        {"ui_areas": [{"route": "reports", "menu_label": "R", "required_permission": "r.v"}]},
        {
            "permissions": [
                {"key": "report.view", "description": "a"},
                {"key": "report.view", "description": "b"},
            ]
        },
    ],
)
def test_module_descriptor_invalid(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModuleDescriptor.model_validate(_descriptor(**overrides))


def test_problem_details() -> None:
    problem = ProblemDetails(
        title="Not found",
        status=404,
        correlation_id="abc",
        errors=[ProblemFieldError(detail="missing", pointer="#/name")],
    )
    assert problem.type == "about:blank"
    dumped = problem.model_dump(mode="json", exclude_none=True)
    assert dumped["errors"] == [{"detail": "missing", "pointer": "#/name"}]
    with pytest.raises(ValidationError):
        ProblemDetails(title="x", status=99)


def test_mower_state_links_firmware_and_spot_run_default_to_nothing() -> None:
    from mowdeck_contracts.v1 import FirmwareInfo, LinkStatus, SpotRunStatus

    state = _state()
    assert state.links == {} and state.firmware_info is None and state.spot_run is None
    full = _state(
        activity="updating",
        links={"ble_bridge": {"connected": True, "rssi": -60}},
        firmware_info={"installed": "1.11.3", "available": "1.12.0", "update_state": "installing"},
        spot_run={"run_id": str(uuid4()), "state": "spot_mowing", "progress_percent": 40},
    )
    assert full.activity is MowerActivity.UPDATING
    assert full.links["ble_bridge"] == LinkStatus(connected=True, rssi=-60)
    assert isinstance(full.firmware_info, FirmwareInfo)
    assert isinstance(full.spot_run, SpotRunStatus) and not full.spot_run.final
    assert MowerState.model_validate_json(full.model_dump_json()) == full
    with pytest.raises(ValidationError):
        _state(spot_run={"run_id": str(uuid4()), "state": "dancing"})


def test_mower_state_link_fields_default_to_a_state_from_before_them() -> None:
    old = _state()
    assert old.heartbeat_at is None  # clients fall back to observed_at
    assert old.connected is True
    beat = datetime(2026, 10, 8, 12, 5, tzinfo=UTC)
    live = _state(heartbeat_at="2026-10-08T14:05:00+02:00", connected=False)
    assert live.heartbeat_at == beat  # normalised to UTC
    assert MowerState.model_validate_json(live.model_dump_json()) == live
    with pytest.raises(ValidationError):
        _state(heartbeat_at="2026-10-08T12:05:00")  # naive times are rejected


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"])
def test_a_timestamp_outside_the_utc_range_is_a_validation_error(stamp: str) -> None:
    # Found by fuzzing (fuzz/fuzz_contracts.py): the UTC conversion raised OverflowError, which
    # pydantic passes through, so the message crashed the consumer instead of being rejected.
    from mowdeck_contracts.v1 import MowPath

    with pytest.raises(ValidationError, match="out of range"):
        MowPath.model_validate_json(
            '{"path_hash":"h","points_total":0,"planned_at":"' + stamp + '"}'
        )


def test_mower_state_typed_job_gnss_signal_and_self_check_default_to_null() -> None:
    old = _state()
    assert old.job is None and old.gnss is None and old.signal is None
    assert old.self_check is None and old.error_category is None


def test_mower_state_typed_job_gnss_signal_and_self_check_round_trip() -> None:
    from mowdeck_contracts.v1 import (
        GnssSolution,
        JobProgress,
        MowerErrorCategory,
        NetworkKind,
        SelfCheckResult,
    )

    state = _state(
        activity="error",
        job={
            "progress_percent": 42,
            "area_id": "1234567890",
            "elapsed_s": 1260,
            "remaining_s": 1800,
            "mowed_m2": 310.5,
        },
        gnss={"satellites": 31, "correction_age_s": 1, "rtk_status": "fixed"},
        signal={"wifi_dbm": -61, "mobile_dbm": -88, "network": "mobile"},
        self_check={"result": "blocked", "detail": "rain"},
        error_category="trapped",
    )
    assert state.job == JobProgress(
        progress_percent=42, area_id="1234567890", elapsed_s=1260, remaining_s=1800, mowed_m2=310.5
    )
    assert state.gnss is not None and state.gnss.rtk_status is GnssSolution.FIXED
    assert state.gnss.correction_age_s == 1.0
    assert state.signal is not None and state.signal.network is NetworkKind.MOBILE
    assert state.self_check is not None and state.self_check.result is SelfCheckResult.BLOCKED
    assert state.error_category is MowerErrorCategory.TRAPPED
    assert MowerState.model_validate_json(state.model_dump_json()) == state
    # Every inner field is optional: a partial report validates.
    partial = _state(job={}, gnss={}, signal={"network": "wifi"}, self_check={"result": "ok"})
    assert partial.job == JobProgress() and partial.gnss is not None
    assert partial.gnss.rtk_status is GnssSolution.UNKNOWN


@pytest.mark.parametrize(
    "overrides",
    [
        {"job": {"progress_percent": 101}},
        {"job": {"elapsed_s": -1}},
        {"job": {"mowed_m2": -0.5}},
        {"gnss": {"satellites": -1}},
        {"gnss": {"rtk_status": "rtk"}},
        {"signal": {"wifi_dbm": 0}},
        {"signal": {"mobile_dbm": -151}},
        {"signal": {"network": "satellite"}},
        {"self_check": {"result": "maybe"}},
        {"self_check": {"detail": "rain"}},
        {"error_category": "gremlins"},
    ],
)
def test_mower_state_typed_fields_reject_out_of_range_values(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _state(**overrides)
