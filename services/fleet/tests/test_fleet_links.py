"""Additional links of a mower (ADR 0038): the Bluetooth bridge next to a cloud connection.

Eligibility comes from the connection-kind registry (``supports_links``), the key is stored
encrypted and never leaves Fleet but in ``fleet.mowers.credentials``, scans and firmware checks
are relayed to a fake Device-Gateway on the in-memory bus. No manufacturer package is imported:
the cloud mower comes from a fake vendor cloud, the refused mower is the simulator kind.
"""

import base64
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fleet_support import API, Harness, Person, harness
from mowdeck_contracts.v1 import (
    BridgeScanError,
    BridgeScanErrorCode,
    BridgeScanReply,
    BridgeScanRequest,
    CloudDiscoverReply,
    CloudDiscoverRequest,
    DiscoveredDevice,
    EventEnvelope,
    FirmwareCheckError,
    FirmwareCheckErrorCode,
    FirmwareCheckReply,
    FirmwareCheckRequest,
    FirmwareInfo,
    MowerCredentialsReply,
    MowerCredentialsRequest,
    MowerRegistration,
    ProxyInfo,
    ScannedDevice,
)
from mowdeck_contracts.v1.subjects import (
    ALL_BRIDGE_SCANS,
    ALL_FIRMWARE_CHECKS,
    FLEET_MOWERS_CREDENTIALS,
    GATEWAY_CLOUD_DISCOVER,
)
from mowdeck_service_kit.bus import BusMessage, InMemoryBus

from fleet.application.links import LINK_REMOVED, LINK_SET
from fleet.domain.connections import SCHEMAS, links_supported

KEY = base64.b64encode(b"k" * 32).decode()
PSK = base64.b64encode(b"p" * 32).decode()
NEW_PSK = base64.b64encode(b"q" * 32).decode()
EMAIL = "garden@example.com"
PASSWORD = "mammotion-secret"  # noqa: S105 - a test value
DEVICE = "Luba-VP0001"
MAC = "FC:23:00:11:22:33"
LINK = {"host": "bridge.local", "noise_psk": PSK, "ble_address": MAC, "device_name": DEVICE}
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def _proxy(**overrides: Any) -> ProxyInfo:
    return ProxyInfo(
        proxy_name="mowdeck-bridge",
        esphome_version="2026.9.1",
        compiled_at="Oct 10 2026, 09:00:00",
        project_name="mowdeck.bridge",
        project_version="1",
        bluetooth_mac="AA:BB:CC:00:00:01",
        checked_at=NOW,
        **overrides,
    )


class FakeCloud:
    """Answers ``gateway.cloud.discover`` with one mower."""

    async def handle(self, message: BusMessage) -> None:
        request = message.model(CloudDiscoverRequest)
        if request.email.casefold() != EMAIL or request.password != PASSWORD:
            await message.respond(CloudDiscoverReply(devices=[]))
            return
        devices = [
            DiscoveredDevice(device_name=DEVICE, iot_id="iot-1", model="LUBA 2", is_mower=True)
        ]
        await message.respond(CloudDiscoverReply(devices=devices))


class FakeScanner:
    """Answers bridge scans like the Device-Gateway; ``reply`` None stays silent."""

    def __init__(self) -> None:
        self.reply: BridgeScanReply | None = BridgeScanReply(
            proxy=_proxy(), devices=[ScannedDevice(name=DEVICE, address=MAC, rssi=-62)]
        )
        self.requests: list[BridgeScanRequest] = []

    async def handle(self, message: BusMessage) -> None:
        self.requests.append(message.model(BridgeScanRequest))
        if self.reply is not None:
            await message.respond(self.reply)


class FakeFirmware:
    """Answers firmware checks; ``reply`` None stays silent."""

    def __init__(self) -> None:
        self.reply: FirmwareCheckReply | None = FirmwareCheckReply(
            info=FirmwareInfo(
                installed="1.11.5", available="1.12.0", update_state="available", checked_at=NOW
            )
        )
        self.requests: list[FirmwareCheckRequest] = []

    async def handle(self, message: BusMessage) -> None:
        self.requests.append(message.model(FirmwareCheckRequest))
        if self.reply is not None:
            await message.respond(self.reply)


async def _gateway(bus: InMemoryBus) -> tuple[FakeScanner, FakeFirmware]:
    await bus.subscribe(GATEWAY_CLOUD_DISCOVER, FakeCloud().handle, queue="device-gateway")
    scanner, firmware = FakeScanner(), FakeFirmware()
    await bus.subscribe(ALL_BRIDGE_SCANS, scanner.handle)
    await bus.subscribe(ALL_FIRMWARE_CHECKS, firmware.handle)
    return scanner, firmware


