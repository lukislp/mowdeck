"""Connection options of the real Mammotion adapters: validation per kind and secret masking."""

import base64
from typing import Any
from uuid import UUID, uuid4

import pytest
from fleet_cloud import serve_providers
from fleet_support import API, harness
from mowdeck_contracts.v1 import EventEnvelope, MowerCredentialsRequest, MowerRegistration

from fleet.domain.connections import LEGACY_CLOUD_SCHEMA, MAC_PATTERN, SCHEMAS, option_problems
from fleet.domain.errors import InvalidInput
from fleet.domain.rules import (
    SECRET_MASK,
    keep_masked_secrets,
    mask_connection_options,
    plain_secret_options,
    validate_connection_options,
)

PSK = base64.b64encode(bytes(32)).decode()
PASSWORD = "hunter2"  # noqa: S105 - test value
CLOUD: dict[str, Any] = {
    "account": "second@example.com",
    "credential_ref": "env:MAMMOTION_PASSWORD",
    "device_name": "Luba-VP1234",
}
BLE: dict[str, Any] = {"host": "bridge.local", "noise_psk": PSK, "ble_address": "AA:BB:CC:DD:EE:FF"}


def test_fleet_cloud_options_need_account_device_and_a_credential() -> None:
    """Old cloud mowers (own account, no linked account) keep the old schema."""
    legacy = LEGACY_CLOUD_SCHEMA
    assert validate_connection_options(CLOUD, 4096, "cloud", legacy) == CLOUD
    with_password = {**CLOUD, "password": "plain"}
    with_password.pop("credential_ref")
    assert validate_connection_options(with_password, 4096, "cloud", legacy) == with_password
    by_iot = {"account": "a", "iot_id": "iot-1", "credential_ref": "file:/run/secrets/m"}
    assert validate_connection_options(by_iot, 4096, "cloud", legacy) == by_iot
    for bad, match in [
        ({"account": "a", "credential_ref": "env:X"}, "device_name or iot_id"),
        ({"device_name": "x", "credential_ref": "env:X"}, "account is required"),
        ({"account": "a", "device_name": "x"}, "credential_ref or password"),
        ({**CLOUD, "api_token": "t"}, "secrets"),  # other secret keys stay forbidden
        ({**CLOUD, "nested": {"password": "p"}}, "secrets"),  # only top level is allowed
    ]:
        with pytest.raises(InvalidInput, match=match):
            validate_connection_options(bad, 4096, "cloud", legacy)


def test_fleet_ble_bridge_options() -> None:
    assert validate_connection_options({**BLE, "port": 6053}, 4096, "ble_bridge")["port"] == 6053
    by_ref = {"host": "h", "credential_ref": "env:PSK", "ble_address": "aa:bb:cc:dd:ee:ff"}
    assert validate_connection_options(by_ref, 4096, "ble_bridge") == by_ref
    for bad, match in [
        ({**BLE, "host": ""}, "host is required"),
        ({k: v for k, v in BLE.items() if k != "noise_psk"}, "noise_psk"),
        ({**BLE, "ble_address": "bridge"}, "MAC address"),
        ({**BLE, "port": 0}, "port must be"),
        ({**BLE, "port": "6053"}, "port must be"),
        ({**BLE, "port": True}, "port must be"),
    ]:
        with pytest.raises(InvalidInput, match=match):
            validate_connection_options(bad, 4096, "ble_bridge")


def test_fleet_simulated_options_still_reject_every_secret() -> None:
    for bad in ({"password": "p"}, {"noise_psk": PSK}, {"credential_ref": "env:X"}):
        with pytest.raises(InvalidInput, match="secrets"):
            validate_connection_options(bad, 4096, "simulated")


def test_fleet_masking_and_keeping_secrets() -> None:
    options = {**CLOUD, "password": "plain", "noise_psk": PSK}
    masked = mask_connection_options(options)
    assert masked["password"] == SECRET_MASK and masked["noise_psk"] == SECRET_MASK
    assert masked["credential_ref"] == CLOUD["credential_ref"]  # a reference, not a secret
    assert mask_connection_options({"password": ""}) == {"password": ""}
    assert plain_secret_options(options) == ["noise_psk", "password"]
    assert plain_secret_options(CLOUD) == []
    kept = keep_masked_secrets({**masked, "device_name": "Luba-VP9"}, options)
    assert (kept["password"], kept["noise_psk"]) == ("plain", PSK)
    assert kept["device_name"] == "Luba-VP9"
    assert keep_masked_secrets({"password": "new"}, options) == {"password": "new"}


