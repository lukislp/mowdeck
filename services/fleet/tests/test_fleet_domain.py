"""Pure rules: relation per command kind, expiry, status rank, shards, connection options."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_authz import Relation
from mowdeck_contracts.v1 import COMMAND_MODELS, CommandState

from fleet.domain.errors import InvalidInput, LinkNotApplicableError
from fleet.domain.rules import (
    COMMAND_RELATION,
    CommandLimits,
    advances_from,
    bulk_gate_relation,
    clean_name,
    command_ttl,
    in_shard,
    mask_link_options,
    relation_for,
    require_link_supported,
    validate_connection_options,
    validate_link_options,
    validate_timezone,
)

EXPECTED = {
    "start_mow": Relation.CAN_CONTROL,
    "pause_mow": Relation.CAN_CONTROL,
    "resume_mow": Relation.CAN_CONTROL,
    "return_to_dock": Relation.CAN_CONTROL,
    "set_blade_height": Relation.CAN_CONTROL,
    "set_speed": Relation.CAN_CONTROL,
    "set_light": Relation.CAN_CONTROL,
    "firmware_update": Relation.CAN_MANAGE_MOWERS,
    "spot_mow": Relation.CAN_DRIVE,
    "spot_stop": Relation.CAN_CONTROL,
    "manual_drive": Relation.CAN_DRIVE,
    "drive_calibrate": Relation.CAN_DRIVE,
    "set_drive_speed": Relation.CAN_DRIVE,
    "set_setting": Relation.CAN_MANAGE_MOWERS,
    "emergency_stop": Relation.CAN_EMERGENCY_STOP,
}


@pytest.mark.parametrize(("kind", "relation"), sorted(EXPECTED.items()))
def test_fleet_relation_for_each_command_kind(kind: str, relation: Relation) -> None:
    assert relation_for(kind) == relation
    assert Relation(relation_for(kind)) is relation  # a computed relation of the model


def test_fleet_every_contract_command_has_a_relation() -> None:
    kinds = {model.model_fields["kind"].default for model in COMMAND_MODELS}
    assert kinds == set(COMMAND_RELATION)
    with pytest.raises(ValueError, match="unknown command kind"):
        relation_for("self_destruct")


def test_fleet_bulk_gate_needs_bulk_rights_except_for_emergency_stop() -> None:
    assert bulk_gate_relation("emergency_stop") == Relation.CAN_EMERGENCY_STOP
    for kind in ("start_mow", "return_to_dock", "manual_drive"):
        assert bulk_gate_relation(kind) == Relation.CAN_BULK_COMMAND


def test_fleet_command_ttl_rules() -> None:
    limits = CommandLimits(
        default_ttl=timedelta(seconds=30),
        max_ttl=timedelta(seconds=120),
        emergency_stop_ttl=timedelta(seconds=10),
        manual_drive_ttl=timedelta(seconds=2),
    )
    assert command_ttl("start_mow", None, limits) == timedelta(seconds=30)
    assert command_ttl("start_mow", 60, limits) == timedelta(seconds=60)
    assert command_ttl("start_mow", 3600, limits) == timedelta(seconds=120)  # capped
    assert command_ttl("emergency_stop", 300, limits) == timedelta(seconds=10)  # always short
    assert command_ttl("manual_drive", 300, limits) == timedelta(seconds=2)
    tight = CommandLimits(default_ttl=timedelta(seconds=30), max_ttl=timedelta(seconds=5))
    assert command_ttl("pause_mow", None, tight) == timedelta(seconds=5)


def test_fleet_status_rank_only_moves_forward() -> None:
    assert advances_from(CommandState.PENDING) == frozenset()
    assert advances_from(CommandState.ACCEPTED) == {"pending"}
    for final in ("executed", "rejected", "expired", "failed"):
        assert advances_from(final) == {"pending", "accepted"}


def test_fleet_shard_rule_partitions_all_mowers() -> None:
    ids = [uuid4() for _ in range(200)]
    shards = [{m for m in ids if in_shard(m, i, 3)} for i in range(3)]
    assert set().union(*shards) == set(ids)
    assert sum(len(s) for s in shards) == len(ids)  # disjoint
    assert in_shard(UUID(int=7), 1, 3)  # 7 % 3 == 1, same rule as the Device-Gateway
    assert all(in_shard(m, 0, 1) for m in ids)


def test_fleet_connection_options_are_small_and_without_secrets() -> None:
    assert validate_connection_options({"time_scale": 30}, 4096) == {"time_scale": 30}
    with pytest.raises(InvalidInput, match="exceed"):
        validate_connection_options({"blob": "x" * 100}, 50)
    secret_options: list[dict[str, Any]] = [
        {"password": "x"},
        {"nested": {"api_key": "x"}},
        {"list": [{"Token": "x"}]},
        {"Credentials": {}},
        {"client_secret": 1},
    ]
    for bad in secret_options:
        with pytest.raises(InvalidInput, match="secrets"):
            validate_connection_options(bad, 4096)


def test_fleet_timezone_and_names() -> None:
    assert validate_timezone("Europe/Berlin") == "Europe/Berlin"
    for bad in ("Mars/Olympus", "", "../etc/passwd"):
        with pytest.raises(InvalidInput):
            validate_timezone(bad)
    assert clean_name("  Front lawn ", "mower") == "Front lawn"
    with pytest.raises(InvalidInput):
        clean_name("   ", "site")
    with pytest.raises(InvalidInput):
        clean_name("x" * 101, "group")


def test_fleet_link_rules_split_secrets_and_follow_the_registry() -> None:
    options = {"host": "b.local", "noise_psk": "k", "ble_address": "AA:BB:CC:DD:EE:FF", "port": 1}
    public, secret = validate_link_options("ble_bridge", options, 4096)
    assert secret == {"noise_psk": "k"} and "noise_psk" not in public
    assert public["host"] == "b.local" and public["port"] == 1
    assert mask_link_options("ble_bridge", public)["noise_psk"] == "***"
    with pytest.raises(InvalidInput) as missing:
        validate_link_options("ble_bridge", {"host": "b.local"}, 4096)
    assert {f.path for f in missing.value.fields} >= {
        "links/ble_bridge/noise_psk",
        "links/ble_bridge/ble_address",
    }
    with pytest.raises(InvalidInput, match="exceed"):
        validate_link_options("ble_bridge", {**options, "device_name": "x" * 100}, 64)
    assert require_link_supported("cloud", "ble_bridge").kind == "ble_bridge"
    for connection_kind, link_kind in (("simulated", "ble_bridge"), ("cloud", "wifi")):
        with pytest.raises(LinkNotApplicableError):
            require_link_supported(connection_kind, link_kind)