def _settings(**overrides: Any) -> dict[str, Any]:
    return {
        "credential_encryption_key": KEY,
        "allowed_connection_kinds": "simulated,cloud",
        **overrides,
    }


async def _cloud_mower(h: Harness, owner: Person) -> str:
    linked = await h.client.post(
        f"{API}/linked-accounts", headers=owner.headers, json={"email": EMAIL, "password": PASSWORD}
    )
    assert linked.status_code == 201, linked.text
    created = await h.client.post(
        f"{API}/mowers",
        headers=owner.headers,
        json={
            "connection_kind": "cloud",
            "linked_account_id": linked.json()["id"],
            "device_name": DEVICE,
        },
    )
    assert created.status_code == 201, created.text
    mower_id: str = created.json()["id"]
    return mower_id


def _link_url(mower_id: str) -> str:
    return f"{API}/mowers/{mower_id}/links/ble_bridge"


def _updates(h: Harness) -> list[MowerRegistration]:
    return [
        m.model(EventEnvelope[MowerRegistration]).payload
        for m in h.bus.messages("event.mower.updated")
    ]


# --- the registry decides ----------------------------------------------------------------------


def test_fleet_links_eligibility_comes_from_the_kind_registry() -> None:
    assert links_supported("cloud") == {"ble_bridge"}
    assert links_supported("simulated") == frozenset()
    assert links_supported("ble_bridge") == frozenset()
    assert links_supported("wifi") == frozenset()  # no schema, no links
    assert SCHEMAS["ble_bridge"].secret_keys == {"noise_psk"}
    assert "ble_bridge" in SCHEMAS["cloud"].supports_links


async def test_fleet_link_is_refused_for_a_kind_without_links_and_accepted_for_cloud() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        simulated = (await h.register(owner))["id"]
        cloud = await _cloud_mower(h, owner)
        refused = await h.client.put(_link_url(simulated), headers=owner.headers, json=LINK)
        scan_refused = await h.client.post(
            f"{_link_url(simulated)}/scan", headers=owner.headers, json={"host": "b", **LINK}
        )
        accepted = await h.client.put(_link_url(cloud), headers=owner.headers, json=LINK)
    assert refused.status_code == 409
    assert refused.json()["type"] == "urn:mowdeck:problem:link-not-applicable"
    assert scan_refused.status_code == 422  # unknown fields are refused before the gate
    assert accepted.status_code == 200, accepted.text


# --- store, mask, publish ----------------------------------------------------------------------


async def test_fleet_link_roundtrip_masks_the_key_and_never_publishes_it() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        mower_id = await _cloud_mower(h, owner)
        before = len(_updates(h))
        put = await h.client.put(
            _link_url(mower_id), headers=owner.headers, json={**LINK, "allow_manual_blades": True}
        )
        got = await h.client.get(_link_url(mower_id), headers=owner.headers)
        mower = await h.client.get(f"{API}/mowers/{mower_id}", headers=owner.headers)
        listed = await h.client.get(f"{API}/mowers", headers=owner.headers)
        kept = await h.client.put(
            _link_url(mower_id),
            headers=owner.headers,
            json={**LINK, "noise_psk": "***", "allow_manual_blades": False},
        )
        shard = await h.app.state.fleet.mowers.registrations_for_shard(0, 1)
        stored = next(iter(h.store.tables.mower_links.rows.values()))
        reply = await h.bus.request(
            FLEET_MOWERS_CREDENTIALS,
            MowerCredentialsRequest(mower_id=UUID(mower_id), account_id=owner.account_id),
            timeout=1.0,
        )
        secrets = reply.model(MowerCredentialsReply)
        updates = _updates(h)[before:]
        audit = [e for e in h.store.tables.audit.rows.values() if e.action == LINK_SET]
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["kind"] == "ble_bridge" and body["proxy_info"] is None
    assert body["options"] == {
        "host": "bridge.local",
        "port": 6053,
        "noise_psk": "***",
        "ble_address": MAC,
        "device_name": DEVICE,
        "allow_manual_blades": True,
    }
    assert got.json() == body
    assert mower.json()["links"]["ble_bridge"] == body
    assert listed.json()["items"][0]["links"]["ble_bridge"]["options"]["noise_psk"] == "***"
    assert kept.status_code == 200 and kept.json()["options"]["allow_manual_blades"] is False
    for text in (put.text, got.text, mower.text, listed.text, kept.text):
        assert PSK not in text
    assert "noise_psk" not in stored.options and PSK.encode() not in stored.secrets_encrypted
    # the registration carries the public options and the withheld marker, never the key
    assert len(updates) == 2
    for payload in (*updates, shard[0]):
        link = payload.links["ble_bridge"]
        assert "noise_psk" not in link and PSK not in str(payload)
        assert link["credentials"] == "fleet" and link["credentials_rev"]
        assert link["host"] == "bridge.local" and link["ble_address"] == MAC
        assert payload.links_needing_credentials == {"ble_bridge"}
    assert updates[0].links["ble_bridge"]["allow_manual_blades"] is True
    assert updates[1].links["ble_bridge"]["allow_manual_blades"] is False
    # "***" kept the key: the revision is unchanged and the gateway gets the same secret
    assert (
        updates[0].links["ble_bridge"]["credentials_rev"]
        == (updates[1].links["ble_bridge"]["credentials_rev"])
    )
    assert secrets.found and secrets.link_secrets == {"ble_bridge": {"noise_psk": PSK}}
    assert secrets.secret_options["password"] == PASSWORD
    assert [a.details["created"] for a in audit] == [True, False]
    assert all(PSK not in str(a.details) for a in audit)


