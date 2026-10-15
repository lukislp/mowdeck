"""The owner script runs end to end on the fake port (``--dry-run``)."""

import importlib.util
import itertools
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from mowdeck_contracts.v1 import (
    CommandStatus,
    DriveCalibration,
    DriveCalibrationResult,
    DriveCalibrationStep,
)
from mowdeck_control import default_map

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "try_real_mower.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("try_real_mower", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("args", "command", "sent"),
    [
        (["--start-area", "Back lawn", "--pattern", "checkerboard"], "start_mow", "start_job"),
        (["--dock"], "return_to_dock", "return_to_dock"),
        (["--estop"], "emergency_stop", "pause_execute_task"),
        (["--light", "on"], "set_light", "set_car_manual_light"),
        ([], None, None),
    ],
)
def test_mammotion_owner_script_dry_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    command: str | None,
    sent: str | None,
) -> None:
    log = tmp_path / "log.jsonl"
    assert _script().main(["--dry-run", "--log", str(log), *args]) == 0
    out = capsys.readouterr().out
    assert "SAFETY" in out and "Back lawn" in out
    steps = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    names = [s["step"] for s in steps]
    assert names[:5] == ["start", "connected", "capabilities", "state", "map"]
    assert steps[3]["rtk_source"] == "network"
    if command is not None and sent is not None:
        (record,) = [s for s in steps if s["step"] == "command"]
        assert record["command"] == command
        assert sent in out
    else:
        assert "command" not in names


def test_mammotion_owner_script_needs_credentials_or_a_valid_area(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _script()
    for name in ("MAMMOTION_ACCOUNT", "MAMMOTION_PASSWORD", "MAMMOTION_DEVICE_NAME"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="MAMMOTION_ACCOUNT"):
        script.main(["--log", str(tmp_path / "x.jsonl")])
    with pytest.raises(SystemExit, match="no area named"):
        script.main(["--dry-run", "--start-area", "Moon", "--log", str(tmp_path / "y.jsonl")])
    with pytest.raises(SystemExit, match="HOST,PSK,MAC"):
        script.main(["--esphome", "only-host", "--log", str(tmp_path / "z.jsonl")])


def test_mammotion_owner_script_builds_real_connections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import base64

    script = _script()
    monkeypatch.setenv("MAMMOTION_ACCOUNT", "second@example.com")
    monkeypatch.setenv("MAMMOTION_PASSWORD", "from-env")
    monkeypatch.setenv("MAMMOTION_DEVICE_NAME", "Luba-VP1234")
    cloud = script.build_connection(
        script.parse_args(["--session-cache", str(tmp_path / "s.json")])
    )
    assert cloud.kind.value == "cloud"
    assert cloud.options.session_cache_path == tmp_path / "s.json"
    psk = base64.b64encode(bytes(32)).decode()
    ble = script.build_connection(
        script.parse_args(
            ["--esphome", f"bridge.local,{psk},AA:BB:CC:DD:EE:FF", "--device-name", "Luba-VP1"]
        )
    )
    assert ble.kind.value == "ble_bridge"


def test_mammotion_owner_script_camera_prints_a_redacted_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "camera.jsonl"
    assert _script().main(["--dry-run", "--camera", "--log", str(log)]) == 0
    out = capsys.readouterr().out
    assert "dry-run-channel" in out and "Front left (uid 1)" in out
    assert "<redacted>" in out
    for secret in ("dry-run-viewer-token", "dry-run-key-0123456789", "ZHJ5LXJ1bi1zYWx0"):
        assert secret not in out
        assert secret not in log.read_text("utf-8")
    steps = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    (record,) = [s for s in steps if s["step"] == "camera"]
    assert record["provider"] == "agora" and record["encryption"]["mode"] == "aes-256-gcm2"


@pytest.mark.parametrize(
    ("flag", "step"), [("--turn-check", "turn_check"), ("--spot-dry-run", "spot_dry_run")]
)
def test_mammotion_owner_script_spot_checks_need_manual_driving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str, step: str
) -> None:
    """The cloud link of the dry run offers no manual driving: both checks refuse cleanly."""
    log = tmp_path / "log.jsonl"
    assert _script().main(["--dry-run", "--log", str(log), flag]) == 1
    assert "no manual driving" in capsys.readouterr().out
    steps = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    (record,) = [s for s in steps if s["step"] == step]
    assert record["error"] == "no manual drive"


