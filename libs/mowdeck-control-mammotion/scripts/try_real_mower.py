"""Try the Mammotion adapter against a real mower (spike 0001, owner run).

Connects through ``mowdeck-control-mammotion`` exactly as the Device-Gateway does, prints the
capabilities, the state and a map summary, and optionally sends one command through the
``MowerController`` (with all its safety rules). Every step is appended to a JSON lines log for
the spike results table.

Cloud (default): set ``MAMMOTION_ACCOUNT``, ``MAMMOTION_PASSWORD`` (use the second account the
mower is shared with) and ``MAMMOTION_DEVICE_NAME``::

    uv run python libs/mowdeck-control-mammotion/scripts/try_real_mower.py
    uv run python libs/mowdeck-control-mammotion/scripts/try_real_mower.py --start-area "Front lawn"
    uv run python libs/mowdeck-control-mammotion/scripts/try_real_mower.py --dock
    uv run python libs/mowdeck-control-mammotion/scripts/try_real_mower.py --estop
    uv run python libs/mowdeck-control-mammotion/scripts/try_real_mower.py --camera
    uv run python libs/mowdeck-control-mammotion/scripts/try_real_mower.py --light on

``--camera`` asks the Mammotion cloud for a live camera session (ADR 0032), exactly as the
Device-Gateway does for the web app, and prints its fields with the token, the encryption key and
the salt redacted, plus the camera list. Requesting a session makes the mower start publishing
video (and counts against the 4G video budget when the mower is on 4G).

BLE through an ESPHome Bluetooth Proxy (later)::

    uv run python .../try_real_mower.py --esphome mowdeck-bridge.local,<noise_psk>,AA:BB:CC:DD:EE:FF

Dual link (ADR 0038: the cloud plus the bridge, as the Device-Gateway runs it for a mower with
a ``ble_bridge`` link; set ``MAMMOTION_BLE_ADDRESS`` or pass ``--ble-address``)::

    uv run python .../try_real_mower.py --bridge mowdeck-bridge.local,<noise_psk> --wait 90

The run prints the link state (``connection``, ``links.ble_bridge`` with RSSI and the bridge
firmware) every two seconds, so the hardware questions of the design can be answered: does
the BLE transport attach and get preferred, does the state flip to ``ble_bridge`` and back when
the bridge is unplugged, does the cloud go on meanwhile. ``--scan HOST,KEY`` only looks through
the bridge for mowers (what the web app's "add a bridge" dialog does) and ``--firmware-check``
asks the Mammotion cloud what is installed and offered; neither changes anything on the mower.

Spot mowing (ADR 0039), over a link with manual driving (``--bridge`` or ``--esphome``),
blades always off, the mower idle on the lawn with a fixed RTK position::

    uv run python .../try_real_mower.py --bridge HOST,KEY --turn-check
    uv run python .../try_real_mower.py --bridge HOST,KEY --spot-dry-run

``--turn-check`` turns in place with ``angular`` +0.3 for two seconds (5 frames per second
through the controller, exactly as a spot run sends them), then stops and checks that the
compass heading decreased (left positive), and prints the report interval and the latency from
the first frame to the first report that shows the turn. ``--spot-dry-run`` drives a circle of
1 m diameter that starts at the mower's position along its heading (to the left), at
``--spot-speed`` (default 0.2 m/s), with ``SpotMowRun`` behind ``MowerController`` (geofence,
pauses, aborts, dead-man). Ctrl-C sends ``spot_stop``.

From the dock (ADR 0041), the mower docked, blades always off::

    uv run python .../try_real_mower.py --bridge HOST,KEY --spot-dry-run --from-dock
    uv run python .../try_real_mower.py --bridge H,K --spot-dry-run --from-dock --spot-at 6.5,4

``--from-dock`` runs the whole cycle: ``leave_dock`` over the cloud, the wait for a fixed RTK
position out of the dock, the transit planned on the map (printed before the start), the 1 m
circle around ``--spot-at`` (local metres; default: the centre of ``--spot-area`` or of the
area nearest to the dock) and ``return_to_dock``. Ctrl-C sends ``spot_stop``; the mower then
stops where it is and is not sent home.

Drive calibration (ADR 0042), over the bridge, blades off, the mower idle on open lawn with a
fixed RTK position and about 2 m of room ahead::

    uv run python .../try_real_mower.py --bridge HOST,KEY --calibrate
    uv run python .../try_real_mower.py --bridge HOST,KEY --calibrate --step-s 2.5 \\
        --calibration-out out/calibration.json
    uv run python .../try_real_mower.py --bridge HOST,KEY --spot-dry-run \\
        --use-calibration out/calibration.json

``--calibrate`` sends ``DriveCalibrate`` through the ``MowerController``: short forward steps at
several stick values (at most 1.5 m in all) and turns in place both ways, ``--step-s`` seconds
each. It prints a table of the steps (command, measured speed or yaw rate, reports, report
interval, distance or turn), the fit (full speed, full yaw rate, dead zones, the speed setting it
was measured at) and the link options JSON to paste. Ctrl-C sends ``spot_stop``.
``--calibration-out FILE`` writes the result to a file that ``--use-calibration FILE`` reads back
(it also takes the options JSON), so ``--spot-dry-run`` steers with it.

Saving to Fleet (``PUT {fleet-url}/mowers/{id}/drive-calibration`` with the bearer token in
``MOWDECK_TOKEN``), right after a calibration or from a file without touching the mower::

    uv run python .../try_real_mower.py --bridge HOST,KEY --calibrate --save \\
        --fleet-url https://mowdeck.example/api/v1/fleet --mower-id <uuid>
    uv run python .../try_real_mower.py --save --use-calibration out/calibration.json \\
        --fleet-url https://mowdeck.example/api/v1/fleet --mower-id <uuid>

Without the URL, the mower id or the token, ``--save`` prints the JSON to paste instead.

The speed setting for manual driving (the vendor app's movement speed, by which the mower scales
the stick), read or changed over the cloud::

    uv run python .../try_real_mower.py --get-speed
    uv run python .../try_real_mower.py --set-speed 0.4

Without hardware or credentials (fake PyMammotion port)::

    uv run python .../try_real_mower.py --dry-run --start-area "Front lawn" --estop

Apart from ``--turn-check``, ``--spot-dry-run`` and ``--calibrate`` the script never drives the
mower manually, and it never switches blades on outside a mowing job.
"""

