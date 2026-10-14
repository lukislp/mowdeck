"""Connection options, secret references, entry points and package metadata."""

import tomllib
from pathlib import Path
from typing import Any

import pytest
from mammotion_support import BLE_OPTIONS, CLOUD_OPTIONS, NOISE_PSK, config
from mowdeck_contracts.v1 import ConnectionKind
from mowdeck_control import ConnectionRegistry
from mowdeck_control_mammotion import (
    PYMAMMOTION_VERSION,
    MammotionBleProxyConnection,
    MammotionCloudConnection,
    create_cloud_connection,
)
from mowdeck_control_mammotion.options import (
    BleProxyOptions,
    CloudOptions,
    OptionsError,
    resolve_secret,
)

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_mammotion_cloud_options_with_password_warn(caplog: pytest.LogCaptureFixture) -> None:
    options = CloudOptions.parse(
        {**CLOUD_OPTIONS, "session_cache_path": "/data/mammotion.json", "confirm_timeout_s": 10}
    )
    assert options.password == CLOUD_OPTIONS["password"]
    assert options.session_cache_path == Path("/data/mammotion.json")
    assert options.confirm_timeout_s == 10
    assert options.refresh_seconds == 20
    assert options.refresh_seconds_active == 5
    assert (
        CloudOptions.parse({**CLOUD_OPTIONS, "refresh_seconds_active": 2}).refresh_seconds_active
        == 2
    )
    assert CloudOptions.parse({**CLOUD_OPTIONS, "refresh_seconds": 60}).refresh_seconds == 60
    assert options.all_cameras is True
    assert CloudOptions.parse({**CLOUD_OPTIONS, "all_cameras": False}).all_cameras is False
    assert "credential_ref" in caplog.text
    assert "secret-password" not in repr(options)


def test_mammotion_cloud_options_with_a_credential_reference(tmp_path: Path) -> None:
    secret = tmp_path / "pw"
    secret.write_text("from-file\n", "utf-8")
    base = {"account": "a@example.com", "iot_id": "iot-1"}
    by_file = CloudOptions.parse({**base, "credential_ref": f"file:{secret}"})
    assert (by_file.password, by_file.device_name) == ("from-file", None)
    by_env = CloudOptions.parse(
        {**base, "credential_ref": "env:MAMMOTION_PW", "password": "ignored"},
        environ={"MAMMOTION_PW": "from-env"},
    )
    assert by_env.password == "from-env"  # noqa: S105 - test value


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"password": "p", "device_name": "x"}, "'account' is required"),
        ({"account": "a", "password": "p"}, "device_name' or 'iot_id'"),
        ({"account": "a", "device_name": "x"}, "'credential_ref' or 'password'"),
        ({"account": 3, "password": "p", "device_name": "x"}, "must be a string"),
        ({**CLOUD_OPTIONS, "colour": "red"}, "unknown options: colour"),
        ({**CLOUD_OPTIONS, "confirm_timeout_s": "soon"}, "must be a number"),
        ({**CLOUD_OPTIONS, "map_sync_timeout_s": 1}, "between"),
        ({**CLOUD_OPTIONS, "refresh_seconds": 5}, "between 10.0 and 300.0"),
        ({**CLOUD_OPTIONS, "refresh_seconds": 301}, "between 10.0 and 300.0"),
        ({**CLOUD_OPTIONS, "refresh_seconds": True}, "must be a number"),
        ({**CLOUD_OPTIONS, "refresh_seconds_active": 1}, "between 2.0 and 60.0"),
        ({**CLOUD_OPTIONS, "refresh_seconds_active": 61}, "between 2.0 and 60.0"),
        ({**CLOUD_OPTIONS, "refresh_seconds_active": "5"}, "must be a number"),
        ({**CLOUD_OPTIONS, "all_cameras": "yes"}, "must be true or false"),
        ({**CLOUD_OPTIONS, "credential_ref": "vault:x"}, "env:NAME or file:/path"),
        ({**CLOUD_OPTIONS, "credential_ref": "env:MISSING_VAR_X"}, "is not set"),
        ({**CLOUD_OPTIONS, "credential_ref": "file:/does/not/exist"}, "cannot read"),
    ],
)
def test_mammotion_invalid_cloud_options(options: dict[str, Any], match: str) -> None:
    with pytest.raises(OptionsError, match=match):
        CloudOptions.parse(options, environ={})


