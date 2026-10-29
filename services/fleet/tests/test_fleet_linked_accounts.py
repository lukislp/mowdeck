"""Linked Mammotion accounts, cloud mowers from their device lists, one physical mower per
installation, and the move of old cloud mowers (ADR 0034).

A fake Device-Gateway answers ``gateway.cloud.discover`` on the in-memory bus.
"""

import base64
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fleet_cloud import link_body, login_of, serve_providers
from fleet_support import API, Harness, Person, harness
from mowdeck_authz import RelationshipTuple, account, principal_subject
from mowdeck_contracts.v1 import (
    CloudDiscoverError,
    CloudDiscoverErrorCode,
    CloudDiscoverReply,
    CloudDiscoverRequest,
    DiscoveredDevice,
    EventEnvelope,
    MowerCredentialsReply,
    MowerCredentialsRequest,
    MowerRegistration,
)
from mowdeck_contracts.v1.subjects import FLEET_MOWERS_CREDENTIALS, GATEWAY_CLOUD_DISCOVER
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from pydantic import ValidationError

from fleet.adapters.crypto import (
    DEV_KEY,
    AesGcmCipher,
    DisabledCipher,
    cipher_from_key,
    key_id,
    parse_key,
)
from fleet.application.throttle import LoginThrottle, TooManyLoginAttempts
from fleet.domain.models import Mower
from fleet.settings import Settings

KEY = base64.b64encode(b"k" * 32).decode()
EMAIL = "garden@example.com"
PASSWORD = "mammotion-secret"  # noqa: S105 - a test value
SHARED_IOT = "iot-shared-0001"


def _devices(*extra: DiscoveredDevice) -> list[DiscoveredDevice]:
    return [
        DiscoveredDevice(
            device_name="Luba-VPA",
            iot_id=SHARED_IOT,
            model="LUBA 2 AWD 5000X",
            online=True,
            is_mower=True,
        ),
        DiscoveredDevice(device_name="Luba-VPB", iot_id="iot-b", online=False, is_mower=True),
        DiscoveredDevice(device_name="RTK-1", iot_id="iot-rtk", model="RTK", is_mower=False),
        *extra,
    ]


class FakeCloud:
    """Answers discovery requests like the Device-Gateway with the fake account table."""

    def __init__(self, accounts: dict[str, tuple[str, list[DiscoveredDevice]]]) -> None:
        self.accounts = accounts
        self.requests: list[CloudDiscoverRequest] = []
        self.down = False

    async def handle(self, message: BusMessage) -> None:
        request = message.model(CloudDiscoverRequest)
        self.requests.append(request)
        if self.down:
            error = CloudDiscoverError(code=CloudDiscoverErrorCode.UNAVAILABLE, detail="cloud down")
            await message.respond(CloudDiscoverReply(error=error))
            return
        email, password = login_of(request)
        found = self.accounts.get(email.casefold())
        if found is None or found[0] != password:
            error = CloudDiscoverError(code=CloudDiscoverErrorCode.LOGIN_FAILED, detail="refused")
            await message.respond(CloudDiscoverReply(error=error))
            return
        await message.respond(CloudDiscoverReply(devices=found[1]))


async def _cloud(
    bus: InMemoryBus, accounts: dict[str, tuple[str, list[DiscoveredDevice]]] | None = None
) -> FakeCloud:
    table = {EMAIL: (PASSWORD, _devices())}
    table.update(accounts or {})
    cloud = FakeCloud(table)
    await bus.subscribe(GATEWAY_CLOUD_DISCOVER, cloud.handle, queue="device-gateway")
    await serve_providers(bus)
    return cloud


def _settings(**overrides: Any) -> dict[str, Any]:
    return {
        "credential_encryption_key": KEY,
        "allowed_connection_kinds": "simulated,cloud",
        **overrides,
    }


async def _link(h: Harness, who: Person, email: str = EMAIL, password: str = PASSWORD) -> Any:
    return await h.client.post(
        f"{API}/linked-accounts", headers=who.headers, json=link_body(email, password)
    )


