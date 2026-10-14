"""Send a command to a mower through NATS and print the result and the telemetry (development).

Defaults match the bootstrap mower of ``infra/compose/.env.example``::

    uv run python services/device-gateway/scripts/send_command.py start --seconds 10
    uv run python services/device-gateway/scripts/send_command.py dock
    uv run python services/device-gateway/scripts/send_command.py stop

The command is published like Fleet will publish it later (``command.mower.<id>``, JetStream,
msg id = command id); the script then prints every ``command.result.mower.<id>`` and
``telemetry.mower.<id>`` message for ``--seconds`` seconds.
"""

import argparse
import asyncio
import os
from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    EmergencyStop,
    MowerCommand,
    MowerState,
    PauseMow,
    PrincipalKind,
    PrincipalRef,
    ResumeMow,
    ReturnToDock,
    StartMow,
)
from mowdeck_contracts.v1.subjects import command_result_subject, telemetry_subject
from mowdeck_control.simulator import DEFAULT_ACCOUNT_ID, DEFAULT_MOWER_ID
from mowdeck_service_kit.bus import BusMessage, JetStreamBus, publish_command

COMMANDS = ("start", "pause", "resume", "dock", "stop")


def build_command(name: str, areas: Sequence[str]) -> MowerCommand:
    """The contract command for a command line name."""
    match name:
        case "start":
            return StartMow(area_ids=list(areas))
        case "pause":
            return PauseMow()
        case "resume":
            return ResumeMow()
        case "dock":
            return ReturnToDock()
        case _:
            return EmergencyStop()


def format_state(state: MowerState) -> str:
    """One telemetry line."""
    raw = state.raw
    progress = f" progress={raw['progress_percent']}%" if "progress_percent" in raw else ""
    position = f" x={raw.get('x')} y={raw.get('y')}" if "x" in raw else ""
    return (
        f"telemetry activity={state.activity} battery={state.battery_percent}%"
        f" blade={raw.get('blade_on', '?')}{position}{progress}"
    )


async def send(args: argparse.Namespace) -> None:
    """Publish the command and print results and telemetry for ``args.seconds``."""
    bus = JetStreamBus(args.nats_url, name="device-gateway-send-command")
    await bus.connect()

    async def on_result(message: BusMessage) -> None:
        result = message.model(CommandResult)
        reason = f" ({result.reason})" if result.reason else ""
        print(f"result command={result.command_id} status={result.status}{reason}", flush=True)

    async def on_state(message: BusMessage) -> None:
        print(format_state(message.model(MowerState)), flush=True)

    try:
        await bus.subscribe(command_result_subject(args.mower_id), on_result)
        await bus.subscribe(telemetry_subject(args.mower_id), on_state)
        envelope = CommandEnvelope.create(
            mower_id=args.mower_id,
            account_id=args.account_id,
            issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
            command=build_command(args.command, args.areas),
            ttl=timedelta(seconds=args.ttl),
        )
        await publish_command(bus, envelope)
        print(f"sent {args.command} command={envelope.command_id} to mower {args.mower_id}")
        await asyncio.sleep(args.seconds)
    finally:
        await bus.close()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument(
        "--nats-url", default=os.environ.get("MOWDECK_NATS_URL", "nats://127.0.0.1:4222")
    )
    parser.add_argument("--mower-id", type=UUID, default=DEFAULT_MOWER_ID)
    parser.add_argument("--account-id", type=UUID, default=DEFAULT_ACCOUNT_ID)
    parser.add_argument("--areas", nargs="+", default=["front"], help="areas for start")
    parser.add_argument("--seconds", type=float, default=5.0, help="how long to print messages")
    parser.add_argument("--ttl", type=float, default=30.0, help="command expiry in seconds")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point."""
    asyncio.run(send(parse_args(argv)))


if __name__ == "__main__":
    main()