def test_mammotion_owner_script_from_dock_needs_manual_driving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "log.jsonl"
    argv = ["--dry-run", "--log", str(log), "--spot-dry-run", "--from-dock"]
    assert _script().main(argv) == 1
    assert "no manual driving" in capsys.readouterr().out


def test_mammotion_owner_script_from_dock_options() -> None:
    script = _script()
    with pytest.raises(SystemExit):
        script.parse_args(["--from-dock"])  # needs --spot-dry-run
    with pytest.raises(SystemExit):
        script.parse_args(["--spot-dry-run", "--spot-at", "1,2"])  # needs --from-dock
    args = script.parse_args(["--spot-dry-run", "--from-dock", "--spot-at", "6.5,4"])
    garden = default_map()
    assert script.spot_centre(args, garden) == (6.5, 4.0)
    nearest = script.parse_args(["--spot-dry-run", "--from-dock"])
    assert script.spot_centre(nearest, garden) == (6.0, 4.0)  # the front lawn's centre
    back = script.parse_args(["--spot-dry-run", "--from-dock", "--spot-area", "Back lawn"])
    assert script.spot_centre(back, garden) == (5.0, 14.0)


def test_mammotion_owner_script_turn_check_summary() -> None:
    script = _script()
    assert script.heading_delta(10.0, 350.0) == pytest.approx(-20.0)
    assert script.heading_delta(350.0, 10.0) == pytest.approx(20.0)
    # (received, observed, heading): one report per second, the turn shows 1.3 s after the
    # first frame (sent at t = 10).
    left = [(9.0, 100.0, 90.0), (10.5, 101.0, 90.5), (11.3, 102.0, 80.0), (12.4, 103.0, 70.0)]
    summary = script.turn_check_summary(left, 10.0)
    assert summary["verdict"] == "ok" and summary["heading_change_deg"] == -20.0
    assert summary["latency_s"] == 1.3
    assert summary["report_interval_s"] == {"min": 1.0, "median": 1.0, "max": 1.0}
    right = [(9.0, 100.0, 90.0), (11.0, 101.0, 105.0)]
    assert script.turn_check_summary(right, 10.0)["verdict"] == "sign_error"
    still = [(9.0, 100.0, 90.0), (11.0, 101.0, 90.5)]
    assert script.turn_check_summary(still, 10.0)["verdict"] == "no_turn"
    assert script.turn_check_summary([(11.0, 101.0, None)], 10.0)["verdict"] == "no_heading"


def test_mammotion_owner_script_circle_starts_at_the_mower_and_turns_left() -> None:
    script = _script()
    points = script.circle_points(2.0, 3.0, math.pi / 2)  # heading +y
    assert points[0] == pytest.approx((2.0, 3.0))
    assert points[-1] == pytest.approx((2.0, 3.0))
    assert points[1][0] < 2.0 and points[1][1] > 3.0  # ahead and to the left
    center = (1.5, 3.0)
    assert all(math.dist(p, center) == pytest.approx(0.5) for p in points)
    assert all(math.dist(a, b) <= 0.21 for a, b in itertools.pairwise(points))


# --- drive calibration and the speed setting (ADR 0042) ------------------------------------


def _calibration(**changes: Any) -> DriveCalibration:
    values: dict[str, Any] = {
        "full_speed_mps": 0.42,
        "full_yaw_rate_dps": 61.5,
        "dead_zone": 0.15,
        "turn_dead_zone": 0.16,
        "drive_speed_setting": 0.4,
        "calibrated_at": datetime(2026, 10, 15, 9, 30, tzinfo=UTC),
    }
    return DriveCalibration(**(values | changes))


STEPS = [
    DriveCalibrationStep(
        kind="forward",
        command=0.5,
        duration_s=2.5,
        measured=0.171,
        reports=3,
        report_interval_s=1.02,
        distance_m=0.43,
    ),
    DriveCalibrationStep(
        kind="turn", command=-0.6, duration_s=2.5, measured=-27.25, reports=2, turned_deg=-68.1
    ),
    DriveCalibrationStep(kind="forward", command=1.0, duration_s=0.0, skipped=True),
]