def _events(h: Harness, event_type: str) -> list[MowerRegistration]:
    return [
        m.model(EventEnvelope[MowerRegistration]).payload
        for m in h.bus.messages(f"event.mower.{event_type}")
    ]


async def _register(h: Harness, who: Person, linked_id: str, device: str, **body: Any) -> Any:
    return await h.client.post(
        f"{API}/mowers",
        headers=who.headers,
        json={
            "connection_kind": "cloud",
            "linked_account_id": linked_id,
            "device_name": device,
            **body,
        },
    )


def _no_secret(*bodies: Iterable[Any] | Any) -> None:
    for body in bodies:
        assert PASSWORD not in str(body)


# --- linking ---------------------------------------------------------------------------------


async def test_fleet_link_an_account_lists_its_devices_and_never_returns_the_password() -> None:
    async with harness(**_settings()) as h:
        cloud = await _cloud(h.bus)
        owner = await h.owner()
        created = await _link(h, owner, email=" Garden@example.com ")
        listed = await h.client.get(f"{API}/linked-accounts", headers=owner.headers)
        one = await h.client.get(created.headers["Location"], headers=owner.headers)
        stored = next(iter(h.store.tables.linked_accounts.rows.values()))
        audit = [e.action for e in h.store.tables.audit.rows.values()]
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["email"] == "Garden@example.com" and body["provider"] == "acme"
    assert body["device_count"] == 3 and body["mower_count"] == 0
    assert body["last_synced_at"] is not None and body["last_sync_error"] is None
    devices = {d["device_name"]: d for d in body["devices"]}
    assert devices["Luba-VPA"]["model"] == "LUBA 2 AWD 5000X" and devices["Luba-VPA"]["online"]
    assert devices["RTK-1"]["is_mower"] is False
    assert not any(d["registered"] or d["registered_elsewhere"] for d in body["devices"])
    assert [i["id"] for i in listed.json()["items"]] == [body["id"]]
    assert one.json()["id"] == body["id"]
    _no_secret(created.text, listed.text, one.text)
    assert PASSWORD.encode() not in stored.password_encrypted
    assert login_of(cloud.requests[0]) == ("Garden@example.com", PASSWORD)
    assert cloud.requests[0].linked_account_id is None
    assert "linked_account.created" in audit


async def test_fleet_link_refusals() -> None:
    async with harness(**_settings()) as h:
        cloud = await _cloud(h.bus)
        owner = await h.owner()
        refused = await _link(h, owner, password="wrong")  # noqa: S106 - a test value
        not_mail = await _link(h, owner, email="no-at-sign")
        assert (await _link(h, owner)).status_code == 201
        twice = await _link(h, owner, email=EMAIL.upper())
        cloud.down = True
        down = await _link(h, owner, email="other@example.com")
        rows = len(h.store.tables.linked_accounts.rows)
    assert refused.status_code == 422
    assert refused.json()["type"] == "urn:mowdeck:problem:linked-account-login-failed"
    assert refused.json()["errors"][0]["pointer"] == "#/credentials/password"
    assert not_mail.status_code == 422
    assert twice.status_code == 409
    assert down.status_code == 503
    assert rows == 1
    assert len(cloud.requests) == 3  # the duplicate never reached the cloud


async def test_fleet_link_needs_the_key_and_a_gateway() -> None:
    async with harness(allowed_connection_kinds="simulated,cloud") as h:  # no key
        owner = await h.owner()
        no_key = await _link(h, owner)
    async with harness(**_settings()) as h:  # a key, but nobody answers the discovery
        owner = await h.owner()
        no_gateway = await _link(h, owner)
    assert no_key.status_code == 503
    assert "MOWDECK_CREDENTIAL_ENCRYPTION_KEY" in no_key.json()["detail"]
    assert no_gateway.status_code == 503