async def test_fleet_api_never_echoes_connection_secrets(capfd: pytest.CaptureFixture[str]) -> None:
    """Secrets never leave Fleet in an API response or a registration (ADR 0034): events and the
    list reply carry ``credentials: "fleet"`` instead; the gateway fetches them on request."""
    async with harness(allowed_connection_kinds="simulated,cloud,ble_bridge") as h:
        owner = await h.owner()
        hdr = owner.headers
        # A cloud mower registered before linked accounts, with its own account and password.
        sim = await h.register(owner)
        old = h.store.tables.mowers.rows[UUID(sim["id"])]
        old.connection_kind = "cloud"
        old.connection_options = {**CLOUD, "password": PASSWORD}
        # New cloud mowers come from a linked account only.
        plain = await h.client.post(
            f"{API}/mowers",
            headers=hdr,
            json={"name": "x", "connection_kind": "cloud", "connection_options": CLOUD},
        )
        ble = await h.register(owner, connection_kind="ble_bridge", connection_options=BLE)
        listed = await h.client.get(f"{API}/mowers", headers=hdr)
        read = await h.client.get(f"{API}/mowers/{old.id}", headers=hdr)
        echoed_back = await h.client.patch(
            f"{API}/mowers/{old.id}",
            headers=hdr,
            json={"connection_options": {**read.json()["connection_options"], "device_name": "L2"}},
        )
        new_password = await h.client.patch(
            f"{API}/mowers/{old.id}",
            headers=hdr,
            json={"connection_options": {**read.json()["connection_options"], "password": "new"}},
        )
        new_iot = await h.client.patch(
            f"{API}/mowers/{old.id}",
            headers=hdr,
            json={"connection_options": {**read.json()["connection_options"], "iot_id": "x"}},
        )
        to_cloud = await h.client.patch(
            f"{API}/mowers/{ble['id']}",
            headers=hdr,
            json={"connection_kind": "cloud", "connection_options": CLOUD},
        )
        registered = [
            m.model(EventEnvelope[MowerRegistration]).payload
            for m in h.bus.messages("event.mower.registered")
        ]
        updated = [
            m.model(EventEnvelope[MowerRegistration]).payload
            for m in h.bus.messages("event.mower.updated")
        ]
        shard = await h.app.state.fleet.mowers.registrations_for_shard(0, 1)
        fetch = h.app.state.fleet.linked_accounts.credentials
        old_secrets = await fetch(
            MowerCredentialsRequest(mower_id=old.id, account_id=old.account_id)
        )
        ble_secrets = await fetch(
            MowerCredentialsRequest(mower_id=UUID(ble["id"]), account_id=owner.account_id)
        )
        foreign = await fetch(MowerCredentialsRequest(mower_id=old.id, account_id=uuid4()))
        stored = h.store.tables.mowers.rows[old.id].connection_options
    for body in (ble, read.json(), echoed_back.json(), listed.text):
        assert PASSWORD not in str(body) and PSK not in str(body)
    assert read.json()["connection_options"]["password"] == SECRET_MASK
    assert ble["connection_options"]["noise_psk"] == SECRET_MASK
    assert echoed_back.status_code == 200
    assert echoed_back.json()["connection_options"]["device_name"] == "L2"
    assert stored["password"] == PASSWORD  # the mask was not stored
    assert plain.status_code == 422
    assert {e["pointer"] for e in plain.json()["errors"]} >= {"#/linked_account_id"}
    # PATCH applies the plain password ban and keeps the IoT id Fleet-managed.
    assert new_password.status_code == 422
    assert new_password.json()["errors"][0]["code"] == "use_linked_account"
    assert new_iot.status_code == 422 and to_cloud.status_code == 422
    # No secret in any registration: events, the list reply ...
    for payload in [*registered, *updated, *shard]:
        options = payload.connection_options
        assert not {"password", "noise_psk", "account"} & set(options), options
        assert PASSWORD not in repr(payload) and PSK not in repr(payload)
    assert updated[0].connection_options["credentials"] == "fleet"
    assert registered[-1].needs_credentials  # the BLE bridge
    # ... only the credentials request answers them, for the mower's own account.
    assert old_secrets.secret_options == {"account": CLOUD["account"], "password": PASSWORD}
    assert ble_secrets.secret_options == {"noise_psk": PSK}
    assert not foreign.found and foreign.secret_options == {}
    assert PASSWORD not in repr(old_secrets)
    logs = "".join(capfd.readouterr())
    assert PASSWORD not in logs and PSK not in logs


