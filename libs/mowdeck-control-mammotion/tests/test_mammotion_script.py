"""The owner script runs end to end on the fake port (``--dry-run``)."""

import importlib.util
import itertools
import json
import math
from pathlib import Path
from types import ModuleType

import pytest

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