import argparse
import asyncio
import itertools
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    DEFAULT_CALIBRATION_STEP_S,
    MAX_CALIBRATION_STEP_S,
    MIN_CALIBRATION_STEP_S,
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    DriveCalibrate,
    DriveCalibration,
    DriveCalibrationStep,
    EmergencyStop,
    LocalPoint,
    ManualDrive,
    MowerCommand,
    MowerMap,
    MowerState,
    PrincipalKind,
    PrincipalRef,
    ReturnToDock,
    SetDriveSpeed,
    SetLight,
    SpotMow,
    SpotMowPlan,
    SpotSegment,
    SpotStop,
    StartMow,
)
from mowdeck_control import (
    CommandRejected,
    ConnectionConfig,
    MowerConnection,
    MowerController,
    NoRouteError,
    NotConnected,
    camera_session_of,
    drive_speed_port_of,
    firmware_port_of,
    plan_transit,
    pose_of,
    transit_start,
)
from mowdeck_control_mammotion.base import MammotionBase
from mowdeck_control_mammotion.cloud import MammotionCloudConnection, create_cloud_connection
from mowdeck_control_mammotion.fake import SAMPLE_DEVICE_NAME, FakeMammotionPort, firmware_offer
from mowdeck_control_mammotion.mapping import area_summary
from mowdeck_control_mammotion.options import CloudOptions
from mowdeck_control_mammotion.port import Camera, StreamSubscriptionResponse

SAFETY_NOTICE = """\
SAFETY: this script can start a real mowing job and drive the mower to its dock.
  * Keep people, children and pets off the lawn; someone stays at the red STOP button.
  * Close the Mammotion app on the phone and disable the old Home Assistant integration.
  * Only --turn-check, --spot-dry-run and --calibrate drive the mower manually, always
    with the blades off; stay within reach of the mower and keep the vendor app open.
  * --estop sends pause, zero movement and blades off with emergency priority; the
    physical STOP button is still the only guaranteed stop."""

DEFAULT_STEP_S = DEFAULT_CALIBRATION_STEP_S
"""``DriveCalibrate.step_s`` when ``--step-s`` is not given."""
TOKEN_ENV = "MOWDECK_TOKEN"  # noqa: S105 - the variable name, not a token
"""Environment variable with the bearer token for ``--save``."""

MOWER_ID = UUID("00000000-0000-4000-8000-0000000000e1")
ACCOUNT_ID = UUID("00000000-0000-4000-8000-0000000000e2")
ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.SERVICE)