def test_fleet_option_schema_checks_types_ranges_and_patterns() -> None:
    schema = SCHEMAS["ble_bridge"]
    assert option_problems(schema, BLE) == []
    found = option_problems(
        schema,
        {
            "host": "h",
            "noise_psk": PSK,
            "ble_address": "AA:BB:CC:DD:EE:FF",
            "allow_manual_blades": "yes",
            "scan_timeout_s": 0.5,
            "manual_blade_height_mm": 60,
        },
    )
    assert [(p.key, p.code) for p in found] == [
        ("allow_manual_blades", "type"),
        ("scan_timeout_s", "range"),
    ]
    assert found[1].message == "scan_timeout_s must be a number between 1 and 600"
    cloud = option_problems(LEGACY_CLOUD_SCHEMA, {"account": "a", "credential_ref": "vault:x"})
    assert [(p.key, p.code) for p in cloud] == [
        ("credential_ref", "pattern"),
        ("device_name", "one_of"),
    ]
    assert option_problems(SCHEMAS["simulated"], {"time_scale": 30}) == []


def test_fleet_option_errors_name_the_fields() -> None:
    with pytest.raises(InvalidInput) as raised:
        validate_connection_options({"host": "", "ble_address": "x"}, 4096, "ble_bridge")
    assert [(f.path, f.code) for f in raised.value.fields] == [
        ("connection_options/host", "required"),
        ("connection_options/noise_psk", "one_of"),
        ("connection_options/ble_address", "pattern"),
    ]


async def test_fleet_api_lists_the_allowed_connection_kinds_with_their_schema() -> None:
    async with harness(allowed_connection_kinds="ble_bridge,simulated,wifi,cloud") as h:
        await serve_providers(h.bus)
        owner = await h.owner()
        anonymous = await h.client.get(f"{API}/connection-kinds")
        response = await h.client.get(f"{API}/connection-kinds", headers=owner.headers)
        invalid = await h.client.post(
            f"{API}/mowers",
            headers=owner.headers,
            json={
                "name": "Bridge",
                "connection_kind": "ble_bridge",
                "connection_options": {"host": "bridge.local", "ble_address": "nope"},
            },
        )
    assert anonymous.status_code == 401
    assert response.status_code == 200
    items = response.json()["items"]
    assert [i["kind"] for i in items] == ["simulated", "cloud", "ble_bridge", "wifi"]
    simulated, cloud, ble, wifi = items
    assert simulated["options"] == [] and simulated["extra_options"] is True
    assert wifi["options"] == [] and wifi["extra_options"] is True
    options = {o["key"]: o for o in cloud["options"]}
    # Cloud mowers come from a linked account: no credentials in the options any more, and
    # only the keys Fleet manages; the rest is what the provider declares (ADR 0066).
    assert cloud["linked_account"] is True and simulated["linked_account"] is False
    assert not {"account", "password", "credential_ref"} & set(options)
    assert all(o["managed"] for o in options.values())
    assert {"device_name", "iot_id", "cloud_provider"} <= set(options)
    assert ble["provider_options"] == {}
    acme = {o["key"]: o for o in cloud["provider_options"]["acme"]}
    assert acme["session_cache_path"]["advanced"] is True
    assert acme["session_cache_path"]["managed"] is False
    refresh = acme["refresh_seconds"]
    assert refresh["advanced"] is True and refresh["type"] == "number"
    assert (refresh["minimum"], refresh["maximum"], refresh["default"]) == (10, 300, 20)
    stream = acme["report_stream_seconds"]
    assert (stream["minimum"], stream["maximum"], stream["default"]) == (0, 300, 60)
    assert cloud["groups"] == []
    ble_options = {o["key"]: o for o in ble["options"]}
    assert ble_options["port"]["default"] == 6053
    assert ble_options["noise_psk"]["secret"] is True
    assert ble_options["ble_address"]["pattern"] == MAC_PATTERN
    assert ble_options["allow_manual_blades"]["type"] == "boolean"
    assert invalid.status_code == 422
    pointers = {e["pointer"]: e["code"] for e in invalid.json()["errors"]}
    assert pointers == {
        "#/connection_options/noise_psk": "one_of",
        "#/connection_options/ble_address": "pattern",
    }


async def test_fleet_api_names_a_kind_that_is_not_enabled() -> None:
    async with harness() as h:
        owner = await h.owner()
        refused = await h.client.post(
            f"{API}/mowers",
            headers=owner.headers,
            json={"name": "Cloud", "connection_kind": "cloud", "connection_options": CLOUD},
        )
        kinds = await h.client.get(f"{API}/connection-kinds", headers=owner.headers)
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["pointer"] == "#/connection_kind"
    assert [i["kind"] for i in kinds.json()["items"]] == ["simulated"]