async def test_fleet_linked_accounts_are_for_managers_of_the_account_only() -> None:
    async with harness(**_settings()) as h:
        await _cloud(h.bus)
        owner = await h.owner()
        stranger = await h.owner()
        viewer = await h.member(owner, "viewer")
        linked = (await _link(h, owner)).json()
        base = f"{API}/linked-accounts/{linked['id']}"
        viewer_create = await _link(h, viewer, email="v@example.com")
        viewer_list = await h.client.get(f"{API}/linked-accounts", headers=viewer.headers)
        viewer_devices = await h.client.get(f"{base}/devices", headers=viewer.headers)
        foreign = [
            await h.client.get(base, headers=stranger.headers),
            await h.client.get(f"{base}/devices", headers=stranger.headers),
            await h.client.post(f"{base}/sync", headers=stranger.headers, json={}),
            await h.client.delete(base, headers=stranger.headers),
            await h.client.post(
                f"{base}/mowers",
                headers=stranger.headers,
                json={"mowers": [{"device_name": "Luba-VPA"}]},
            ),
        ]
        stranger_list = await h.client.get(f"{API}/linked-accounts", headers=stranger.headers)
        stolen = await _register(h, stranger, linked["id"], "Luba-VPA")
    assert viewer_create.status_code == 403
    assert viewer_list.status_code == 403 and viewer_devices.status_code == 403
    assert [r.status_code for r in foreign] == [404, 404, 404, 404, 404]
    assert stranger_list.json()["items"] == []
    assert stolen.status_code == 422  # another account's link is unknown here
    assert stolen.json()["errors"][0]["pointer"] == "#/linked_account_id"


# --- cloud mowers from a linked account ------------------------------------------------------


async def test_fleet_cloud_mower_from_a_linked_account() -> None:
    async with harness(**_settings()) as h:
        await _cloud(h.bus)
        owner = await h.owner()
        linked = (await _link(h, owner)).json()
        created = await _register(h, owner, linked["id"], "Luba-VPA")
        devices = await h.client.get(
            f"{API}/linked-accounts/{linked['id']}/devices", headers=owner.headers
        )
        renamed = await h.client.patch(
            f"{API}/mowers/{created.json()['id']}",
            headers=owner.headers,
            json={"name": "Front", "connection_options": {"refresh_seconds": 30}},
        )
        bad_options = await h.client.patch(
            f"{API}/mowers/{created.json()['id']}",
            headers=owner.headers,
            json={"connection_options": {"password": "x"}},
        )
        shard = await h.app.state.fleet.mowers.registrations_for_shard(0, 1)
        stored: Mower = next(iter(h.store.tables.mowers.rows.values()))
        reply = await h.bus.request(
            FLEET_MOWERS_CREDENTIALS,
            MowerCredentialsRequest(mower_id=stored.id, account_id=owner.account_id),
            timeout=1.0,
        )
        secrets = reply.model(MowerCredentialsReply)
        registered, updated = _events(h, "registered"), _events(h, "updated")
    assert created.status_code == 201, created.text
    mower = created.json()
    assert mower["name"] == "Luba-VPA" and mower["model"] == "LUBA 2 AWD 5000X"
    assert mower["linked_account_id"] == linked["id"] and mower["iot_id"] == SHARED_IOT
    assert mower["connection_options"] == {"device_name": "Luba-VPA", "iot_id": SHARED_IOT}
    assert (
        "account" not in stored.connection_options and "password" not in stored.connection_options
    )
    for payload in (registered[0], updated[0], shard[0]):
        options = payload.connection_options
        assert not {"account", "password"} & set(options)  # never in events or the list
        assert options["credentials"] == "fleet" and options["credentials_rev"]
        assert options["linked_account_id"] == linked["id"]
        assert options["device_name"] == "Luba-VPA"
    assert secrets.found
    assert secrets.secret_options == {"email": EMAIL, "password": PASSWORD}
    assert updated[0].connection_options["refresh_seconds"] == 30
    items = {d["device_name"]: d for d in devices.json()["items"]}
    assert items["Luba-VPA"]["registered"] and items["Luba-VPA"]["mower_id"] == mower["id"]
    assert not items["Luba-VPB"]["registered"]
    assert renamed.status_code == 200
    assert renamed.json()["connection_options"]["device_name"] == "Luba-VPA"  # managed, kept
    assert bad_options.status_code == 422
    _no_secret(created.text, devices.text, renamed.text)


