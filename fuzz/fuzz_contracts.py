"""Wire models of libs/mowdeck-contracts (v1): what services accept from the network.

Realtime validates every WebSocket frame of a browser, Fleet the command bodies of the API,
and every consumer the JSON of NATS messages with these models. Properties, for each model:

* ``model_validate_json`` raises nothing but ``ValidationError`` for any bytes (a different
  exception is an HTTP 500 or a crashed consumer instead of a rejected message);
* an accepted message with finite numbers survives the trip a service makes when it passes it
  on: dumped to JSON and validated again it is accepted and equal (JSON mode and Python mode).
"""

import math
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import atheris

with atheris.instrument_imports():
    from mowdeck_contracts import v1
    from mowdeck_contracts.v1.realtime import RealtimeClientMessage
    from pydantic import BaseModel, ValidationError

from harness import Input

MODELS: list[type[BaseModel]] = [
    RealtimeClientMessage,
    v1.CommandRequest,
    v1.CommandEnvelope,
    v1.CommandResult,
    v1.MowerState,
    v1.MowerMap,
    v1.MowerCapabilities,
    v1.SpotMowPlan,
    v1.MowPath,
    v1.MowerRegistration,
    v1.DriveStartRequest,
]


def _non_finite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_non_finite(v) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_non_finite(v) for v in value)
    return False


def _check(model: type[BaseModel], raw: bytes) -> None:
    try:
        message = model.model_validate_json(raw)
    except ValidationError:
        return
    if _non_finite(message.model_dump()):
        # Pydantic writes NaN and the infinities as null (and NaN != NaN), so the round trip
        # cannot hold for them; fields that must be finite say so with bounds.
        return
    dumped = message.model_dump_json()
    assert model.model_validate_json(dumped) == message, dumped
    assert model.model_validate(message.model_dump(mode="json")) == message


def test_one_input(data: bytes) -> None:
    inp = Input(data)
    model = MODELS[inp.uint(1) % len(MODELS)]
    _check(model, inp.rest())


def seeds() -> list[bytes]:
    """One typical message per model, built with the models themselves."""
    mower = UUID("3f2c1b0a-0000-4000-8000-00000000abcd")
    account = UUID("3f2c1b0a-0000-4000-8000-00000000a001")
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    principal = v1.PrincipalRef(subject=UUID(int=5), kind=v1.PrincipalKind.USER)
    segment = v1.SpotSegment.from_points([(0.0, 0.0), (4.5, 0.25)], blades_on=True, speed_mps=0.3)
    spot_plan = v1.SpotMowPlan.build([segment], ["stripes"])
    start = v1.StartMow(spot_plan=spot_plan)
    reference = v1.GeoReference(origin_lat=48.1, origin_lon=11.5, rotation_deg=12.5)
    area = v1.MapArea(id="1", name="Lawn", boundary=[(0, 0), (10, 0), (10, 5)])
    messages: list[tuple[int, str]] = [
        (0, '{"type":"auth","token":"eyJ.x.y"}'),
        (0, '{"type":"subscribe","mower_ids":["' + str(mower) + '"],"request_id":"r1"}'),
        (0, '{"type":"drive_frame","session_id":"' + str(mower) + '","linear":0.5,"angular":0}'),
        (1, v1.CommandRequest(command=start, ttl_seconds=60).model_dump_json()),
        (1, '{"command":{"kind":"set_blade_height","mm":45}}'),
        (
            2,
            v1.CommandEnvelope(
                mower_id=mower,
                account_id=account,
                issued_by=principal,
                issued_at=now,
                expires_at=now + timedelta(minutes=5),
                priority=v1.CommandPriority.NORMAL,
                command=start,
            ).model_dump_json(),
        ),
        (
            3,
            v1.CommandResult(
                command_id=UUID(int=9), mower_id=mower, status=v1.CommandStatus.EXECUTED
            ).model_dump_json(),
        ),
        (
            4,
            '{"mower_id":"' + str(mower) + '","account_id":"' + str(account) + '",'
            '"observed_at":"2026-10-21T12:00:00+02:00","activity":"mowing",'
            '"battery_percent":80,"position":{"lat":48.1,"lon":11.5,"heading":90.0},'
            '"rtk_fix":"fixed","connection":"cloud"}',
        ),
        (5, v1.MowerMap(reference=reference, areas=[area]).model_dump_json()),
        (6, v1.MowerCapabilities().model_dump_json()),
        (7, spot_plan.model_dump_json()),
        (8, v1.MowPath(path_hash="h", points_total=0, planned_at=now).model_dump_json()),
        (
            9,
            v1.MowerRegistration(
                mower_id=mower,
                account_id=account,
                name="Luba",
                connection_kind=v1.ConnectionKind.CLOUD,
            ).model_dump_json(),
        ),
        (
            10,
            v1.DriveStartRequest(
                on_behalf_of=principal, account_id=account, mower_id=mower, max_seconds=60
            ).model_dump_json(),
        ),
    ]
    return [bytes([index]) + text.encode("utf-8") for index, text in messages]


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
