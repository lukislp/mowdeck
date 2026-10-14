"""Run the simulated mower for a few seconds and print its state, without the platform.

Usage::

    python -m mowdeck_control.demo --seconds 10 --time-scale 60 --areas front back

The demo builds a :class:`~mowdeck_control.SimulatedMower`, wraps it in a
:class:`~mowdeck_control.MowerController`, sends a ``StartMow`` envelope and prints one line per
interval.
"""

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from typing import TextIO
from uuid import UUID

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    MowerState,
    PrincipalKind,
    PrincipalRef,
    StartMow,
)

from mowdeck_control.controller import MowerController
from mowdeck_control.simulator import SimulatedMower, SimulationConfig

DEMO_PRINCIPAL = PrincipalRef(
    subject=UUID("00000000-0000-4000-8000-0000000000de"),
    kind=PrincipalKind.SERVICE,
    display_name="mowdeck-control demo",
)

Sleep = Callable[[float], Awaitable[None]]


def format_state(elapsed: float, state: MowerState) -> str:
    """One human readable line per state."""
    raw = state.raw
    position = f"{state.position.lat:.7f},{state.position.lon:.7f}" if state.position else "unknown"
    line = (
        f"t={elapsed:6.1f}s activity={state.activity:<9} battery={state.battery_percent:3d}% "
        f"local=({raw.get('x', 0.0):6.2f},{raw.get('y', 0.0):6.2f}) wgs84={position} "
        f"rtk={state.rtk_fix} blade={'on' if raw.get('blade_on') else 'off'} "
        f"phase={raw.get('phase')}"
    )
    if "progress_percent" in raw:
        line += f" progress={raw['progress_percent']}%"
    if state.error_code:
        line += f" error={state.error_code}"
    return line


async def run_demo(
    *,
    seconds: float,
    interval: float,
    time_scale: float,
    areas: Sequence[str],
    out: TextIO,
    sleep: Sleep = asyncio.sleep,
) -> int:
    """Run the demo; returns a process exit code."""
    mower = SimulatedMower(config=SimulationConfig(time_scale=time_scale))
    controller = MowerController(mower, mower_id=mower.mower_id)
    async with controller:
        envelope = CommandEnvelope.create(
            mower_id=mower.mower_id,
            account_id=mower.account_id,
            issued_by=DEMO_PRINCIPAL,
            command=StartMow(area_ids=list(areas)),
            ttl=timedelta(seconds=30),
        )
        result = await controller.handle(envelope)
        print(f"start_mow {result.status}: {result.reason}", file=out)
        if result.status not in ("executed", "accepted"):
            return 1
        elapsed = 0.0
        print(format_state(elapsed, await mower.get_state()), file=out)
        while elapsed + interval <= seconds + 1e-9:
            await sleep(interval)
            elapsed += interval
            print(format_state(elapsed, mower.tick(interval)), file=out)
    return 0


def main(argv: Sequence[str] | None = None, *, out: TextIO | None = None) -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(
        prog="python -m mowdeck_control.demo",
        description="Run the MowDeck simulated mower and print its state.",
    )
    parser.add_argument("--seconds", type=float, default=5.0, help="real seconds to run")
    parser.add_argument("--interval", type=float, default=0.5, help="seconds between lines")
    parser.add_argument(
        "--time-scale", type=float, default=30.0, help="simulated seconds per real second"
    )
    parser.add_argument("--areas", nargs="+", default=["front"], help="area ids to mow")
    args = parser.parse_args(argv)
    if args.seconds < 0 or args.interval <= 0 or args.time_scale <= 0:
        parser.error("--seconds must be >= 0, --interval and --time-scale > 0")
    return asyncio.run(
        run_demo(
            seconds=args.seconds,
            interval=args.interval,
            time_scale=args.time_scale,
            areas=args.areas,
            out=out or sys.stdout,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