async def test_fleet_cloud_registration_errors() -> None:
    async with harness(**_settings()) as h:
        await _cloud(h.bus)
        owner = await h.owner()
        linked = (await _link(h, owner)).json()["id"]
        missing = await h.client.post(
            f"{API}/mowers", headers=owner.headers, json={"connection_kind": "cloud"}
        )
        unknown = await _register(h, owner, linked, "Luba-NOPE")
        rtk = await _register(h, owner, linked, "RTK-1")
        with_secret = await _register(
            h, owner, linked, "Luba-VPB", connection_options={"password": "x"}
        )
        simulated = await h.client.post(
            f"{API}/mowers",
            headers=owner.headers,
            json={"name": "s", "connection_kind": "simulated", "linked_account_id": linked},
        )
        first = await _register(h, owner, linked, "Luba-VPB", name="Back")
        twice = await _register(h, owner, linked, "Luba-VPB")
    assert missing.status_code == 422
    assert {e["pointer"] for e in missing.json()["errors"]} == {
        "#/linked_account_id",
        "#/device_name",
    }
    assert unknown.status_code == 422 and unknown.json()["errors"][0]["code"] == "unknown_device"
    assert rtk.status_code == 422 and rtk.json()["errors"][0]["code"] == "not_a_mower"
    assert with_secret.status_code == 422
    assert simulated.status_code == 422
    assert first.status_code == 201 and first.json()["name"] == "Back"
    assert twice.status_code == 409
    assert twice.json()["type"] == "urn:mowdeck:problem:mower-already-registered"
    assert "Back" in twice.json()["detail"]  # same account: the name may be shown


async def test_fleet_one_physical_mower_in_one_mowdeck_account_only() -> None:
    """A device shared between two Mammotion accounts is still one mower: its IoT id."""
    async with harness(**_settings()) as h:
        await _cloud(h.bus, {"other@example.com": ("other-pass", _devices())})
        first, second = await h.owner(), await h.owner()
        mine = (await _link(h, first)).json()["id"]
        theirs = (await _link(h, second, "other@example.com", "other-pass")).json()["id"]
        taken = await _register(h, first, mine, "Luba-VPA", name="Secret garden")
        refused = await _register(h, second, theirs, "Luba-VPA")
        bulk_refused = await h.client.post(
            f"{API}/linked-accounts/{theirs}/mowers",
            headers=second.headers,
            json={"mowers": [{"device_name": "Luba-VPA"}]},
        )
        seen = await h.client.get(f"{API}/linked-accounts/{theirs}/devices", headers=second.headers)
        removed = await h.client.delete(f"{API}/mowers/{taken.json()['id']}", headers=first.headers)
        freed = await _register(h, second, theirs, "Luba-VPA")
    assert taken.status_code == 201
    assert refused.status_code == 409
    problem = refused.json()
    assert problem["type"] == "urn:mowdeck:problem:mower-already-registered"
    assert "Secret garden" not in refused.text and str(first.account_id) not in refused.text
    assert bulk_refused.status_code == 409
    item = next(d for d in seen.json()["items"] if d["device_name"] == "Luba-VPA")
    assert item["registered_elsewhere"] is True and item["registered"] is False
    assert item["mower_id"] is None
    assert removed.status_code == 204
    assert freed.status_code == 201