async def test_fleet_new_key_changes_the_revision() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        mower_id = await _cloud_mower(h, owner)
        await h.client.put(_link_url(mower_id), headers=owner.headers, json=LINK)
        await h.client.put(
            _link_url(mower_id), headers=owner.headers, json={**LINK, "noise_psk": NEW_PSK}
        )
        secrets = (
            await h.bus.request(
                FLEET_MOWERS_CREDENTIALS,
                MowerCredentialsRequest(mower_id=UUID(mower_id), account_id=owner.account_id),
                timeout=1.0,
            )
        ).model(MowerCredentialsReply)
        updates = [u for u in _updates(h) if u.links]
    first, second = updates[-2].links["ble_bridge"], updates[-1].links["ble_bridge"]
    assert first["credentials_rev"] != second["credentials_rev"]
    assert secrets.link_secrets == {"ble_bridge": {"noise_psk": NEW_PSK}}


async def test_fleet_link_delete_publishes_the_registration_without_it() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        operator = await h.member(owner, "operator")
        mower_id = await _cloud_mower(h, owner)
        missing = await h.client.get(_link_url(mower_id), headers=owner.headers)
        await h.client.put(_link_url(mower_id), headers=owner.headers, json=LINK)
        forbidden = await h.client.delete(_link_url(mower_id), headers=operator.headers)
        seen = await h.client.get(_link_url(mower_id), headers=operator.headers)
        removed = await h.client.delete(_link_url(mower_id), headers=owner.headers)
        gone = await h.client.get(_link_url(mower_id), headers=owner.headers)
        twice = await h.client.delete(_link_url(mower_id), headers=owner.headers)
        mower = await h.client.get(f"{API}/mowers/{mower_id}", headers=owner.headers)
        updates = _updates(h)
        audit = [e.action for e in h.store.tables.audit.rows.values()]
        rows = len(h.store.tables.mower_links.rows)
    assert missing.status_code == 404
    assert forbidden.status_code == 403 and seen.status_code == 200  # can_view reads the link
    assert removed.status_code == 204 and gone.status_code == 404 and twice.status_code == 404
    assert mower.json()["links"] == {}
    assert updates[-1].links == {} and updates[-2].links != {}
    assert LINK_REMOVED in audit
    assert rows == 0


async def test_fleet_link_options_are_validated_with_the_bridge_schema() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        mower_id = await _cloud_mower(h, owner)
        bad_mac = await h.client.put(
            _link_url(mower_id), headers=owner.headers, json={**LINK, "ble_address": "bridge"}
        )
        no_key_yet = await h.client.put(
            _link_url(mower_id), headers=owner.headers, json={**LINK, "noise_psk": "***"}
        )
        unknown = await h.client.put(
            _link_url(mower_id), headers=owner.headers, json={**LINK, "wifi_password": "x"}
        )
        stranger = await h.owner()
        hidden = await h.client.put(_link_url(mower_id), headers=stranger.headers, json=LINK)
    assert bad_mac.status_code == 422
    assert bad_mac.json()["errors"][0]["pointer"] == "#/links/ble_bridge/ble_address"
    assert bad_mac.json()["errors"][0]["code"] == "pattern"
    assert no_key_yet.status_code == 422
    assert no_key_yet.json()["errors"][0]["pointer"] == "#/links/ble_bridge/noise_psk"
    assert unknown.status_code == 422  # request bodies refuse unknown fields
    assert hidden.status_code == 404


