"""Maintenance command contracts (ADR 0048): the five command kinds, the capability list and
their place in the command union."""

from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    COMMAND_MODELS,
    MAINTENANCE_ACTIONS,
    MAINTENANCE_COMMAND_MODELS,
    CommandEnvelope,
    LeaveDock,
    MowerCapabilities,
    MowerCommand,
    PrincipalKind,
    PrincipalRef,
    RelocateDock,
    RestartMower,
    ResyncRtk,
    SyncMap,
)
from pydantic import TypeAdapter, ValidationError

ADAPTER: TypeAdapter[MowerCommand] = TypeAdapter(MowerCommand)


@pytest.mark.parametrize(
    ("kind", "model"),
    [
        ("restart_mower", RestartMower),
        ("sync_map", SyncMap),
        ("relocate_dock", RelocateDock),
        ("leave_dock", LeaveDock),
        ("resync_rtk", ResyncRtk),
    ],
)
def test_contracts_maintenance_kinds_parse(kind: str, model: type) -> None:
    command = ADAPTER.validate_python({"kind": kind})
    assert isinstance(command, model)
    assert command.model_dump() == {"kind": kind}


def test_contracts_maintenance_actions_match_the_models() -> None:
    kinds = tuple(model.model_fields["kind"].default for model in MAINTENANCE_COMMAND_MODELS)
    assert kinds == MAINTENANCE_ACTIONS
    assert set(MAINTENANCE_COMMAND_MODELS) <= set(COMMAND_MODELS)


def test_contracts_maintenance_command_travels_in_an_envelope() -> None:
    envelope = CommandEnvelope.create(
        mower_id=uuid4(),
        account_id=uuid4(),
        issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
        command=RestartMower(),
    )
    again = CommandEnvelope.model_validate_json(envelope.model_dump_json())
    assert isinstance(again.command, RestartMower)


def test_contracts_maintenance_capability_defaults_to_none_and_sorts() -> None:
    assert MowerCapabilities().maintenance == frozenset()
    caps = MowerCapabilities(undock=True, maintenance=frozenset(MAINTENANCE_ACTIONS))
    dumped = caps.model_dump(mode="json")
    assert dumped["maintenance"] == sorted(MAINTENANCE_ACTIONS)
    assert MowerCapabilities.model_validate_json(caps.model_dump_json()) == caps


def test_contracts_maintenance_capability_refuses_unknown_actions() -> None:
    with pytest.raises(ValidationError):
        MowerCapabilities.model_validate({"maintenance": ["self_destruct"]})


def test_contracts_maintenance_leave_dock_needs_undock() -> None:
    with pytest.raises(ValidationError, match="leave_dock needs undock"):
        MowerCapabilities(maintenance=frozenset({"leave_dock"}))
    assert (
        "leave_dock"
        in MowerCapabilities(undock=True, maintenance=frozenset({"leave_dock"})).maintenance
    )