async def test_fleet_bulk_registration_from_a_linked_account() -> None:
    async with harness(**_settings()) as h:
        await _cloud(h.bus)
        owner = await h.owner()
        site = await h.create(owner, "sites", name="Garden")
        linked = (await _link(h, owner)).json()["id"]
        url = f"{API}/linked-accounts/{linked}/mowers"
        response = await h.client.post(
            url,
            headers=owner.headers,
            json={
                "mowers": [
                    {"device_name": "Luba-VPA", "name": "Front", "site_id": site["id"]},
                    {"device_name": "Luba-VPB"},
                    {"device_name": "RTK-1"},
                ]
            },
        )
        again = await h.client.post(
            url, headers=owner.headers, json={"mowers": [{"device_name": "Luba-VPA"}]}
        )
        doubled = await h.client.post(
            url,
            headers=owner.headers,
            json={"mowers": [{"device_name": "Luba-VPB"}, {"device_name": "Luba-VPB"}]},
        )
        unknown = await h.client.post(
            f"{API}/linked-accounts/{uuid4()}/mowers",
            headers=owner.headers,
            json={"mowers": [{"device_name": "Luba-VPA"}]},
        )
        tuples = h.tuples_on(f"mower:{response.json()['created'][0]['id']}")
    assert response.status_code == 201, response.text
    body = response.json()
    assert [(m["name"], m["site_id"] == site["id"]) for m in body["created"]] == [
        ("Front", True),
        ("Luba-VPB", False),
    ]
    assert [(f["device_name"], f["status"]) for f in body["failed"]] == [("RTK-1", 422)]
    assert body["failed"][0]["type"] == "urn:mowdeck:problem:validation-failed"
    assert f"parent@site:{site['id']}" in tuples
    assert again.status_code == 409
    assert doubled.status_code == 422
    assert unknown.status_code == 404


async def test_fleet_delete_a_linked_account_only_when_unused() -> None:
    async with harness(**_settings()) as h:
        await _cloud(h.bus)
        owner = await h.owner()
        linked = (await _link(h, owner)).json()["id"]
        mower = (await _register(h, owner, linked, "Luba-VPA", name="Front")).json()
        in_use = await h.client.delete(f"{API}/linked-accounts/{linked}", headers=owner.headers)
        await h.client.delete(f"{API}/mowers/{mower['id']}", headers=owner.headers)
        removed = await h.client.delete(f"{API}/linked-accounts/{linked}", headers=owner.headers)
        gone = await h.client.get(f"{API}/linked-accounts/{linked}", headers=owner.headers)
        removal = _events(h, "removed")
    assert in_use.status_code == 409
    problem = in_use.json()
    assert problem["type"] == "urn:mowdeck:problem:linked-account-in-use"
    assert problem["errors"] == [
        {"pointer": f"#/mowers/{mower['id']}", "detail": "Front", "code": "in_use"}
    ]
    assert removed.status_code == 204 and gone.status_code == 404
    assert "password" not in removal[0].connection_options  # nothing to connect any more


async def test_fleet_sync_rereads_and_records_failures() -> None:
    async with harness(**_settings()) as h:
        cloud = await _cloud(h.bus)
        owner = await h.owner()
        linked = (await _link(h, owner)).json()["id"]
        await _register(h, owner, linked, "Luba-VPA")
        extra = DiscoveredDevice(device_name="Luba-VPC", iot_id="iot-c", is_mower=True)
        cloud.accounts[EMAIL] = (PASSWORD, _devices(extra))
        url = f"{API}/linked-accounts/{linked}/sync"
        synced = await h.client.post(url, headers=owner.headers, json={})
        cloud.accounts[EMAIL] = ("changed-pass", _devices(extra))
        failed = await h.client.post(url, headers=owner.headers, json={})
        after_failure = await h.client.get(f"{API}/linked-accounts/{linked}", headers=owner.headers)
        repeated = await h.client.post(url, headers=owner.headers, json={})
        asked = len(cloud.requests)
        cloud.down = True
        unavailable = await h.client.post(
            url, headers=owner.headers, json={"password": "another-pass"}
        )
        cloud.down = False
        before = (await h.app.state.fleet.mowers.registrations_for_shard(0, 1))[0]
        fixed = await h.client.post(url, headers=owner.headers, json={"password": "changed-pass"})
        updated = _events(h, "updated")
        mower = next(iter(h.store.tables.mowers.rows.values()))
        fetched = await h.app.state.fleet.linked_accounts.credentials(
            MowerCredentialsRequest(mower_id=mower.id, account_id=owner.account_id)
        )
        audit = [e.details for e in h.store.tables.audit.rows.values()]
    assert synced.status_code == 200, synced.text
    assert synced.json()["device_count"] == 4 and synced.json()["mower_count"] == 1
    assert cloud.requests[1].linked_account_id == UUID(linked)
    assert login_of(cloud.requests[1])[1] == PASSWORD  # decrypted from the store
    assert failed.status_code == 422
    assert after_failure.json()["last_sync_error"] == "the vendor cloud refused the credentials"
    assert after_failure.json()["device_count"] == 4  # the cached devices stay
    assert repeated.status_code == 422 and asked == 3  # same wrong password: vendor not asked
    assert unavailable.status_code == 503
    assert fixed.status_code == 200 and fixed.json()["last_sync_error"] is None
    # the new password changes the revision, so the gateway reconnects and fetches it
    rev = updated[-1].connection_options["credentials_rev"]
    assert rev != before.connection_options["credentials_rev"]
    assert "password" not in updated[-1].connection_options
    assert fetched.secret_options["password"] == "changed-pass"  # noqa: S105
    assert "changed-pass" not in fixed.text
    assert not any(EMAIL in str(details) for details in audit)  # the audit names no e-mail