async def test_fleet_link_needs_the_credential_key() -> None:
    async with harness(allowed_connection_kinds="simulated,cloud") as h:  # no key
        await _gateway(h.bus)
        owner = await h.owner()
        simulated = (await h.register(owner))["id"]
        refused = await h.client.put(_link_url(simulated), headers=owner.headers, json=LINK)
    assert refused.status_code == 503
    assert "MOWDECK_CREDENTIAL_ENCRYPTION_KEY" in refused.json()["detail"]


async def test_fleet_links_go_with_the_mower_and_with_a_kind_change() -> None:
    async with harness(**_settings(allowed_connection_kinds="simulated,cloud")) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        mower_id = await _cloud_mower(h, owner)
        await h.client.put(_link_url(mower_id), headers=owner.headers, json=LINK)
        changed = await h.client.patch(
            f"{API}/mowers/{mower_id}",
            headers=owner.headers,
            json={"connection_kind": "simulated", "connection_options": {}},
        )
        after_change = len(h.store.tables.mower_links.rows)
        other = await h.owner()  # another account links the same vendor account
        second = await _cloud_mower(h, other)
        await h.client.put(_link_url(second), headers=other.headers, json=LINK)
        deleted = await h.client.delete(f"{API}/mowers/{second}", headers=other.headers)
        rows = len(h.store.tables.mower_links.rows)
        audit = [e.details for e in h.store.tables.audit.rows.values() if e.action == LINK_REMOVED]
    assert changed.status_code == 200 and changed.json()["links"] == {}
    assert after_change == 0
    assert audit[0]["reason"] == "connection_kind_changed"
    assert deleted.status_code == 204 and rows == 0


# --- scans and checks --------------------------------------------------------------------------


async def test_fleet_bridge_scan_is_relayed_and_the_check_refreshes_proxy_info() -> None:
    async with harness(**_settings()) as h:
        scanner, _ = await _gateway(h.bus)
        owner = await h.owner()
        mower_id = await _cloud_mower(h, owner)
        scan = await h.client.post(
            f"{_link_url(mower_id)}/scan",
            headers=owner.headers,
            json={"host": "bridge.local", "noise_psk": PSK},
        )
        no_link_yet = await h.client.post(
            f"{_link_url(mower_id)}/scan", headers=owner.headers, json={"host": "bridge.local"}
        )
        not_linked_check = await h.client.post(
            f"{_link_url(mower_id)}/check", headers=owner.headers
        )
        await h.client.put(_link_url(mower_id), headers=owner.headers, json=LINK)
        before = await h.client.get(_link_url(mower_id), headers=owner.headers)
        scanner.reply = BridgeScanReply(
            proxy=_proxy(outdated=True, outdated_reason="project_version"), devices=[]
        )
        check = await h.client.post(f"{_link_url(mower_id)}/check", headers=owner.headers)
        after = await h.client.get(_link_url(mower_id), headers=owner.headers)
        other_host = await h.client.post(
            f"{_link_url(mower_id)}/scan", headers=owner.headers, json={"host": "other.local"}
        )
        still = await h.client.get(_link_url(mower_id), headers=owner.headers)
    assert scan.status_code == 200, scan.text
    assert scan.headers["cache-control"] == "no-store"
    assert scan.json()["devices"][0]["name"] == DEVICE and scan.json()["error"] is None
    assert PSK not in scan.text
    first = scanner.requests[0]
    assert first.host == "bridge.local" and first.port == 6053 and first.noise_psk == PSK
    assert first.expected_name == DEVICE and first.account_id == owner.account_id
    assert no_link_yet.status_code == 422  # "***" needs a stored link
    assert no_link_yet.json()["errors"][0]["pointer"] == "#/links/ble_bridge/noise_psk"
    assert not_linked_check.status_code == 404
    assert before.json()["proxy_info"] is None
    assert check.status_code == 200
    assert scanner.requests[-2].noise_psk == PSK and scanner.requests[-2].host == "bridge.local"
    info = after.json()["proxy_info"]
    assert info["proxy_name"] == "mowdeck-bridge" and info["outdated"] is True
    assert info["outdated_reason"] == "project_version"
    assert other_host.status_code == 200 and scanner.requests[-1].noise_psk == PSK
    assert still.json()["proxy_info"] == info  # another bridge: the cache is not touched


async def test_fleet_bridge_scan_outcomes_stay_in_the_reply_and_relay_errors_are_problems() -> None:
    async with harness(**_settings(), bridge_scan_request_timeout_seconds=1) as h:
        scanner, _ = await _gateway(h.bus)
        owner = await h.owner()
        mower_id = await _cloud_mower(h, owner)
        url = f"{_link_url(mower_id)}/scan"
        body = {"host": "bridge.local", "noise_psk": PSK}

        def error(code: BridgeScanErrorCode, proxy: ProxyInfo | None = None) -> BridgeScanReply:
            return BridgeScanReply(
                proxy=proxy, error=BridgeScanError(code=code, detail=f"{code.value} here")
            )

        scanner.reply = error(BridgeScanErrorCode.TIMEOUT, _proxy())
        timeout = await h.client.post(url, headers=owner.headers, json=body)
        outcomes = {}
        for code in (
            BridgeScanErrorCode.UNREACHABLE,
            BridgeScanErrorCode.AUTH_FAILED,
            BridgeScanErrorCode.BUSY,
        ):
            scanner.reply = error(code)
            outcomes[code] = await h.client.post(url, headers=owner.headers, json=body)
        scanner.reply = error(BridgeScanErrorCode.UNSUPPORTED)
        unsupported = await h.client.post(url, headers=owner.headers, json=body)
        scanner.reply = error(BridgeScanErrorCode.NOT_FOUND)
        not_held = await h.client.post(url, headers=owner.headers, json=body)
        scanner.reply = error(BridgeScanErrorCode.FORBIDDEN)
        forbidden = await h.client.post(url, headers=owner.headers, json=body)
        scanner.reply = None  # no gateway answers
        silent = await h.client.post(url, headers=owner.headers, json=body)
    assert timeout.status_code == 200
    assert timeout.json()["error"]["code"] == "timeout"
    assert timeout.json()["proxy"]["proxy_name"] == "mowdeck-bridge"
    for code, response in outcomes.items():
        assert response.status_code == 200 and response.json()["error"]["code"] == code.value
    assert unsupported.status_code == 409
    assert unsupported.json()["type"] == "urn:mowdeck:problem:bridge-scan-unsupported"
    assert not_held.status_code == 503
    assert forbidden.status_code == 404
    assert silent.status_code == 503
    assert silent.json()["type"] == "urn:mowdeck:problem:unavailable"


async def test_fleet_bridge_scan_without_any_gateway_is_unavailable() -> None:
    async with harness(**_settings()) as h:
        await h.bus.subscribe(GATEWAY_CLOUD_DISCOVER, FakeCloud().handle, queue="device-gateway")
        owner = await h.owner()
        mower_id = await _cloud_mower(h, owner)
        response = await h.client.post(
            f"{_link_url(mower_id)}/scan",
            headers=owner.headers,
            json={"host": "bridge.local", "noise_psk": PSK},
        )
    assert response.status_code == 503


# --- firmware ----------------------------------------------------------------------------------


async def test_fleet_firmware_check_is_relayed_and_maps_errors() -> None:
    async with harness(**_settings(), firmware_check_request_timeout_seconds=1) as h:
        _, firmware = await _gateway(h.bus)
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        stranger = await h.owner()
        mower_id = (await h.register(owner))["id"]
        url = f"{API}/mowers/{mower_id}/firmware/check"
        ok = await h.client.post(url, headers=viewer.headers)
        hidden = await h.client.post(url, headers=stranger.headers)

        def error(code: FirmwareCheckErrorCode) -> FirmwareCheckReply:
            return FirmwareCheckReply(error=FirmwareCheckError(code=code, detail=f"{code} here"))

        firmware.reply = error(FirmwareCheckErrorCode.UNSUPPORTED)
        unsupported = await h.client.post(url, headers=owner.headers)
        firmware.reply = error(FirmwareCheckErrorCode.UNAVAILABLE)
        unavailable = await h.client.post(url, headers=owner.headers)
        firmware.reply = error(FirmwareCheckErrorCode.NOT_FOUND)
        not_held = await h.client.post(url, headers=owner.headers)
        firmware.reply = error(FirmwareCheckErrorCode.FORBIDDEN)
        forbidden = await h.client.post(url, headers=owner.headers)
        firmware.reply = None
        silent = await h.client.post(url, headers=owner.headers)
    assert ok.status_code == 200, ok.text
    assert ok.headers["cache-control"] == "no-store"
    assert ok.json()["available"] == "1.12.0" and ok.json()["update_state"] == "available"
    (request, *_) = firmware.requests
    assert str(request.mower_id) == mower_id and request.account_id == owner.account_id
    assert hidden.status_code == 404
    assert unsupported.status_code == 409
    assert unsupported.json()["type"] == "urn:mowdeck:problem:firmware-check-unsupported"
    assert unavailable.status_code == 503 and not_held.status_code == 503
    assert forbidden.status_code == 404
    assert silent.status_code == 503
