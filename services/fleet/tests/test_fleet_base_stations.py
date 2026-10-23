"""RTK base stations (ADR 0047): registered from a linked account's device list (or the
simulated one), linked to mowers, published in their registrations, firmware checked and
updated through a linked mower. Permissions on the account, tenant isolation, audit and the
command record are covered. A fake vendor cloud and a fake Device-Gateway answer on the
in-memory bus."""

import base64
from typing import Any
from uuid import uuid4

from fleet_support import API, Harness, Person, harness
from mowdeck_contracts.v1 import (
    CloudDiscoverReply,
    CloudDiscoverRequest,
    CommandEnvelope,
    DeviceType,
    DiscoveredDevice,
    EventEnvelope,
    FirmwareCheckError,
    FirmwareCheckErrorCode,
    FirmwareCheckReply,
    FirmwareCheckRequest,
    FirmwareInfo,
    MowerRegistration,
)
from mowdeck_contracts.v1.subjects import ALL_FIRMWARE_CHECKS, GATEWAY_CLOUD_DISCOVER
from mowdeck_service_kit.bus import BusMessage, InMemoryBus

KEY = base64.b64encode(b"k" * 32).decode()
EMAIL = "garden@example.com"
OTHER_EMAIL = "shared@example.com"
PASSWORD = "mammotion-secret"  # noqa: S105 - a test value
MOWER = "Luba-VP0001"
BASE = "RTK-0001"
BASE_IOT = "iot-rtk-1"
DEVICES = [
    DiscoveredDevice(device_name=MOWER, iot_id="iot-1", model="LUBA 2", is_mower=True),
    DiscoveredDevice(
        device_name=BASE,
        iot_id=BASE_IOT,
        model="RTK",
        is_mower=False,
        device_type=DeviceType.RTK_BASE,
    ),
    DiscoveredDevice(device_name="Spino-1", iot_id="iot-pool", is_mower=False),
]


class FakeCloud:
    """Both e-mails list the same devices (Mammotion's sharing: one machine, two accounts)."""

    async def handle(self, message: BusMessage) -> None:
        request = message.model(CloudDiscoverRequest)
        devices = DEVICES if request.password == PASSWORD else []
        await message.respond(CloudDiscoverReply(devices=devices))


class FakeFirmware:
    """Answers firmware checks like the gateway that holds the mower."""

    def __init__(self) -> None:
        self.reply: FirmwareCheckReply | None = FirmwareCheckReply(
            info=FirmwareInfo(installed="1.2.3.4", available="1.2.5.0", update_state="available")
        )
        self.requests: list[FirmwareCheckRequest] = []

    async def handle(self, message: BusMessage) -> None:
        self.requests.append(message.model(FirmwareCheckRequest))
        if self.reply is not None:
            await message.respond(self.reply)


async def _gateway(bus: InMemoryBus) -> FakeFirmware:
    await bus.subscribe(GATEWAY_CLOUD_DISCOVER, FakeCloud().handle, queue="device-gateway")
    firmware = FakeFirmware()
    await bus.subscribe(ALL_FIRMWARE_CHECKS, firmware.handle)
    return firmware


def _settings() -> dict[str, Any]:
    return {"credential_encryption_key": KEY, "allowed_connection_kinds": "simulated,cloud"}


