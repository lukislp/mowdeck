"""Mower reports through the Mammotion adapter (libs/mowdeck-control-mammotion).

The mower (over the cloud or the Bluetooth bridge) sends protobuf ``LubaMsg`` frames; a corrupt
or hostile frame is still decoded by PyMammotion as far as it goes. The fuzz bytes are a few
length-prefixed frames, applied in order like ``DeviceHandle`` does (PyMammotion's own decode
and reducer failures are its business and skipped). Properties of the adapter's mapping on the
resulting device, which runs on every update of a live mower:

* ``state_of``, ``map_of``, ``capabilities_of``, ``mow_path_of`` and ``area_summary`` never
  raise: a report that maps to an invalid contract would stop the mower's state updates;
* a mapped position lies within WGS84 bounds, the battery within 0..100 %.
"""

import sys
from datetime import UTC, datetime
from uuid import UUID

import atheris

with atheris.instrument_imports():
    from mowdeck_contracts.v1 import ConnectionKind
    from mowdeck_control_mammotion import mapping
    from mowdeck_control_mammotion.port import MowerDevice
    from pymammotion.device.state_reducer import MowerStateReducer
    from pymammotion.proto import (
        LubaMsg,
        MctlSys,
        ReportInfoData,
        RptConnectStatus,
        RptDevLocation,
        RptDevStatus,
        RptRtk,
        RptWork,
        SystemUpdateBufMsg,
    )

from harness import Input

NAME = "Luba-VS123456"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
REDUCER = MowerStateReducer()


def _frames(inp: Input) -> list[bytes]:
    frames = []
    for _ in range(inp.int_in(1, 4)):
        size = inp.uint(2)
        frames.append(inp.take(size))
    return frames


def _device(frames: list[bytes]) -> MowerDevice:
    device = MowerDevice(name=NAME)
    for frame in frames:
        try:
            message = LubaMsg().parse(frame)
            device = REDUCER.apply(device, message)
        except Exception:  # noqa: S112 - PyMammotion drops frames it cannot apply
            continue
    return device


def _check(device: MowerDevice, since: datetime | None) -> None:
    state = mapping.state_of(
        device,
        mower_id=UUID(int=1),
        account_id=UUID(int=2),
        connection=ConnectionKind.CLOUD,
        device_name=NAME,
        firmware="1.0.0",
        observed_at=NOW,
        fault_since=since,
    )
    assert 0 <= state.battery_percent <= 100
    if state.position is not None:
        assert -90 <= state.position.lat <= 90
        assert -180 <= state.position.lon <= 180
    mower_map = mapping.map_of(device)
    mapping.area_summary(mower_map)
    mapping.capabilities_of(
        device,
        device_name=NAME,
        product_key="",
        manual_drive=True,
        manual_drive_blades=False,
    )
    mapping.mow_path_of(device, planned_at=NOW, complete_only=False)


def test_one_input(data: bytes) -> None:
    inp = Input(data)
    since = NOW if inp.boolean() else None
    _check(_device(_frames(inp)), since)


def _report(**fields: object) -> bytes:
    data = ReportInfoData(
        dev=RptDevStatus(sys_status=13, charge_state=0, battery_val=76, self_check_status=0),
        rtk=RptRtk(status=4, gps_stars=31, age=1),
        locations=[RptDevLocation(real_pos_x=120000, real_pos_y=-45000, real_toward=9000)],
        work=RptWork(man_run_speed=40, knife_height=55, progress=0x00320064),
        connect=RptConnectStatus(wifi_rssi=-60, mnet_rssi=-90),
        **fields,  # type: ignore[arg-type]
    )
    return bytes(LubaMsg(sys=MctlSys(toapp_report_data=data)))


def seeds() -> list[bytes]:
    """A mowing report, a docked report and the RTK origin and dock buffer."""
    mowing = _report()
    docked = bytes(
        LubaMsg(
            sys=MctlSys(
                toapp_report_data=ReportInfoData(
                    dev=RptDevStatus(sys_status=11, charge_state=1, battery_val=100)
                )
            )
        )
    )
    origin = bytes(
        LubaMsg(
            sys=MctlSys(
                system_update_buf=SystemUpdateBufMsg(
                    update_buf_data=[1, 0, 1, 838, 200, 1, 0, 0, 2, 5, 3]
                )
            )
        )
    )

    def framed(*frames: bytes) -> bytes:
        body = b"".join(len(f).to_bytes(2, "big") + f for f in frames)
        return b"\x01" + bytes([len(frames) - 1]) + body

    return [framed(mowing), framed(docked, origin), framed(origin, mowing, mowing)]


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