class JsonLog:
    """Append-only JSON lines log (one object per step) for the spike results table."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, step: str, **data: Any) -> None:
        record = {"ts": time.time(), "step": step, **data}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Try mowdeck-control-mammotion against a real Mammotion mower.",
        epilog=SAFETY_NOTICE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--start-area", metavar="NAME", help="start mowing the area with this name")
    action.add_argument("--dock", action="store_true", help="send the mower back to its dock")
    action.add_argument("--estop", action="store_true", help="send an emergency stop")
    action.add_argument(
        "--turn-check",
        action="store_true",
        help="turn in place (angular +0.3 for 2 s) and check the heading sign, report rate and "
        "latency (needs manual driving: --bridge or --esphome)",
    )
    action.add_argument(
        "--spot-dry-run",
        action="store_true",
        help="drive a 1 m circle from the current position with the blades off as a spot run "
        "(needs manual driving: --bridge or --esphome); Ctrl-C stops it",
    )
    action.add_argument(
        "--calibrate",
        action="store_true",
        help="drive calibration: short forward steps and turns in place with the blades off, "
        "then print the steps, the fit and the options JSON (needs manual driving: --bridge or "
        "--esphome); Ctrl-C stops it",
    )
    action.add_argument(
        "--get-speed",
        action="store_true",
        help="print the speed setting for manual driving (m/s)",
    )
    action.add_argument(
        "--set-speed",
        type=float,
        metavar="MPS",
        help="change the speed setting for manual driving (m/s, clamped to the model's range) "
        "and print the read-back",
    )
    action.add_argument(
        "--light",
        choices=("on", "off"),
        help="switch the camera headlamp; the state shows light_on, raw.manual_light and "
        "raw.lamp_bright afterwards",
    )
    parser.add_argument(
        "--pattern",
        default="stripes",
        help="native pattern for --start-area (stripes, diagonal, checkerboard, edge_only, "
        "random_angle)",
    )
    parser.add_argument("--angle", type=float, default=0.0, help="stripe angle in degrees")
    parser.add_argument(
        "--spot-speed",
        type=float,
        default=0.2,
        help="speed of --spot-dry-run in m/s (0.05 to 0.3)",
    )
    parser.add_argument(
        "--from-dock",
        action="store_true",
        help="with --spot-dry-run: start docked, undock, drive the transit, the circle and "
        "back to the dock (blades off)",
    )
    parser.add_argument(
        "--spot-at",
        metavar="X,Y",
        help="with --from-dock: centre of the circle in local metres",
    )
    parser.add_argument(
        "--spot-area",
        metavar="NAME",
        help="with --from-dock: put the circle at the centre of this area",
    )
    parser.add_argument(
        "--step-s",
        type=float,
        metavar="SECONDS",
        help="with --calibrate: duration of each step "
        f"({MIN_CALIBRATION_STEP_S:g} to {MAX_CALIBRATION_STEP_S:g}, default {DEFAULT_STEP_S:g})",
    )
    parser.add_argument(
        "--calibration-out",
        type=Path,
        metavar="FILE",
        help="with --calibrate: write the result as JSON (read back by --use-calibration)",
    )
    parser.add_argument(
        "--use-calibration",
        type=Path,
        metavar="FILE",
        help="a calibration JSON (--calibration-out or the options JSON) for --spot-dry-run, or "
        "to --save without a new calibration",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="with --calibrate or --use-calibration: PUT the calibration to Fleet (--fleet-url, "
        f"--mower-id and the token in {TOKEN_ENV}); without them print the JSON to paste",
    )
    parser.add_argument(
        "--fleet-url",
        metavar="URL",
        help="with --save: Fleet's API base URL, e.g. https://HOST/api/v1/fleet",
    )
    parser.add_argument(
        "--mower-id", type=UUID, metavar="UUID", help="with --save: the mower's id in Fleet"
    )
    parser.add_argument(
        "--camera",
        action="store_true",
        help="request a live camera session and print it (token and key redacted)",
    )
    parser.add_argument(
        "--esphome",
        metavar="HOST,PSK,MAC",
        help="use the BLE path through an ESPHome Bluetooth Proxy instead of the cloud",
    )
    parser.add_argument(
        "--bridge",
        metavar="HOST,PSK",
        help="cloud plus a Bluetooth bridge (dual link, ADR 0038); needs --ble-address or "
        "MAMMOTION_BLE_ADDRESS",
    )
    parser.add_argument(
        "--ble-address", metavar="MAC", help="the mower's Bluetooth address for --bridge"
    )
    parser.add_argument(
        "--scan",
        metavar="HOST,PSK",
        help="look for mowers through the bridge and exit (no cloud login needed)",
    )
    parser.add_argument(
        "--firmware-check",
        action="store_true",
        help="ask the Mammotion cloud what firmware is installed and offered",
    )
    parser.add_argument(
        "--device-name", help="BLE only: the advertised name, e.g. Luba-VP... (else advertised)"
    )
    parser.add_argument(
        "--session-cache",
        type=Path,
        default=Path("out/mammotion_session.json"),
        help="PyMammotion credential cache (holds live tokens; never commit it)",
    )
    parser.add_argument(
        "--log", type=Path, default=Path("out/try_real_mower.jsonl"), help="JSON lines log file"
    )
    parser.add_argument(
        "--wait", type=float, default=20.0, help="seconds to watch the state after a command"
    )
    parser.add_argument("--yes", action="store_true", help="do not ask before sending a command")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="use the fake PyMammotion port (no network, no credentials)",
    )
    args = parser.parse_args(argv)
    if (args.from_dock or args.spot_at or args.spot_area) and not args.spot_dry_run:
        parser.error("--from-dock, --spot-at and --spot-area go with --spot-dry-run")
    if (args.spot_at or args.spot_area) and not args.from_dock:
        parser.error("--spot-at and --spot-area go with --from-dock")
    if (args.step_s is not None or args.calibration_out) and not args.calibrate:
        parser.error("--step-s and --calibration-out go with --calibrate")
    if args.step_s is not None and not (
        MIN_CALIBRATION_STEP_S <= args.step_s <= MAX_CALIBRATION_STEP_S
    ):
        parser.error(
            f"--step-s must be between {MIN_CALIBRATION_STEP_S:g} and "
            f"{MAX_CALIBRATION_STEP_S:g} seconds"
        )
    if args.use_calibration and not (args.spot_dry_run or args.save):
        parser.error("--use-calibration goes with --spot-dry-run or --save")
    if args.save and args.calibrate == bool(args.use_calibration):
        parser.error("--save needs either --calibrate or --use-calibration")
    if args.save and args.use_calibration and args.spot_dry_run:
        parser.error("--save --use-calibration only saves; run --spot-dry-run separately")
    if (args.fleet_url or args.mower_id) and not args.save:
        parser.error("--fleet-url and --mower-id go with --save")
    if args.set_speed is not None and not 0 < args.set_speed <= 5:
        parser.error("--set-speed must be above 0 and at most 5 m/s")
    return args


def build_connection(args: argparse.Namespace) -> MowerConnection:
    """The adapter connection for the chosen path (cloud, ESPHome BLE or dry run)."""
    if args.dry_run:
        dry_options = CloudOptions.parse(
            {"account": "dry-run@example.com", "password": "dry-run", "device_name": "dry-run"}
        )
        config = ConnectionConfig(mower_id=MOWER_ID, account_id=ACCOUNT_ID)
        port = FakeMammotionPort(device_name=SAMPLE_DEVICE_NAME)
        port.firmware_check = firmware_offer()  # the fake vendor offers an update
        port.stream = StreamSubscriptionResponse(
            appid="dry-run-app",
            openEncrypt=1,
            cameras=[Camera(cameraId=0, token="dry-run-camera-token")],  # noqa: S106
            channelName="dry-run-channel",
            areaCode="GLOB",
            token="dry-run-viewer-token",  # noqa: S106 - a placeholder, not a credential
            uid=1000,
            key="dry-run-key-0123456789",
            salt="ZHJ5LXJ1bi1zYWx0LTAxMjM0NTY3ODlhYmNkZWY=",
        )
        return MammotionCloudConnection(port, config, dry_options)
    if args.esphome:
        try:
            host, psk, mac = (part.strip() for part in args.esphome.split(","))
        except ValueError:
            raise SystemExit("--esphome needs HOST,PSK,MAC") from None
        from mowdeck_control_mammotion.ble import create_ble_bridge_connection

        options: dict[str, Any] = {"host": host, "noise_psk": psk, "ble_address": mac}
        if args.device_name:
            options["device_name"] = args.device_name
        return create_ble_bridge_connection(
            ConnectionConfig(mower_id=MOWER_ID, account_id=ACCOUNT_ID, options=options)
        )
    missing = [
        name
        for name in ("MAMMOTION_ACCOUNT", "MAMMOTION_PASSWORD", "MAMMOTION_DEVICE_NAME")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit(f"set {', '.join(missing)} (or use --dry-run)")
    cloud_options: dict[str, Any] = {
        "account": os.environ["MAMMOTION_ACCOUNT"],
        "credential_ref": "env:MAMMOTION_PASSWORD",
        "device_name": os.environ["MAMMOTION_DEVICE_NAME"],
        "session_cache_path": str(args.session_cache),
    }
    if args.bridge:
        host, psk = parse_host_key(args.bridge, "--bridge")
        mac = args.ble_address or os.environ.get("MAMMOTION_BLE_ADDRESS")
        if not mac:
            raise SystemExit("--bridge needs --ble-address or MAMMOTION_BLE_ADDRESS")
        cloud_options["links"] = {
            "ble_bridge": {
                "host": host,
                "noise_psk": psk,
                "ble_address": mac,
                "device_name": os.environ["MAMMOTION_DEVICE_NAME"],
            }
        }
    return create_cloud_connection(
        ConnectionConfig(mower_id=MOWER_ID, account_id=ACCOUNT_ID, options=cloud_options)
    )


def parse_host_key(value: str, flag: str) -> tuple[str, str]:
    """``HOST,KEY`` of ``--bridge`` and ``--scan``."""
    try:
        host, psk = (part.strip() for part in value.split(","))
    except ValueError:
        raise SystemExit(f"{flag} needs HOST,KEY") from None
    return host, psk


async def run_scan(args: argparse.Namespace, log: JsonLog) -> int:
    """``--scan``: look for mowers through the bridge (ADR 0038) and print the reply."""
    from mowdeck_control_mammotion.esphome import scan_proxy

    host, psk = parse_host_key(args.scan, "--scan")
    reply = await scan_proxy(
        host=host,
        port=6053,
        noise_psk=psk,
        expected_name=os.environ.get("MAMMOTION_DEVICE_NAME") or args.device_name,
        timeout_s=min(args.wait, 30.0),
    )
    data = reply.model_dump(mode="json")
    show("bridge scan", data)
    log.write("bridge_scan", **data)
    return 0 if reply.ok else 1


def state_line(state: MowerState) -> dict[str, Any]:
    """The values the spike table records."""
    return {
        "connection": state.connection.value,
        "links": {k: v.model_dump(mode="json") for k, v in state.links.items()},
        "firmware_info": state.firmware_info.model_dump(mode="json")
        if state.firmware_info
        else None,
        "activity": state.activity.value,
        "battery_percent": state.battery_percent,
        "charging": state.charging,
        "rtk_fix": state.rtk_fix.value,
        "rtk_source": state.rtk_source.value,
        "firmware": state.firmware,
        "position": state.position.model_dump() if state.position else None,
        "position_source": state.position_source.value,
        "blade_height_mm": state.blade_height_mm,
        "light_on": state.light_on,
        "drive_speed_mps": state.drive_speed_mps,
        "error_code": state.error_code,
        "raw": state.raw,
    }


def find_area(mower_map: MowerMap, name: str) -> str:
    """Area id by name (case-insensitive) or by id."""
    for area in mower_map.areas:
        if area.id == name or (area.name or "").casefold() == name.casefold():
            return area.id
    names = ", ".join(f"{a.name or '?'} ({a.id})" for a in mower_map.areas) or "none"
    raise SystemExit(f"no area named {name!r}; areas: {names}")


async def show_camera(connection: MowerConnection, log: JsonLog) -> None:
    """Request a camera session and print it with every secret redacted."""
    try:
        session = await camera_session_of(connection)
    except NotConnected as exc:
        show("camera", {"error": str(exc)})
        log.write("camera", error=str(exc))
        return
    if session is None:
        show("camera", {"error": "this connection has no camera"})
        log.write("camera", error="no camera")
        return
    redacted = session.redacted()
    show("camera session (secrets redacted)", redacted)
    cameras = [
        f"{c.id}: {c.name} (uid {c.uid if c.uid is not None else '?'})" for c in session.cameras
    ]
    show("cameras", cameras or ["none listed"])
    log.write("camera", **redacted)
    print(
        "\nTo see the picture, open the mower in the MowDeck web app and press Live camera "
        "(docs/dev/web-app.md, section Live camera). A LUBA 2 AWD X lists Front left (uid 1) "
        "and Front right (uid 2); choose Both in the camera picker to see them side by side."
    )


async def show_firmware(connection: MowerConnection, log: JsonLog) -> None:
    """``--firmware-check``: the vendor's answer as the Device-Gateway stores it."""
    port = firmware_port_of(connection)
    if port is None:
        show("firmware", {"error": "this connection has no firmware service"})
        return
    try:
        info = await port.check_firmware()
    except NotConnected as exc:
        show("firmware", {"error": str(exc)})
        log.write("firmware", error=str(exc))
        return
    data = info.model_dump(mode="json") if info else None
    show("firmware", data)
    log.write("firmware", info=data)