def test_mammotion_owner_script_calibration_options() -> None:
    script = _script()
    args = script.parse_args(["--calibrate"])
    assert args.calibrate and args.step_s is None and not args.save
    args = script.parse_args(["--calibrate", "--step-s", "3", "--calibration-out", "c.json"])
    assert args.step_s == 3.0 and args.calibration_out == Path("c.json")
    mower = "00000000-0000-4000-8000-0000000000aa"
    args = script.parse_args(
        ["--calibrate", "--save", "--fleet-url", "http://fleet", "--mower-id", mower]
    )
    assert args.mower_id == UUID(mower) and args.fleet_url == "http://fleet"
    args = script.parse_args(["--spot-dry-run", "--use-calibration", "c.json"])
    assert args.use_calibration == Path("c.json")
    assert script.parse_args(["--set-speed", "0.4"]).set_speed == 0.4
    assert script.parse_args(["--get-speed"]).get_speed
    for bad in (
        ["--step-s", "2"],  # needs --calibrate
        ["--calibrate", "--step-s", "0.5"],  # below 1 s
        ["--calibrate", "--step-s", "6"],  # above the contract's 5 s
        ["--calibration-out", "c.json"],
        ["--save"],  # nothing to save
        ["--calibrate", "--save", "--use-calibration", "c.json"],
        ["--use-calibration", "c.json"],  # neither spot run nor save
        ["--fleet-url", "http://fleet"],
        ["--set-speed", "0"],
        ["--calibrate", "--get-speed"],  # one action at a time
    ):
        with pytest.raises(SystemExit):
            script.parse_args(bad)


def test_mammotion_owner_script_calibration_table() -> None:
    script = _script()
    rows = script.calibration_rows(STEPS)
    assert rows[0] == ["1", "forward", "linear +0.50", "0.171 m/s", "3", "1.02", "0.43 m", "no"]
    assert rows[1] == ["2", "turn", "angular -0.60", "-27.250 deg/s", "2", "-", "-68.1 deg", "no"]
    assert rows[2][3] == "-" and rows[2][7] == "yes"
    lines = script.calibration_table(STEPS).splitlines()
    assert lines[0].split() == [
        "#",
        "kind",
        "command",
        "measured",
        "reports",
        "interval",
        "s",
        "distance/turned",
        "skipped",
    ]
    assert set(lines[1].replace(" ", "")) == {"-"}
    assert len(lines) == 2 + len(STEPS)
    assert lines[3].index("angular") == lines[2].index("linear")  # aligned columns
    fit = script.calibration_fit(_calibration())
    assert fit["full_speed_mps"] == 0.42 and fit["drive_speed_setting_mps"] == 0.4
    assert fit["calibrated_at"] == "2026-10-15T09:30:00+00:00"


def test_mammotion_owner_script_loads_every_calibration_json(tmp_path: Path) -> None:
    script = _script()
    calibration = _calibration()
    written = tmp_path / "out.json"
    written.write_text(json.dumps(script.calibration_file(calibration, STEPS)), "utf-8")
    assert script.load_calibration(written) == calibration
    options = tmp_path / "options.json"
    options.write_text(json.dumps(calibration.to_options()), "utf-8")
    assert script.load_calibration(options) == calibration
    body = tmp_path / "body.json"
    body.write_text(json.dumps(script.calibration_body(calibration)), "utf-8")
    assert script.load_calibration(body) == calibration
    for content in ("not json", '{"drive_full_speed_mps": -1}', '{"full_speed_mps": 0.4}'):
        broken = tmp_path / "broken.json"
        broken.write_text(content, "utf-8")
        with pytest.raises(SystemExit):
            script.load_calibration(broken)
    with pytest.raises(SystemExit, match="cannot read"):
        script.load_calibration(tmp_path / "missing.json")


class _Response:
    status = 200

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_mammotion_owner_script_saves_to_fleet() -> None:
    script = _script()
    calibration = _calibration()
    requests: list[Any] = []

    def opener(request: Any, timeout: float) -> _Response:
        requests.append(request)
        return _Response()

    mower = UUID("00000000-0000-4000-8000-0000000000aa")
    saved, message = script.save_calibration(
        calibration,
        fleet_url="https://fleet.example/api/",
        mower_id=mower,
        token="t0k",  # noqa: S106 - a placeholder
        opener=opener,
    )
    assert saved and "HTTP 200" in message
    (request,) = requests
    assert request.get_method() == "PUT"
    assert request.full_url == f"https://fleet.example/api/mowers/{mower}/drive-calibration"
    assert request.get_header("Authorization") == "Bearer t0k"
    assert json.loads(request.data) == {
        "full_speed_mps": 0.42,
        "full_yaw_rate_dps": 61.5,
        "dead_zone": 0.15,
        "turn_dead_zone": 0.16,
        "drive_speed_setting": 0.4,
        "calibrated_at": "2026-10-15T09:30:00+00:00",
    }
    saved, message = script.save_calibration(
        calibration, fleet_url=None, mower_id=mower, token=None, opener=opener
    )
    assert not saved and "--fleet-url" in message and "MOWDECK_TOKEN" in message
    saved, message = script.save_calibration(
        calibration,
        fleet_url="file:///etc",
        mower_id=mower,
        token="t",  # noqa: S106 - a placeholder
        opener=opener,
    )
    assert not saved and "http or https" in message
    assert len(requests) == 1


