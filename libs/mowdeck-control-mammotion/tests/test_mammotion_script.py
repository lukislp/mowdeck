"""The owner script runs end to end on the fake port (``--dry-run``)."""

import importlib.util
import json
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