def test_mammotion_ble_options() -> None:
    options = BleProxyOptions.parse({**BLE_OPTIONS, "port": 6054, "allow_manual_blades": True})
    assert options.ble_address == "AA:BB:CC:DD:EE:FF"
    assert options.port == 6054 and options.allow_manual_blades
    assert NOISE_PSK not in repr(options)
    by_ref = BleProxyOptions.parse(
        {"host": "h", "ble_address": "AA:BB:CC:DD:EE:FF", "credential_ref": "env:PSK"},
        environ={"PSK": NOISE_PSK},
    )
    assert by_ref.noise_psk == NOISE_PSK and by_ref.port == 6053


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({**BLE_OPTIONS, "ble_address": "not-a-mac"}, "MAC address"),
        ({**BLE_OPTIONS, "noise_psk": "c2hvcnQ="}, "32 bytes"),
        ({**BLE_OPTIONS, "noise_psk": "%%%"}, "32 bytes"),
        ({k: v for k, v in BLE_OPTIONS.items() if k != "noise_psk"}, "'noise_psk' or"),
        ({**BLE_OPTIONS, "port": 70000}, "between"),
        ({**BLE_OPTIONS, "port": True}, "must be a number"),
        ({**BLE_OPTIONS, "allow_manual_blades": "yes"}, "true or false"),
        ({k: v for k, v in BLE_OPTIONS.items() if k != "host"}, "'host' is required"),
    ],
)
def test_mammotion_invalid_ble_options(options: dict[str, Any], match: str) -> None:
    with pytest.raises(OptionsError, match=match):
        BleProxyOptions.parse(options)


def test_mammotion_resolve_secret_rejects_empty_targets() -> None:
    with pytest.raises(OptionsError):
        resolve_secret("env:", {})


def test_mammotion_registry_resolves_cloud_and_ble_bridge_from_entry_points() -> None:
    registry = ConnectionRegistry()
    kinds = registry.kinds()
    assert ConnectionKind.CLOUD in kinds and ConnectionKind.BLE_BRIDGE in kinds
    assert ConnectionKind.WIFI not in kinds  # not implemented (firmware 1.30.31.19+)
    cloud = registry.create(ConnectionKind.CLOUD, config(dict(CLOUD_OPTIONS)))
    ble = registry.create(ConnectionKind.BLE_BRIDGE, config(dict(BLE_OPTIONS)))
    assert isinstance(cloud, MammotionCloudConnection) and cloud.kind is ConnectionKind.CLOUD
    assert isinstance(ble, MammotionBleProxyConnection) and ble.kind is ConnectionKind.BLE_BRIDGE
    with pytest.raises(OptionsError):
        create_cloud_connection(config({}))


def test_mammotion_package_is_gpl_and_pins_pymammotion() -> None:
    project = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["license"] == "GPL-3.0-only"
    assert f"pymammotion=={PYMAMMOTION_VERSION}" in project["dependencies"]
    assert any(dep.startswith("packaging") for dep in project["dependencies"])
    assert "aioesphomeapi==45.0.3" in project["optional-dependencies"]["esphome"]
    entry_points = project["entry-points"]["mowdeck.connections"]
    assert set(entry_points) == {"cloud", "ble_bridge"}
    assert "GNU GENERAL PUBLIC LICENSE" in (PACKAGE_ROOT / "LICENSE").read_text("utf-8")
    import pymammotion

    assert Path(pymammotion.__file__).parent.name == "pymammotion"


def test_mammotion_pymammotion_is_imported_by_the_port_module_only() -> None:
    source = PACKAGE_ROOT / "src" / "mowdeck_control_mammotion"
    importers = sorted(
        path.name
        for path in source.rglob("*.py")
        if "import pymammotion" in path.read_text("utf-8")
        or "from pymammotion" in path.read_text("utf-8")
    )
    assert importers == ["port.py"]