async def _linked(h: Harness, who: Person, email: str = EMAIL) -> str:
    response = await h.client.post(
        f"{API}/linked-accounts", headers=who.headers, json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    linked_id: str = response.json()["id"]
    return linked_id


async def _cloud_mower(h: Harness, who: Person, linked_id: str) -> str:
    response = await h.client.post(
        f"{API}/mowers",
        headers=who.headers,
        json={"connection_kind": "cloud", "linked_account_id": linked_id, "device_name": MOWER},
    )
    assert response.status_code == 201, response.text
    mower_id: str = response.json()["id"]
    return mower_id


async def _base(h: Harness, who: Person, **body: Any) -> Any:
    return await h.client.post(f"{API}/base-stations", headers=who.headers, json=body)


async def _link(h: Harness, who: Person, mower_id: str, base_id: str | None) -> Any:
    return await h.client.put(
        f"{API}/mowers/{mower_id}/base-station",
        headers=who.headers,
        json={"base_station_id": base_id},
    )


def _updated(h: Harness) -> list[MowerRegistration]:
    return [
        m.model(EventEnvelope[MowerRegistration]).payload
        for m in h.bus.messages("event.mower.updated")
    ]


def _audit(h: Harness) -> list[str]:
    return [e.action for e in h.store.tables.audit.rows.values()]


# --- registering --------------------------------------------------------------------------------


async def test_fleet_base_station_from_the_device_list() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        linked_id = await _linked(h, owner)
        devices_before = await h.client.get(
            f"{API}/linked-accounts/{linked_id}/devices", headers=owner.headers
        )
        created = await _base(
            h, owner, linked_account_id=linked_id, device_name=BASE, name="Garden base"
        )
        again = await _base(h, owner, linked_account_id=linked_id, device_name=BASE)
        mower_as_base = await _base(h, owner, linked_account_id=linked_id, device_name=MOWER)
        pool = await _base(h, owner, linked_account_id=linked_id, device_name="Spino-1")
        unknown = await _base(h, owner, linked_account_id=linked_id, device_name="RTK-9")
        missing = await _base(h, owner)
        base_as_mower = await h.client.post(
            f"{API}/mowers",
            headers=owner.headers,
            json={"connection_kind": "cloud", "linked_account_id": linked_id, "device_name": BASE},
        )
        devices = await h.client.get(
            f"{API}/linked-accounts/{linked_id}/devices", headers=owner.headers
        )
        listed = await h.client.get(f"{API}/base-stations", headers=owner.headers)
        one = await h.client.get(created.headers["Location"], headers=owner.headers)
    before = {d["device_name"]: d for d in devices_before.json()["items"]}
    assert before[BASE]["device_type"] == "rtk_base" and before[BASE]["registered"] is False
    assert before["Spino-1"]["device_type"] == "other"
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Garden base" and body["source"] == "cloud"
    assert body["device_name"] == BASE and body["iot_id"] == BASE_IOT and body["model"] == "RTK"
    assert body["linked_account_id"] == linked_id and body["mower_ids"] == []
    assert again.status_code == 409
    assert again.json()["type"].endswith("base-station-already-registered")
    assert "Garden base" in again.json()["detail"]
    for refused in (mower_as_base, pool):
        assert refused.status_code == 422
        assert refused.json()["errors"][0]["code"] == "not_a_base_station"
    assert unknown.status_code == 422 and unknown.json()["errors"][0]["code"] == "unknown_device"
    assert missing.status_code == 422
    assert base_as_mower.status_code == 422
    assert "register it as a base station" in base_as_mower.json()["detail"]
    after = {d["device_name"]: d for d in devices.json()["items"]}
    assert after[BASE]["registered"] is True and after[BASE]["base_station_id"] == body["id"]
    assert after[BASE]["mower_id"] is None
    assert [b["id"] for b in listed.json()["items"]] == [body["id"]]
    assert one.json() == body
    assert "base_station.registered" in _audit(h)


async def test_fleet_base_station_one_physical_base_per_installation() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        first, second = await h.owner(), await h.owner()
        first_linked = await _linked(h, first)
        second_linked = await _linked(h, second, OTHER_EMAIL)
        created = await _base(h, first, linked_account_id=first_linked, device_name=BASE)
        taken = await _base(h, second, linked_account_id=second_linked, device_name=BASE)
        devices = await h.client.get(
            f"{API}/linked-accounts/{second_linked}/devices", headers=second.headers
        )
    assert created.status_code == 201
    assert taken.status_code == 409
    assert taken.json()["detail"] == "this base station is already registered in MowDeck"
    shown = {d["device_name"]: d for d in devices.json()["items"]}
    assert shown[BASE]["registered_elsewhere"] is True and shown[BASE]["registered"] is False


async def test_fleet_simulated_base_station_and_its_rules() -> None:
    async with harness(**_settings()) as h:
        owner = await h.owner()
        simulated = await _base(h, owner, source="simulated")
        smuggled = await _base(h, owner, source="simulated", device_name=BASE)
        mower = await h.register(owner)
        linked = await _link(h, owner, mower["id"], simulated.json()["id"])
    assert simulated.status_code == 201, simulated.text
    assert simulated.json()["name"] == "Simulated base station"
    assert simulated.json()["source"] == "simulated" and simulated.json()["iot_id"] is None
    assert smuggled.status_code == 422
    assert linked.status_code == 200 and linked.json()["base_station_id"] == simulated.json()["id"]
    assert _updated(h)[-1].base_station is not None
    assert _updated(h)[-1].base_station.source == "simulated"  # type: ignore[union-attr]

    async with harness(allowed_connection_kinds="cloud", credential_encryption_key=KEY) as h:
        owner = await h.owner()
        refused = await _base(h, owner, source="simulated")
    assert refused.status_code == 422


# --- linking mowers ---------------------------------------------------------------------------


async def test_fleet_link_mower_publishes_the_base_station() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        linked_id = await _linked(h, owner)
        mower_id = await _cloud_mower(h, owner, linked_id)
        base = (await _base(h, owner, linked_account_id=linked_id, device_name=BASE)).json()
        simulated_mower = await h.register(owner)
        linked = await _link(h, owner, mower_id, base["id"])
        same = await _link(h, owner, mower_id, base["id"])
        wrong_kind = await _link(h, owner, simulated_mower["id"], base["id"])
        foreign = await _link(h, owner, mower_id, str(uuid4()))
        shown = await h.client.get(f"{API}/base-stations/{base['id']}", headers=owner.headers)
        registrations = await h.app.state.fleet.mowers.registrations_for_shard(0, 1)
        events_after_link = len(_updated(h))
        unlinked = await _link(h, owner, mower_id, None)
    assert linked.status_code == 200, linked.text
    assert linked.json()["base_station_id"] == base["id"]
    assert same.status_code == 200
    assert wrong_kind.status_code == 409
    assert wrong_kind.json()["type"].endswith("base-station-not-applicable")
    assert foreign.status_code == 404
    assert shown.json()["mower_ids"] == [mower_id]
    published = _updated(h)
    assert events_after_link == 1  # the repeated link changed nothing
    ref = published[0].base_station
    assert ref is not None and str(ref.base_station_id) == base["id"]
    assert ref.iot_id == BASE_IOT and ref.device_name == BASE and ref.name == BASE
    assert "password" not in published[0].model_dump_json()
    by_id = {str(r.mower_id): r for r in registrations}
    assert by_id[mower_id].base_station == ref
    assert by_id[simulated_mower["id"]].base_station is None
    assert unlinked.status_code == 200 and unlinked.json()["base_station_id"] is None
    assert _updated(h)[-1].base_station is None
    assert "mower.base_station_set" in _audit(h) and "mower.base_station_removed" in _audit(h)


async def test_fleet_rename_and_delete_base_station() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        linked_id = await _linked(h, owner)
        mower_id = await _cloud_mower(h, owner, linked_id)
        base = (await _base(h, owner, linked_account_id=linked_id, device_name=BASE)).json()
        await _link(h, owner, mower_id, base["id"])
        renamed = await h.client.patch(
            f"{API}/base-stations/{base['id']}", headers=owner.headers, json={"name": "Shed"}
        )
        in_use = await h.client.delete(f"{API}/linked-accounts/{linked_id}", headers=owner.headers)
        await h.client.delete(f"{API}/mowers/{mower_id}", headers=owner.headers)
        still_in_use = await h.client.delete(
            f"{API}/linked-accounts/{linked_id}", headers=owner.headers
        )
        mower_id = await _cloud_mower(h, owner, linked_id)
        await _link(h, owner, mower_id, base["id"])
        deleted = await h.client.delete(f"{API}/base-stations/{base['id']}", headers=owner.headers)
        gone = await h.client.get(f"{API}/base-stations/{base['id']}", headers=owner.headers)
        mower = await h.client.get(f"{API}/mowers/{mower_id}", headers=owner.headers)
    assert renamed.status_code == 200 and renamed.json()["name"] == "Shed"
    assert in_use.status_code == 409  # the mower
    assert still_in_use.status_code == 409 and "base stations" in still_in_use.json()["detail"]
    assert deleted.status_code == 204
    assert gone.status_code == 404
    assert mower.json()["base_station_id"] is None
    assert _updated(h)[-1].base_station is None
    assert {"base_station.updated", "base_station.removed"} <= set(_audit(h))


# --- permissions and tenant isolation ---------------------------------------------------------


async def test_fleet_base_station_permissions_and_isolation() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        stranger = await h.owner()
        linked_id = await _linked(h, owner)
        mower_id = await _cloud_mower(h, owner, linked_id)
        base = (await _base(h, owner, linked_account_id=linked_id, device_name=BASE)).json()
        url = f"{API}/base-stations/{base['id']}"
        viewer_list = await h.client.get(f"{API}/base-stations", headers=viewer.headers)
        viewer_create = await _base(h, viewer, source="simulated")
        viewer_rename = await h.client.patch(url, headers=viewer.headers, json={"name": "x"})
        viewer_delete = await h.client.delete(url, headers=viewer.headers)
        viewer_link = await _link(h, viewer, mower_id, base["id"])
        await _link(h, owner, mower_id, base["id"])
        viewer_update = await h.client.post(
            f"{url}/firmware/update", headers=viewer.headers, json={"version": "1.2.5.0"}
        )
        viewer_check = await h.client.post(f"{url}/firmware/check", headers=viewer.headers)
        stranger_list = await h.client.get(f"{API}/base-stations", headers=stranger.headers)
        stranger_get = await h.client.get(url, headers=stranger.headers)
        stranger_delete = await h.client.delete(url, headers=stranger.headers)
        stranger_check = await h.client.post(f"{url}/firmware/check", headers=stranger.headers)
        stranger_mower = await h.register(stranger)
        stranger_link = await _link(h, stranger, stranger_mower["id"], base["id"])
        stranger_uses_own = await _link(h, stranger, mower_id, None)
        anonymous = await h.client.get(f"{API}/base-stations")
    assert viewer_list.status_code == 200 and len(viewer_list.json()["items"]) == 1
    for refused in (viewer_create, viewer_rename, viewer_delete, viewer_update):
        assert refused.status_code == 403, refused.text
    assert viewer_link.status_code == 403  # sees the mower, cannot manage it
    assert viewer_check.status_code == 200
    assert stranger_list.json()["items"] == []
    assert stranger_get.status_code == 404 and stranger_delete.status_code == 404
    assert stranger_check.status_code == 404
    assert stranger_link.status_code == 404  # another account's base station
    assert stranger_uses_own.status_code == 404  # another account's mower
    assert anonymous.status_code == 401


# --- firmware ---------------------------------------------------------------------------------


async def test_fleet_base_station_firmware_check_through_a_linked_mower() -> None:
    async with harness(**_settings()) as h:
        firmware = await _gateway(h.bus)
        owner = await h.owner()
        linked_id = await _linked(h, owner)
        mower_id = await _cloud_mower(h, owner, linked_id)
        base = (await _base(h, owner, linked_account_id=linked_id, device_name=BASE)).json()
        url = f"{API}/base-stations/{base['id']}/firmware/check"
        unlinked = await h.client.post(url, headers=owner.headers)
        await _link(h, owner, mower_id, base["id"])
        checked = await h.client.post(url, headers=owner.headers)
        firmware.reply = FirmwareCheckReply(
            error=FirmwareCheckError(code=FirmwareCheckErrorCode.UNAVAILABLE, detail="offline")
        )
        offline = await h.client.post(url, headers=owner.headers)
    assert unlinked.status_code == 409
    assert unlinked.json()["type"].endswith("base-station-not-linked")
    assert checked.status_code == 200, checked.text
    assert checked.json()["available"] == "1.2.5.0"
    request = firmware.requests[0]
    assert str(request.mower_id) == mower_id and request.target == "base_station"
    assert offline.status_code == 503 and "offline" in offline.json()["detail"]


async def test_fleet_base_station_firmware_update_is_a_recorded_command() -> None:
    async with harness(**_settings()) as h:
        await _gateway(h.bus)
        owner = await h.owner()
        linked_id = await _linked(h, owner)
        mower_id = await _cloud_mower(h, owner, linked_id)
        other_mower = await _cloud_mower_unlinked(h, owner)
        base = (await _base(h, owner, linked_account_id=linked_id, device_name=BASE)).json()
        url = f"{API}/base-stations/{base['id']}/firmware/update"
        unlinked = await h.client.post(url, headers=owner.headers, json={"version": "1.2.5.0"})
        direct_unlinked = await h.client.post(
            f"{API}/mowers/{mower_id}/commands",
            headers=owner.headers,
            json={"command": {"kind": "base_station_firmware_update", "version": "1.2.5.0"}},
        )
        await _link(h, owner, mower_id, base["id"])
        not_linked_mower = await h.client.post(
            url, headers=owner.headers, json={"version": "1.2.5.0", "mower_id": other_mower}
        )
        accepted = await h.client.post(url, headers=owner.headers, json={"version": "1.2.5.0"})
        group = await h.create(owner, "groups", name="All")
        bulk = await h.client.post(
            f"{API}/groups/{group['id']}/commands",
            headers=owner.headers,
            json={"command": {"kind": "base_station_firmware_update", "version": "1.2.5.0"}},
        )
        published = h.commands_published()
        records = list(h.store.tables.commands.rows.values())
    assert unlinked.status_code == 409
    assert direct_unlinked.status_code == 409
    assert direct_unlinked.json()["type"].endswith("base-station-not-linked")
    assert not_linked_mower.status_code == 422
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["kind"] == "base_station_firmware_update"
    assert accepted.json()["mower_id"] == mower_id
    assert bulk.status_code == 422
    envelope: CommandEnvelope = published[-1]
    assert envelope.command.kind == "base_station_firmware_update"
    assert str(envelope.mower_id) == mower_id
    assert [r.kind for r in records] == ["base_station_firmware_update"]
    assert records[0].command == {"kind": "base_station_firmware_update", "version": "1.2.5.0"}


async def _cloud_mower_unlinked(h: Harness, who: Person) -> str:
    """A simulated mower that is not linked to the base station."""
    created = await h.register(who)
    mower_id: str = created["id"]
    return mower_id