# --- the cipher ------------------------------------------------------------------------------


def test_fleet_cipher_round_trip_binds_the_context() -> None:
    cipher = AesGcmCipher(parse_key(KEY))
    blob = cipher.encrypt("linked-1", PASSWORD)
    assert blob[0] == 1 and blob[1] == key_id(parse_key(KEY)) and PASSWORD.encode() not in blob
    assert cipher.decrypt("linked-1", blob) == PASSWORD
    assert cipher.encrypt("linked-1", PASSWORD) != blob  # a fresh nonce every time
    assert not cipher.needs_reseal(blob)
    with pytest.raises(ValueError, match="wrong key"):
        cipher.decrypt("linked-2", blob)
    with pytest.raises(ValueError, match="wrong key"):
        AesGcmCipher(b"x" * 32).decrypt("linked-1", blob)
    with pytest.raises(ValueError, match="format"):
        cipher.decrypt("linked-1", b"\x02" + blob[1:])
    assert parse_key("ab" * 32) == bytes([0xAB]) * 32
    assert parse_key(base64.urlsafe_b64encode(b"\xfb" * 32).decode().rstrip("=")) == b"\xfb" * 32
    for bad in ("not base64!", base64.b64encode(b"short").decode()):
        with pytest.raises(ValueError, match="credential encryption key"):
            parse_key(bad)
    disabled = cipher_from_key("  ")
    assert isinstance(disabled, DisabledCipher) and not disabled.enabled
    assert not disabled.needs_reseal(blob)
    for call in (lambda: disabled.encrypt("c", "v"), lambda: disabled.decrypt("c", b"")):
        with pytest.raises(ValueError, match="not set"):
            call()
    assert cipher_from_key(KEY).enabled


def test_fleet_cipher_rotation_keeps_old_values_readable() -> None:
    new_key = base64.b64encode(b"n" * 32).decode()
    old_blob = AesGcmCipher(parse_key(KEY)).encrypt("linked-1", PASSWORD)
    rotated = cipher_from_key(new_key, previous=f"{KEY}, ")
    assert isinstance(rotated, AesGcmCipher)
    assert rotated.decrypt("linked-1", old_blob) == PASSWORD
    assert rotated.needs_reseal(old_blob)
    assert not rotated.needs_reseal(rotated.encrypt("linked-1", PASSWORD))
    with pytest.raises(ValueError, match="wrong key"):
        cipher_from_key(new_key).decrypt("linked-1", old_blob)


async def test_fleet_sync_reseals_a_password_of_a_previous_key() -> None:
    new_key = base64.b64encode(b"n" * 32).decode()
    async with harness(**_settings()) as h:
        await _cloud(h.bus)
        owner = await h.owner()
        linked = (await _link(h, owner)).json()["id"]
        old_blob = next(iter(h.store.tables.linked_accounts.rows.values())).password_encrypted
    store = h.store
    async with harness(
        store=store,
        **_settings(credential_encryption_key=new_key),
        credential_encryption_key_previous=KEY,
    ) as h:
        await _cloud(h.bus)
        owner_again = h.person(owner.account_id, permissions=owner.principal.permissions)
        await h.identity.write(
            [
                RelationshipTuple(
                    principal_subject(owner_again.principal), "owner", account(owner.account_id)
                )
            ]
        )
        synced = await h.client.post(
            f"{API}/linked-accounts/{linked}/sync", headers=owner_again.headers, json={}
        )
        new_blob = next(iter(store.tables.linked_accounts.rows.values())).password_encrypted
    assert synced.status_code == 200, synced.text
    assert old_blob[1] == key_id(parse_key(KEY)) and new_blob[1] == key_id(parse_key(new_key))