def link_text(line: dict[str, Any]) -> str:
    """The bridge part of a state line (empty without a bridge link)."""
    bridge = line["links"].get("ble_bridge")
    if not bridge:
        return ""
    up = "up" if bridge["connected"] else "down"
    return (
        f" bridge={up} rssi={bridge['rssi']} esphome={bridge['esphome_version']} "
        f"outdated={bridge['outdated_reason']}"
    )


async def watch_links(connection: MowerConnection, log: JsonLog, seconds: float) -> None:
    """``--bridge`` without a command: print the link state for ``seconds`` (the dual-link
    hardware questions: attach, flip to ``ble_bridge``, loss and reconnect)."""
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        await asyncio.sleep(2)
        line = state_line(await connection.get_state())
        print(f"link connection={line['connection']}{link_text(line)}")
        log.write("link_state", **line)


def confirm(args: argparse.Namespace, what: str) -> None:
    if args.yes or args.dry_run:
        return
    answer = input(f"{what}: lawn clear and someone at STOP? Type yes to send: ")
    if answer.strip().lower() != "yes":
        raise SystemExit("aborted")


def show(title: str, data: Any) -> None:
    print(f"\n== {title}")
    print(json.dumps(data, indent=2, default=str))


# --- spot mowing hardware checks (ADR 0039) ------------------------------------------------

TURN_ANGULAR = 0.3
"""``angular`` of the turn check (left positive: the compass heading must decrease)."""
TURN_SECONDS = 2.0
FRAME_HZ = 5.0
TURN_SEEN_DEG = 2.0
"""A heading change of at least this many degrees counts as the turn showing in a report."""
CIRCLE_DIAMETER_M = 1.0
SPOT_SPEED_RANGE = (0.05, 0.3)


def heading_delta(start_deg: float, end_deg: float) -> float:
    """Signed compass change from ``start_deg`` to ``end_deg`` in ``(-180, 180]`` degrees."""
    return 180.0 - (180.0 - (end_deg - start_deg)) % 360.0


def turn_check_summary(
    samples: Sequence[tuple[float, float, float | None]], first_frame_s: float
) -> dict[str, Any]:
    """Evaluate a turn check from ``(received_s, observed_s, heading_deg)`` samples (one per
    published state: perf-counter receive time, ``observed_at`` as a timestamp, heading).

    The last heading before the first frame is the reference; the verdict is ``ok`` when the
    compass heading decreased by at least :data:`TURN_SEEN_DEG`, ``sign_error`` when it
    increased by that much, ``no_turn`` otherwise and ``no_heading`` without headings."""
    before = [h for r, _, h in samples if r <= first_frame_s and h is not None]
    after = [(r, h) for r, _, h in samples if r > first_frame_s and h is not None]
    observed = sorted({o for _, o, _ in samples})
    intervals = [b - a for a, b in itertools.pairwise(observed)]
    summary: dict[str, Any] = {
        "reports": len(observed),
        "report_interval_s": {
            "min": round(min(intervals), 2),
            "median": round(statistics.median(intervals), 2),
            "max": round(max(intervals), 2),
        }
        if intervals
        else None,
        "start_heading_deg": before[-1] if before else None,
        "end_heading_deg": after[-1][1] if after else None,
        "heading_change_deg": None,
        "latency_s": None,
        "verdict": "no_heading",
    }
    if not before or not after:
        return summary
    start = before[-1]
    change = heading_delta(start, after[-1][1])
    summary["heading_change_deg"] = round(change, 1)
    seen = [r for r, h in after if abs(heading_delta(start, h)) >= TURN_SEEN_DEG]
    if seen:
        summary["latency_s"] = round(seen[0] - first_frame_s, 2)
    if change <= -TURN_SEEN_DEG:
        summary["verdict"] = "ok"
    elif change >= TURN_SEEN_DEG:
        summary["verdict"] = "sign_error"
    else:
        summary["verdict"] = "no_turn"
    return summary


def circle_points(
    x: float, y: float, theta: float, diameter_m: float = CIRCLE_DIAMETER_M, step_m: float = 0.2
) -> list[LocalPoint]:
    """A closed circle that starts at ``(x, y)`` heading ``theta`` (local radians) and turns
    left (counter-clockwise), points about ``step_m`` apart."""
    r = diameter_m / 2
    cx, cy = x - r * math.sin(theta), y + r * math.cos(theta)
    steps = max(8, math.ceil(math.pi * diameter_m / step_m))
    start = theta - math.pi / 2
    angles = [start + 2 * math.pi * i / steps for i in range(steps + 1)]
    return [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angles]


def envelope_of(command: MowerCommand, ttl: timedelta = timedelta(seconds=60)) -> CommandEnvelope:
    """An envelope of the script's principal."""
    return CommandEnvelope.create(
        mower_id=MOWER_ID, account_id=ACCOUNT_ID, issued_by=ISSUER, command=command, ttl=ttl
    )


def frame_of(angular: float) -> CommandEnvelope:
    """A turn-in-place frame as a spot run sends it (``ttl_ms`` 1000)."""
    frame = ManualDrive(linear=0.0, angular=angular, ttl_ms=1000)
    return envelope_of(frame, timedelta(seconds=1))


NO_DRIVE = (
    "this connection offers no manual driving (use --bridge or --esphome; with --bridge the "
    "bridge did not attach within --wait seconds)"
)
VERDICTS = {
    "ok": "OK: the compass heading decreased (left positive); spot runs steer the right way.",
    "sign_error": "SIGN ERROR: the heading increased; do NOT start a spot run.",
    "no_turn": "NO TURN SEEN: no heading change in the reports; check the link and the report "
    "rate before a spot run.",
    "no_heading": "NO HEADING: the mower reports no heading; spot runs cannot start.",
}


async def wait_for_manual_drive(controller: MowerController, wait_s: float) -> bool:
    """True once the connection offers manual driving; the dual link offers it only after the
    bridge attached, so the capabilities are read again every second for up to ``wait_s``."""
    deadline = time.perf_counter() + wait_s
    while True:
        if (await controller.capabilities(refresh=True)).manual_drive:
            return True
        if time.perf_counter() >= deadline:
            return False
        print("waiting for the Bluetooth bridge (manual driving) ...")
        await asyncio.sleep(1.0)


async def run_turn_check(
    controller: MowerController, log: JsonLog, *, settle_s: float, wait_s: float
) -> int:
    """``--turn-check``: turn in place left and evaluate heading sign, report rate, latency."""
    if not await wait_for_manual_drive(controller, wait_s):
        show("turn check", {"error": NO_DRIVE})
        log.write("turn_check", error="no manual drive")
        return 1
    samples: list[tuple[float, float, float | None]] = []
    subscription = controller.watch_state()

    async def collect() -> None:
        async for state in subscription:
            heading = state.position.heading if state.position else None
            samples.append((time.perf_counter(), state.observed_at.timestamp(), heading))

    collector = asyncio.create_task(collect(), name="turn-check-states")
    await asyncio.sleep(settle_s)  # a reference heading before the first frame
    first_frame_s = time.perf_counter()
    refused: CommandResult | None = None
    try:
        for _ in range(round(TURN_SECONDS * FRAME_HZ)):
            result = await controller.handle(frame_of(TURN_ANGULAR))
            if result.status is not CommandStatus.ACCEPTED:
                refused = result
                break
            await asyncio.sleep(1 / FRAME_HZ)
        await controller.handle(frame_of(0.0))
        await asyncio.sleep(settle_s * 2)  # reports that arrive after the stop
    finally:
        await controller.end_drive_session("turn check finished")
        collector.cancel()
    if refused is not None:
        show("turn check", {"error": f"frame {refused.status}: {refused.reason}"})
        log.write("turn_check", error=refused.reason, status=str(refused.status))
        return 1
    summary = turn_check_summary(samples, first_frame_s)
    show("turn check", summary)
    log.write("turn_check", **summary)
    print(VERDICTS[summary["verdict"]])
    return 0 if summary["verdict"] == "ok" else 1


async def run_spot_dry_run(
    controller: MowerController, args: argparse.Namespace, log: JsonLog
) -> int:
    """``--spot-dry-run``: a 1 m circle from the current pose with the blades off."""
    low, high = SPOT_SPEED_RANGE
    if not low <= args.spot_speed <= high:
        raise SystemExit(f"--spot-speed must be between {low} and {high} m/s")
    if not await wait_for_manual_drive(controller, 0.0 if args.dry_run else args.wait):
        show("spot dry run", {"error": NO_DRIVE})
        log.write("spot_dry_run", error="no manual drive")
        return 1
    mower_map = await controller.mower_map()
    if args.from_dock:
        return await run_spot_from_dock(controller, args, log, mower_map)
    pose = pose_of(controller.last_state, mower_map)
    if pose is None:
        show("spot dry run", {"error": "the mower reports no position or heading"})
        log.write("spot_dry_run", error="no pose")
        return 1
    points = circle_points(pose.x, pose.y, pose.theta)
    segment = SpotSegment.from_points(points, blades_on=False, speed_mps=args.spot_speed)
    command = SpotMow(
        plan=SpotMowPlan.build([segment], ["circle"]), speed_mps=args.spot_speed, blades=False
    )
    show(
        "spot dry run plan", {"start": [round(pose.x, 2), round(pose.y, 2)], "points": len(points)}
    )
    confirm(args, "spot_mow dry run (blades off, Ctrl-C stops)")
    return await follow_spot_run(controller, command, log)


def spot_centre(args: argparse.Namespace, mower_map: MowerMap) -> LocalPoint:
    """``--spot-at``, the centre of ``--spot-area`` or of the area nearest to the dock."""
    if args.spot_at:
        x, _, y = args.spot_at.partition(",")
        try:
            return (float(x), float(y))
        except ValueError:
            raise SystemExit("--spot-at needs X,Y in metres, for example 6.5,4.0") from None
    if not mower_map.areas:
        raise SystemExit("the map has no areas")
    if args.spot_area:
        found = mower_map.area(find_area(mower_map, args.spot_area))
        if found is None:
            raise SystemExit(f"no area named {args.spot_area!r}")
        area = found
    else:
        dock = (mower_map.dock.x, mower_map.dock.y) if mower_map.dock else (0.0, 0.0)
        area = min(
            mower_map.areas,
            key=lambda a: min(math.dist(dock, p) for p in a.boundary),
        )
    xs = [p[0] for p in area.boundary]
    ys = [p[1] for p in area.boundary]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


async def run_spot_from_dock(
    controller: MowerController,
    args: argparse.Namespace,
    log: JsonLog,
    mower_map: MowerMap,
) -> int:
    """``--spot-dry-run --from-dock``: undock, transit, a 1 m circle, return; blades off."""
    caps = await controller.capabilities()
    state = controller.last_state
    if not caps.undock:
        show("spot from dock", {"error": "this connection cannot undock the mower"})
        log.write("spot_from_dock", error="no undock")
        return 1
    if state is None or state.activity.value not in ("docked", "charging"):
        activity = state.activity.value if state else "unknown"
        show("spot from dock", {"error": f"the mower is {activity}, not docked"})
        log.write("spot_from_dock", error="not docked", activity=activity)
        return 1
    cx, cy = spot_centre(args, mower_map)
    points = circle_points(cx, cy - CIRCLE_DIAMETER_M / 2, 0.0)
    start = transit_start(state, mower_map)
    try:
        if start is None:
            raise NoRouteError("the map has no dock and the mower reports no position")
        route = plan_transit(mower_map, start, points[0])
    except NoRouteError as exc:
        show("spot from dock", {"error": f"no route to the spot: {exc}"})
        log.write("spot_from_dock", error="no_route", detail=str(exc))
        return 1
    segment = SpotSegment.from_points(points, blades_on=False, speed_mps=args.spot_speed)
    command = SpotMow(
        plan=SpotMowPlan.build([segment], ["circle"]),
        speed_mps=args.spot_speed,
        blades=False,
        return_to_dock=True,
    )
    show(
        "spot from dock plan",
        {
            "centre": [round(cx, 2), round(cy, 2)],
            "transit_m": round(route.length_m, 1),
            "transit": [[round(x, 2), round(y, 2)] for x, y in route.points],
            "points": len(points),
        },
    )
    log.write("spot_from_dock_plan", transit_m=route.length_m, centre=[cx, cy])
    confirm(args, "spot_mow from the dock (undock, transit, circle, return; blades off)")
    return await follow_spot_run(controller, command, log)


async def follow_spot_run(controller: MowerController, command: SpotMow, log: JsonLog) -> int:
    """Send ``command``, print the run's status every second, Ctrl-C sends ``spot_stop``."""
    envelope = envelope_of(command)
    result = await controller.handle(envelope)
    show("result", {"command": command.kind, "status": result.status, "reason": result.reason})
    log.write("spot_dry_run", status=str(result.status), reason=result.reason)
    if result.status is not CommandStatus.ACCEPTED:
        return 1
    waiter = asyncio.ensure_future(controller.spot_run_result(envelope.command_id))
    try:
        while not waiter.done():
            await asyncio.wait({waiter}, timeout=1.0)
            print_spot_status(controller, log)
    except (asyncio.CancelledError, KeyboardInterrupt):
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        print("\nCtrl-C: sending spot_stop")
        stopped = await controller.handle(envelope_of(SpotStop()))
        print(f"spot_stop {stopped.status}: {stopped.reason}")
        log.write("spot_stop", status=str(stopped.status), reason=stopped.reason)
        raise
    report = waiter.result()
    if report is None:
        return 1
    data = {
        "state": report.state,
        "reason": report.reason,
        "detail": report.detail,
        "summary": report.summary(),
        "frames_sent": report.frames_sent,
        "paused_s": round(report.paused_s, 1),
        "transit_m": round(report.transit_m, 1),
        "undocked": report.undocked,
        "sent_home": report.sent_home,
    }
    show("spot run report", data)
    log.write("spot_report", **data)
    return 0 if report.state == "done" else 1


def print_spot_status(controller: MowerController, log: JsonLog) -> None:
    """One status line of a running spot run (and a log record)."""
    status = controller.spot_status
    state = controller.last_state
    if status is None or state is None:
        return
    heading = state.position.heading if state.position else None
    reason = f" reason={status.reason}" if status.reason else ""
    print(
        f"spot {status.state} {status.progress_percent}% "
        f"points={status.driven_points}/{status.planned_points} rtk={state.rtk_fix.value} "
        f"heading={heading} observed={state.observed_at.time().isoformat(timespec='seconds')}"
        f"{reason}"
    )
    log.write("spot_status", **status.model_dump(mode="json"), rtk=state.rtk_fix.value)


# --- drive calibration and the speed setting (ADR 0042) ------------------------------------

CALIBRATION_COLUMNS = (
    "#",
    "kind",
    "command",
    "measured",
    "reports",
    "interval s",
    "window s",
    "distance/turned",
    "outcome",
)


def calibration_rows(steps: Sequence[DriveCalibrationStep]) -> list[list[str]]:
    """The cells of the step table (:data:`CALIBRATION_COLUMNS`), one row per step."""
    rows: list[list[str]] = []
    for number, step in enumerate(steps, start=1):
        forward = step.kind == "forward"
        unit = "m/s" if forward else "deg/s"
        measured = "-" if step.measured is None else f"{step.measured:.3f} {unit}"
        interval = "-" if step.report_interval_s is None else f"{step.report_interval_s:.2f}"
        moved = f"{step.distance_m:.2f} m" if forward else f"{step.turned_deg:+.1f} deg"
        window = "-" if step.window_s is None else f"{step.window_s:.1f}"
        outcome = f"{step.outcome}: {step.note}" if step.note else step.outcome
        rows.append(
            [
                str(number),
                step.kind,
                f"{'linear' if forward else 'angular'} {step.command:+.2f}",
                measured,
                str(step.reports),
                interval,
                window,
                moved,
                outcome,
            ]
        )
    return rows


def calibration_table(steps: Sequence[DriveCalibrationStep]) -> str:
    """The steps as a plain-text table with aligned columns."""
    rows = [list(CALIBRATION_COLUMNS), *calibration_rows(steps)]
    widths = [max(len(row[i]) for row in rows) for i in range(len(CALIBRATION_COLUMNS))]
    lines = [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True))
        for row in rows
    ]
    lines.insert(1, "  ".join("-" * width for width in widths))
    return "\n".join(line.rstrip() for line in lines)


def calibration_fit(calibration: DriveCalibration) -> dict[str, Any]:
    """The fitted values with their units, as printed after the table."""
    return {
        "full_speed_mps": round(calibration.full_speed_mps, 4),
        "full_yaw_rate_dps": round(calibration.full_yaw_rate_dps, 2),
        "dead_zone": round(calibration.dead_zone, 3),
        "turn_dead_zone": round(calibration.turn_dead_zone, 3),
        "drive_speed_setting_mps": calibration.drive_speed_setting,
        "calibrated_at": calibration.calibrated_at.isoformat(),
    }


def calibration_body(calibration: DriveCalibration) -> dict[str, Any]:
    """The JSON body of ``PUT /mowers/{id}/drive-calibration`` (the calibration's fields)."""
    return {
        "full_speed_mps": calibration.full_speed_mps,
        "full_yaw_rate_dps": calibration.full_yaw_rate_dps,
        "dead_zone": calibration.dead_zone,
        "turn_dead_zone": calibration.turn_dead_zone,
        "drive_speed_setting": calibration.drive_speed_setting,
        "calibrated_at": calibration.calibrated_at.isoformat(),
    }


def load_calibration(path: Path) -> DriveCalibration:
    """A calibration from ``path``: the file of ``--calibration-out`` (``calibration`` key), the
    link options JSON (``drive_full_speed_mps``, ...) or the calibration's own fields."""
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read the calibration {path}: {exc}") from None
    if isinstance(data, dict) and isinstance(data.get("calibration"), dict):
        data = data["calibration"]
    if isinstance(data, dict) and "drive_full_speed_mps" in data:
        found = DriveCalibration.from_options(data)
        if found is None:
            raise SystemExit(f"{path} holds invalid calibration options")
        return found
    try:
        return DriveCalibration.model_validate(data)
    except ValueError as exc:
        raise SystemExit(f"{path} holds no valid calibration: {exc}") from None


def calibration_file(
    calibration: DriveCalibration, steps: Sequence[DriveCalibrationStep]
) -> dict[str, Any]:
    """What ``--calibration-out`` writes (and :func:`load_calibration` reads back)."""
    return {
        "calibration": calibration.model_dump(mode="json"),
        "options": calibration.to_options(),
        "steps": [step.model_dump(mode="json") for step in steps],
    }


Opener = Callable[..., Any]
"""``urllib.request.urlopen`` (tests pass a fake)."""


def save_calibration(
    calibration: DriveCalibration,
    *,
    fleet_url: str | None,
    mower_id: UUID | None,
    token: str | None,
    opener: Opener = urllib.request.urlopen,
) -> tuple[bool, str]:
    """``--save``: PUT the calibration to Fleet; ``(saved, message)``. Without the URL, the
    mower id or the token nothing is sent and the message says what is missing."""
    missing = [
        name
        for name, value in (
            ("--fleet-url", fleet_url),
            ("--mower-id", mower_id),
            (TOKEN_ENV, token),
        )
        if not value
    ]
    if missing:
        return False, f"not saved (missing {', '.join(missing)}); paste the JSON above instead"
    assert fleet_url is not None  # noqa: S101 - checked above
    scheme = urllib.parse.urlsplit(fleet_url).scheme
    if scheme not in ("http", "https"):
        return False, f"not saved: --fleet-url must be an http or https URL, not {scheme!r}"
    url = f"{fleet_url.rstrip('/')}/mowers/{mower_id}/drive-calibration"
    request = urllib.request.Request(  # noqa: S310 - the scheme is checked above
        url,
        data=json.dumps(calibration_body(calibration)).encode("utf-8"),
        method="PUT",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with opener(request, timeout=15) as response:
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        return False, f"Fleet refused the calibration: HTTP {exc.code} {exc.reason}"
    except (urllib.error.URLError, OSError) as exc:
        return False, f"Fleet not reachable at {url}: {exc}"
    return True, f"saved to {url} (HTTP {status})"


def show_save(calibration: DriveCalibration, args: argparse.Namespace, log: JsonLog) -> bool:
    """Print the body, PUT it when Fleet is configured, and print the outcome."""
    show("calibration JSON (PUT /mowers/{id}/drive-calibration)", calibration_body(calibration))
    saved, message = save_calibration(
        calibration,
        fleet_url=args.fleet_url,
        mower_id=args.mower_id,
        token=os.environ.get(TOKEN_ENV),
    )
    print(message)
    log.write("calibration_save", saved=saved, message=message)
    return saved


def print_calibration_status(controller: MowerController, log: JsonLog) -> None:
    """One status line of a running calibration (and a log record)."""
    status = controller.calibration_status
    state = controller.last_state
    if status is None or state is None:
        return
    heading = state.position.heading if state.position else None
    reason = f" reason={status.reason}" if status.reason else ""
    print(
        f"calibration {status.state} step {status.step_index + 1}/{status.steps_total} "
        f"rtk={state.rtk_fix.value} heading={heading} "
        f"observed={state.observed_at.time().isoformat(timespec='seconds')}{reason}"
    )
    log.write("calibration_status", state=status.state, step_index=status.step_index)


async def run_calibration(
    controller: MowerController, args: argparse.Namespace, log: JsonLog
) -> int:
    """``--calibrate``: run ``DriveCalibrate`` and print the steps, the fit and the options."""
    if not await wait_for_manual_drive(controller, 0.0 if args.dry_run else args.wait):
        show("calibration", {"error": NO_DRIVE})
        log.write("calibration", error="no manual drive")
        return 1
    step_s = args.step_s if args.step_s is not None else DEFAULT_STEP_S
    confirm(args, "drive calibration (forward up to 1.5 m and turns in place, blades off)")
    envelope = envelope_of(DriveCalibrate(step_s=step_s))
    result = await controller.handle(envelope)
    show("result", {"command": "drive_calibrate", "status": result.status, "reason": result.reason})
    log.write("calibration", status=str(result.status), reason=result.reason)
    if result.status is not CommandStatus.ACCEPTED:
        return 1
    waiter = asyncio.ensure_future(controller.calibration_result(envelope.command_id))
    try:
        while not waiter.done():
            await asyncio.wait({waiter}, timeout=1.0)
            print_calibration_status(controller, log)
    except (asyncio.CancelledError, KeyboardInterrupt):
        task = asyncio.current_task()
        if task is not None:
            task.uncancel()
        print("\nCtrl-C: sending spot_stop")
        stopped = await controller.handle(envelope_of(SpotStop()))
        print(f"spot_stop {stopped.status}: {stopped.reason}")
        log.write("spot_stop", status=str(stopped.status), reason=stopped.reason)
        raise
    run = waiter.result()
    if run is None:
        return 1
    print(f"\n{run.summary()}")
    log.write("calibration_end", state=run.state, reason=run.reason, detail=run.detail)
    steps = run.result.steps if run.result is not None else run.status.steps
    if steps:
        print("\n== calibration steps")
        print(calibration_table(steps))
    if run.state != "done" or run.result is None:
        return 1
    calibration = run.result.calibration
    show("fit", calibration_fit(calibration))
    show("link options (paste into the drive link's options)", calibration.to_options())
    log.write("calibration_fit", **calibration_fit(calibration))
    if args.calibration_out:
        args.calibration_out.parent.mkdir(parents=True, exist_ok=True)
        payload = calibration_file(calibration, steps)
        args.calibration_out.write_text(json.dumps(payload, indent=2) + "\n", "utf-8")
        print(f"calibration written to {args.calibration_out}")
    if args.save:
        show_save(calibration, args, log)
    return 0


async def show_drive_speed(
    controller: MowerController, connection: MowerConnection, args: argparse.Namespace, log: JsonLog
) -> int:
    """``--get-speed`` / ``--set-speed``: the speed setting for manual driving."""
    caps = await controller.capabilities()
    port = drive_speed_port_of(connection)
    if port is None or caps.drive_speed_setting is None:
        show("drive speed", {"error": "this connection has no speed setting for manual driving"})
        log.write("drive_speed", error="not supported")
        return 1
    setting = caps.drive_speed_setting.model_dump(mode="json")
    if args.set_speed is not None:
        confirm(args, f"set_drive_speed {args.set_speed:g} m/s")
        result = await controller.handle(envelope_of(SetDriveSpeed(speed_mps=args.set_speed)))
        show(
            "result",
            {"command": "set_drive_speed", "status": result.status, "reason": result.reason},
        )
        log.write("set_drive_speed", status=str(result.status), reason=result.reason)
        if result.status is not CommandStatus.EXECUTED:
            return 1
    try:
        speed = await port.get_drive_speed()
    except (NotConnected, CommandRejected) as exc:
        show("drive speed", {"error": str(exc)})
        log.write("drive_speed", error=str(exc))
        return 1
    show("drive speed", {"drive_speed_mps": speed, "range": setting})
    log.write("drive_speed", drive_speed_mps=speed, range=setting)
    return 0 if speed is not None else 1


async def run(args: argparse.Namespace) -> int:
    print(SAFETY_NOTICE)
    log = JsonLog(args.log)
    if args.scan:
        return await run_scan(args, log)
    stored = load_calibration(args.use_calibration) if args.use_calibration else None
    if stored is not None and args.save:
        # --save --use-calibration: only the PUT, the mower is not touched
        return 0 if show_save(stored, args, log) else 1
    if stored is not None:
        show("drive calibration in use", calibration_fit(stored))
    connection = build_connection(args)
    log.write("start", kind=str(connection.kind), dry_run=args.dry_run)
    started = time.perf_counter()
    async with MowerController(
        connection, mower_id=MOWER_ID, drive_calibration=lambda: stored
    ) as controller:
        connect_s = round(time.perf_counter() - started, 2)
        caps = await controller.capabilities()
        state = await connection.get_state()
        mower_map = await controller.mower_map()
        areas = area_summary(mower_map)
        show("capabilities", caps.model_dump(mode="json"))
        show("state", state_line(state))
        show(
            "map",
            {
                "reference": mower_map.reference.model_dump(),
                "areas": areas,
                "no_go_zones": len(mower_map.no_go_zones),
                "obstacles": len(mower_map.obstacles),
                "paths": len(mower_map.paths),
                "dock": mower_map.dock.model_dump() if mower_map.dock else None,
            },
        )
        log.write("connected", seconds_until_map=connect_s)
        log.write("capabilities", **caps.model_dump(mode="json"))
        log.write("state", **state_line(state))
        log.write("map", areas=areas, reference=mower_map.reference.model_dump())
        if args.camera:
            await show_camera(connection, log)
        if args.firmware_check:
            await show_firmware(connection, log)
        command: MowerCommand | None = None
        if args.turn_check:
            confirm(args, "turn check (turns in place, blades off)")
            settle_s = 0.0 if args.dry_run else 1.5
            wait_s = 0.0 if args.dry_run else args.wait
            return await run_turn_check(controller, log, settle_s=settle_s, wait_s=wait_s)
        if args.spot_dry_run:
            return await run_spot_dry_run(controller, args, log)
        if args.calibrate:
            return await run_calibration(controller, args, log)
        if args.get_speed or args.set_speed is not None:
            return await show_drive_speed(controller, connection, args, log)
        if args.bridge and not (args.start_area or args.dock or args.estop or args.light):
            await watch_links(connection, log, args.wait)
        if args.start_area:
            area_id = find_area(mower_map, args.start_area)
            command = StartMow(
                area_ids=[area_id], pattern={"name": args.pattern, "angle_deg": args.angle}
            )
        elif args.dock:
            command = ReturnToDock()
        elif args.estop:
            command = EmergencyStop()
        elif args.light:
            command = SetLight(on=args.light == "on")
        if command is None:
            return 0
        confirm(args, command.kind)
        envelope = CommandEnvelope.create(
            mower_id=MOWER_ID,
            account_id=ACCOUNT_ID,
            issued_by=ISSUER,
            command=command,
            ttl=timedelta(seconds=60),
        )
        sent = time.perf_counter()
        result = await controller.handle(envelope)
        took = round(time.perf_counter() - sent, 2)
        show("result", {"command": command.kind, "status": result.status, "reason": result.reason})
        log.write(
            "command",
            command=command.kind,
            status=str(result.status),
            reason=result.reason,
            seconds=took,
        )
        deadline = time.perf_counter() + (0 if args.dry_run else args.wait)
        while time.perf_counter() < deadline:
            await asyncio.sleep(2)
            line = state_line(await connection.get_state())
            print(
                f"state activity={line['activity']} battery={line['battery_percent']}% "
                f"rtk={line['rtk_fix']}/{line['rtk_source']} "
                f"position={line['position_source']} light={line['light_on']} "
                f"connection={line['connection']}{link_text(line)}"
            )
            log.write("state_after_command", **line)
        if isinstance(connection, MammotionBase) and args.dry_run:
            port = connection.port
            names = getattr(port, "names", None)
            if callable(names):
                show("dry run: PyMammotion commands sent", names())
    log.write("done")
    print(f"\nlog written to {args.log}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