def test_mammotion_owner_script_save_without_fleet_prints_the_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MOWDECK_TOKEN", raising=False)
    script = _script()
    stored = tmp_path / "c.json"
    stored.write_text(json.dumps(_calibration().to_options()), "utf-8")
    argv = ["--save", "--use-calibration", str(stored), "--log", str(tmp_path / "l.jsonl")]
    assert script.main(argv) == 1
    out = capsys.readouterr().out
    assert '"full_speed_mps": 0.42' in out and "not saved" in out


def test_mammotion_owner_script_drive_speed_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _script()
    log = tmp_path / "log.jsonl"
    assert script.main(["--dry-run", "--log", str(log), "--get-speed"]) == 0
    assert '"drive_speed_mps": 0.4' in capsys.readouterr().out
    assert script.main(["--dry-run", "--log", str(log), "--set-speed", "0.5"]) == 0
    out = capsys.readouterr().out
    assert "set to 0.5 m/s" in out and '"drive_speed_mps": 0.5' in out
    steps = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    assert [s["drive_speed_mps"] for s in steps if s["step"] == "drive_speed"] == [0.4, 0.5]


def test_mammotion_owner_script_calibrate_needs_manual_driving(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "log.jsonl"
    assert _script().main(["--dry-run", "--log", str(log), "--calibrate"]) == 1
    assert "no manual driving" in capsys.readouterr().out
    steps = [json.loads(line) for line in log.read_text("utf-8").splitlines()]
    (record,) = [s for s in steps if s["step"] == "calibration"]
    assert record["error"] == "no manual drive"


def test_mammotion_owner_script_spot_dry_run_takes_a_calibration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stored = tmp_path / "c.json"
    stored.write_text(json.dumps(_calibration().to_options()), "utf-8")
    log = tmp_path / "log.jsonl"
    argv = ["--dry-run", "--log", str(log), "--spot-dry-run", "--use-calibration", str(stored)]
    assert _script().main(argv) == 1  # the dry run's cloud link has no manual driving
    out = capsys.readouterr().out
    assert "drive calibration in use" in out and '"full_speed_mps": 0.42' in out


class _Run:
    """What ``MowerController.calibration_result`` returns, reduced to what the script reads."""

    state = "done"
    reason = None
    detail = None

    def __init__(self) -> None:
        self.result = DriveCalibrationResult(calibration=_calibration(), steps=STEPS)

    def summary(self) -> str:
        return "calibration done: full speed 0.420 m/s"


class _Controller:
    """A controller whose calibration ends at once (the script's flow without a mower)."""

    calibration_status = None
    last_state = None

    def __init__(self) -> None:
        self.commands: list[Any] = []

    async def capabilities(self, refresh: bool = False) -> Any:
        return SimpleNamespace(manual_drive=True)

    async def handle(self, envelope: Any) -> Any:
        self.commands.append(envelope.command)
        return SimpleNamespace(status=CommandStatus.ACCEPTED, reason="started")

    async def calibration_result(self, run_id: Any) -> _Run:
        return _Run()


async def test_mammotion_owner_script_calibrate_prints_and_writes_the_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MOWDECK_TOKEN", raising=False)
    script = _script()
    out_file = tmp_path / "calibration.json"
    args = script.parse_args(
        ["--calibrate", "--step-s", "2", "--calibration-out", str(out_file), "--save", "--yes"]
    )
    controller = _Controller()
    log = script.JsonLog(tmp_path / "log.jsonl")
    assert await script.run_calibration(controller, args, log) == 0
    assert [c.kind for c in controller.commands] == ["drive_calibrate"]
    assert controller.commands[0].step_s == 2.0
    out = capsys.readouterr().out
    assert "linear +0.50" in out and "angular -0.60" in out
    assert '"drive_full_speed_mps": 0.42' in out  # the options JSON to paste
    assert "not saved" in out
    assert script.load_calibration(out_file) == _calibration()