def test_fleet_refuses_the_development_key_in_production() -> None:
    dev = base64.b64encode(DEV_KEY).decode()
    with pytest.raises(ValidationError, match="development key"):
        Settings.model_validate({"environment": "prod", "credential_encryption_key": dev})
    assert Settings.model_validate({"environment": "dev", "credential_encryption_key": dev})
    assert Settings.model_validate({"environment": "prod", "credential_encryption_key": KEY})


# --- throttle and validation ------------------------------------------------------------------


async def test_fleet_failed_logins_are_throttled_per_email() -> None:
    clock = MovableClock()
    async with harness(**_settings(), clock=clock) as h:
        cloud = await _cloud(h.bus)
        owner = await h.owner()
        first = await _link(h, owner, password="wrong-1")  # noqa: S106 - test values
        again = await _link(h, owner, password="wrong-1")  # noqa: S106
        asked_after_repeat = len(cloud.requests)
        for n in range(2, 6):
            await _link(h, owner, password=f"wrong-{n}")
        blocked = await _link(h, owner)  # even the right password waits now
        asked_when_blocked = len(cloud.requests)
        other_email = await _link(h, owner, email="other@example.com", password="x")  # noqa: S106
        clock.advance(timedelta(minutes=16))
        later = await _link(h, owner)
    assert first.status_code == 422 and again.status_code == 422
    assert asked_after_repeat == 1  # the same wrong password never reached the vendor again
    assert blocked.status_code == 429
    assert blocked.json()["type"] == "urn:mowdeck:problem:too-many-requests"
    assert 0 < int(blocked.headers["Retry-After"]) <= 15 * 60 + 1
    assert asked_when_blocked == 5
    assert other_email.status_code == 422  # another e-mail has its own budget
    assert later.status_code == 201


def test_fleet_login_throttle_unit() -> None:
    clock = MovableClock()
    throttle = LoginThrottle(clock, max_failures=2, window=timedelta(minutes=1))
    key = throttle.key(uuid4(), " A@Example.com ")
    assert key[1] == "a@example.com"
    assert throttle.check(key, "p1")
    throttle.failed(key, "p1")
    assert not throttle.check(key, "p1") and throttle.check(key, "p2")
    throttle.failed(key, "p2")
    with pytest.raises(TooManyLoginAttempts) as raised:
        throttle.check(key, "p3")
    assert raised.value.retry_after_seconds == 61
    clock.advance(timedelta(seconds=61))
    assert throttle.check(key, "p1")  # window passed: the old wrong password may be tried again
    throttle.failed(key, "p1")
    throttle.succeeded(key)
    assert throttle.check(key, "p1")


@pytest.mark.parametrize(
    "email", ["no-at-sign", "a b@example.com", "a@b", "a\x00@example.com", "a@@example.com"]
)
async def test_fleet_link_validates_the_email(email: str) -> None:
    async with harness(**_settings()) as h:
        cloud = await _cloud(h.bus)
        owner = await h.owner()
        refused = await _link(h, owner, email=email)
    assert refused.status_code == 422
    assert cloud.requests == []


async def test_fleet_link_refuses_the_body_of_adr_0034() -> None:
    async with harness(**_settings()) as h:
        owner = await h.owner()
        refused = await h.client.post(
            f"{API}/linked-accounts",
            headers=owner.headers,
            json={"email": EMAIL, "password": PASSWORD},
        )
    assert refused.status_code == 422  # the provider and its credentials are required


class MovableClock:
    """A clock the test moves forward."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta
