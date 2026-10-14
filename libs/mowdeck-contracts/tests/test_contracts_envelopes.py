"""Envelope validation rules and JSON round trips."""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandPriority,
    CommandResult,
    CommandStatus,
    EmergencyStop,
    EventEnvelope,
    ManualDrive,
    MowerCommand,
    PauseMow,
    PrincipalKind,
    PrincipalRef,
    SetLight,
    SetSpeed,
    StartMow,
)
from pydantic import BaseModel, TypeAdapter, ValidationError

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def issuer() -> PrincipalRef:
    return PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER, display_name="Anna")


def _envelope(issuer: PrincipalRef, command: Any, **extra: Any) -> CommandEnvelope:
    data: dict[str, Any] = {
        "mower_id": uuid4(),
        "account_id": uuid4(),
        "issued_by": issuer,
        "issued_at": T0,
        "expires_at": T0 + timedelta(seconds=30),
        "command": command,
        **extra,
    }
    return CommandEnvelope.model_validate(data)


class MowerRegistered(BaseModel):
    mower_id: str
    name: str


def test_event_envelope_defaults_and_typed_payload() -> None:
    event = EventEnvelope[MowerRegistered](
        type="mower.registered",
        producer="fleet",
        payload=MowerRegistered(mower_id="m1", name="Luba"),
    )
    assert event.version == 1
    assert event.occurred_at.tzinfo is UTC
    assert event.domain == "mower"
    assert event.name == "registered"
    restored = EventEnvelope[MowerRegistered].model_validate_json(event.model_dump_json())
    assert restored == event
    assert isinstance(restored.payload, MowerRegistered)


def test_event_envelope_generic_dict_payload_round_trip() -> None:
    event = EventEnvelope[dict[str, Any]](
        type="subscription.changed",
        producer="billing",
        account_id=uuid4(),
        causation_id=uuid4(),
        payload={"plan": "pro"},
    )
    restored = EventEnvelope[dict[str, Any]].model_validate_json(event.model_dump_json())
    assert restored == event


@pytest.mark.parametrize("bad_type", ["mower", "Mower.Registered", "mower..x", "mower.reg-x", ""])
def test_event_type_must_be_dotted_lowercase(bad_type: str) -> None:
    with pytest.raises(ValidationError):
        EventEnvelope[dict[str, Any]](type=bad_type, producer="fleet", payload={})


def test_event_version_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope[dict[str, Any]](type="mower.registered", version=0, producer="x", payload={})


def test_timestamps_are_normalised_to_utc() -> None:
    cest = timezone(timedelta(hours=2))
    event = EventEnvelope[dict[str, Any]](
        type="mower.registered",
        producer="fleet",
        occurred_at=datetime(2026, 10, 8, 14, 0, tzinfo=cest),
        payload={},
    )
    assert event.occurred_at == datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    assert event.occurred_at.utcoffset() == timedelta(0)


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        EventEnvelope[dict[str, Any]](
            type="mower.registered",
            producer="fleet",
            occurred_at=datetime(2026, 10, 8, 12, 0),
            payload={},
        )


def test_unknown_fields_are_ignored_for_forward_compatibility(issuer: PrincipalRef) -> None:
    envelope = _envelope(issuer, PauseMow())
    data = envelope.model_dump(mode="json")
    data["added_in_a_later_minor_revision"] = True
    assert CommandEnvelope.model_validate(data) == envelope


def test_models_are_immutable(issuer: PrincipalRef) -> None:
    envelope = _envelope(issuer, PauseMow())
    with pytest.raises(ValidationError):
        envelope.priority = CommandPriority.EMERGENCY


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"kind": "start_mow", "area_ids": ["a1"]}, StartMow),
        ({"kind": "pause_mow"}, PauseMow),
        ({"kind": "set_speed", "mps": 0.5}, SetSpeed),
        ({"kind": "set_light", "on": True}, SetLight),
        ({"kind": "manual_drive", "linear": 0.2, "angular": -0.1}, ManualDrive),
        ({"kind": "emergency_stop"}, EmergencyStop),
    ],
)
def test_command_discriminator(payload: dict[str, Any], expected: type[BaseModel]) -> None:
    command = TypeAdapter(MowerCommand).validate_python(payload)
    assert isinstance(command, expected)


def test_unknown_command_kind_is_rejected() -> None:
    with pytest.raises(ValidationError, match="does not match any of the expected tags"):
        TypeAdapter(MowerCommand).validate_python({"kind": "self_destruct"})


def test_priority_is_derived_from_command(issuer: PrincipalRef) -> None:
    assert _envelope(issuer, PauseMow()).priority is CommandPriority.NORMAL
    assert _envelope(issuer, EmergencyStop()).priority is CommandPriority.EMERGENCY
    assert _envelope(issuer, {"kind": "emergency_stop"}).priority is CommandPriority.EMERGENCY


def test_emergency_stop_with_normal_priority_is_rejected(issuer: PrincipalRef) -> None:
    with pytest.raises(ValidationError, match="emergency"):
        _envelope(issuer, EmergencyStop(), priority="normal")


def test_only_emergency_stop_may_use_emergency_priority(issuer: PrincipalRef) -> None:
    with pytest.raises(ValidationError, match="emergency"):
        _envelope(issuer, PauseMow(), priority="emergency")


def test_expiry_must_follow_issue_time(issuer: PrincipalRef) -> None:
    with pytest.raises(ValidationError, match="expires_at"):
        _envelope(issuer, PauseMow(), expires_at=T0)


def test_is_expired(issuer: PrincipalRef) -> None:
    envelope = _envelope(issuer, PauseMow())
    assert not envelope.is_expired(T0)
    assert not envelope.is_expired(T0 + timedelta(seconds=29))
    assert envelope.is_expired(T0 + timedelta(seconds=30))
    assert envelope.is_expired()  # T0 is in the past
    with pytest.raises(ValueError, match="timezone-aware"):
        envelope.is_expired(datetime(2026, 10, 8, 12, 0))


def test_create_helper(issuer: PrincipalRef) -> None:
    envelope = CommandEnvelope.create(
        mower_id=uuid4(),
        account_id=uuid4(),
        issued_by=issuer,
        command=EmergencyStop(),
        ttl=timedelta(seconds=5),
        correlation_id="req-123",
        now=T0,
    )
    assert envelope.priority is CommandPriority.EMERGENCY
    assert envelope.expires_at == T0 + timedelta(seconds=5)
    assert envelope.correlation_id == "req-123"
    fresh = CommandEnvelope.create(
        mower_id=uuid4(), account_id=uuid4(), issued_by=issuer, command=PauseMow()
    )
    assert not fresh.is_expired()
    assert fresh.correlation_id  # generated


def test_command_envelope_json_round_trip(issuer: PrincipalRef) -> None:
    envelope = _envelope(issuer, ManualDrive(linear=0.5, angular=0.0, blade_on=False))
    restored = CommandEnvelope.model_validate_json(envelope.model_dump_json())
    assert restored == envelope
    assert isinstance(restored.command, ManualDrive)


@pytest.mark.parametrize("bad", ["", "has space", "x" * 129, "line\nbreak"])
def test_correlation_id_format(issuer: PrincipalRef, bad: str) -> None:
    with pytest.raises(ValidationError):
        _envelope(issuer, PauseMow(), correlation_id=bad)


def test_command_result_round_trip() -> None:
    result = CommandResult(
        command_id=uuid4(), mower_id=uuid4(), status=CommandStatus.EXPIRED, reason="too late"
    )
    assert result.at.tzinfo is UTC
    assert CommandResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        CommandResult.model_validate(
            {"command_id": str(uuid4()), "mower_id": str(uuid4()), "status": "lost"}
        )
