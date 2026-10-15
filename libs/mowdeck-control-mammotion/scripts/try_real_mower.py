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

Without hardware or credentials (fake PyMammotion port)::

    uv run python .../try_real_mower.py --dry-run --start-area "Front lawn" --estop

The script never drives the mower manually and never switches blades on outside a mowing job.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    EmergencyStop,
    MowerCommand,
    MowerMap,
    MowerState,
    PrincipalKind,
    PrincipalRef,
    ReturnToDock,
    SetLight,
    StartMow,
)
from mowdeck_control import (
    ConnectionConfig,
    MowerConnection,
    MowerController,
    NotConnected,
    camera_session_of,
    firmware_port_of,
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
  * The script never drives the mower manually and never enables blades outside a job.
  * --estop sends pause, zero movement and blades off with emergency priority; the
    physical STOP button is still the only guaranteed stop."""

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
    return parser.parse_args(argv)


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


async def run(args: argparse.Namespace) -> int:
    print(SAFETY_NOTICE)
    log = JsonLog(args.log)
    if args.scan:
        return await run_scan(args, log)
    connection = build_connection(args)
    log.write("start", kind=str(connection.kind), dry_run=args.dry_run)
    started = time.perf_counter()
    async with MowerController(connection, mower_id=MOWER_ID) as controller:
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
