"""Cloud providers in Fleet (ADR 0065): the provider list, credential fields of any shape, their
storage, the credentials a linked mower's connection gets, re-authentication, and accounts
stored before ADR 0065.

A fake Device-Gateway lists two neutral providers (:mod:`fleet_cloud`): ``acme`` (e-mail and
password) and ``zeta`` (application key and secret plus a region).
"""

import base64
import json
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from fleet_cloud import ACME, ZETA, FakeProviders, link_body, serve_providers
from fleet_support import API, Harness, Person, harness
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

from fleet.adapters.crypto import AesGcmCipher, parse_key
from fleet.domain.models import SECRET_FORMAT_FIELDS, SECRET_FORMAT_PASSWORD, LinkedAccount

KEY = base64.b64encode(b"k" * 32).decode()
APP_KEY = "zeta-app-key-0001"
APP_SECRET = "zeta-app-secret"  # noqa: S105 - a test value
EMAIL = "garden@example.com"
PASSWORD = "acme-password"  # noqa: S105 - a test value
DEVICES = [DiscoveredDevice(device_name="Zeta-1", iot_id="zeta-iot-1", is_mower=True)]


class FakeClouds:
    """Answers discoveries of both providers; records the requests."""

    def __init__(self) -> None:
        self.requests: list[CloudDiscoverRequest] = []
        self.secrets = {"acme": PASSWORD, "zeta": APP_SECRET}

    async def handle(self, message: BusMessage) -> None:
        request = message.model(CloudDiscoverRequest)
        self.requests.append(request)
        key = "password" if request.provider == "acme" else "app_secret"
        if request.credentials.get(key) != self.secrets[request.provider]:
            error = CloudDiscoverError(code=CloudDiscoverErrorCode.LOGIN_FAILED, detail="no")
            await message.respond(CloudDiscoverReply(error=error))
            return
        devices = DEVICES if request.provider == "zeta" else []
        await message.respond(CloudDiscoverReply(devices=devices))


async def _gateway(bus: InMemoryBus, *descriptors: Any) -> tuple[FakeClouds, FakeProviders]:
    clouds = FakeClouds()
    await bus.subscribe(GATEWAY_CLOUD_DISCOVER, clouds.handle, queue="device-gateway")
    providers = await serve_providers(bus, *(descriptors or (ACME, ZETA)))
    return clouds, providers


def _settings() -> dict[str, Any]:
    return {"credential_encryption_key": KEY, "allowed_connection_kinds": "simulated,cloud"}


def _zeta(**changes: str) -> dict[str, Any]:
    credentials = {"app_key": APP_KEY, "app_secret": APP_SECRET, "region": "eu", **changes}
    return {"provider": "zeta", "credentials": credentials}


async def _post(h: Harness, who: Person, body: Mapping[str, Any]) -> Any:
    return await h.client.post(f"{API}/linked-accounts", headers=who.headers, json=dict(body))


def _pointers(response: Any) -> set[str]:
    return {e["pointer"] for e in response.json()["errors"]}


async def test_fleet_lists_the_enabled_providers() -> None:
    async with harness(**_settings()) as h:
        _, providers = await _gateway(h.bus)
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        listed = await h.client.get(f"{API}/linked-accounts/providers", headers=owner.headers)
        again = await h.client.get(f"{API}/linked-accounts/providers", headers=owner.headers)
        forbidden = await h.client.get(f"{API}/linked-accounts/providers", headers=viewer.headers)
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert [i["provider"] for i in items] == ["acme", "zeta"]
    zeta = items[1]
    assert zeta["needs_developer_account"] and not zeta["tested_on_hardware"]
    assert [f["key"] for f in zeta["fields"]] == ["app_key", "app_secret", "region"]
    assert zeta["links"][0]["url"] == "https://developer.zeta.test"
    assert again.json() == listed.json() and providers.requests == 1  # cached
    assert forbidden.status_code == 403


async def test_fleet_providers_need_a_gateway() -> None:
    async with harness(**_settings()) as h:
        owner = await h.owner()
        listed = await h.client.get(f"{API}/linked-accounts/providers", headers=owner.headers)
        linked = await _post(h, owner, _zeta())
    assert listed.status_code == 503 and linked.status_code == 503


async def test_fleet_link_checks_the_credentials_against_the_provider() -> None:
    async with harness(**_settings()) as h:
        clouds, _ = await _gateway(h.bus)
        owner = await h.owner()
        unknown = await _post(h, owner, {"provider": "omega", "credentials": {"a": "b"}})
        missing = await _post(
            h, owner, {"provider": "zeta", "credentials": {"app_key": APP_KEY, "region": "eu"}}
        )
        bad = await _post(h, owner, _zeta(region="mars", extra="x"))
        not_mail = await _post(h, owner, link_body("a@b", PASSWORD))
    assert unknown.status_code == 422 and _pointers(unknown) == {"#/provider"}
    assert missing.status_code == 422 and _pointers(missing) == {"#/credentials/app_secret"}
    assert _pointers(bad) == {"#/credentials/region", "#/credentials/extra"}
    assert not_mail.status_code == 422 and _pointers(not_mail) == {"#/credentials/email"}
    assert APP_SECRET not in missing.text + bad.text
    assert clouds.requests == []  # nothing reached a vendor


async def test_fleet_link_any_provider_stores_public_fields_and_seals_the_secrets() -> None:
    async with harness(**_settings()) as h:
        clouds, _ = await _gateway(h.bus)
        owner = await h.owner()
        created = await _post(h, owner, _zeta(app_key=f" {APP_KEY} "))
        twice = await _post(h, owner, _zeta())
        refused = await _post(h, owner, _zeta(app_key="other-key", app_secret="wrong"))  # noqa: S106
        same_identity_other_provider = await _post(
            h, owner, link_body(f"{APP_KEY}@x.test", PASSWORD)
        )
        stored = next(
            r for r in h.store.tables.linked_accounts.rows.values() if r.provider == "zeta"
        )
        listed = await h.client.get(f"{API}/linked-accounts", headers=owner.headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["provider"] == "zeta" and body["identity"] == APP_KEY == body["email"]
    assert body["public_credentials"] == {"app_key": APP_KEY, "region": "eu"}
    assert [d["device_name"] for d in body["devices"]] == ["Zeta-1"]
    assert APP_SECRET not in created.text + listed.text
    assert clouds.requests[0].credentials == {
        "app_key": APP_KEY,
        "app_secret": APP_SECRET,
        "region": "eu",
    }
    assert twice.status_code == 409 and "Zeta Connect" in twice.json()["detail"]
    assert refused.status_code == 422
    assert refused.json()["errors"][0]["pointer"] == "#/credentials/app_secret"
    assert same_identity_other_provider.status_code == 201
    assert stored.secret_format == SECRET_FORMAT_FIELDS and stored.email == APP_KEY
    assert stored.public_credentials == {"app_key": APP_KEY, "region": "eu"}
    assert APP_SECRET.encode() not in stored.password_encrypted
    opened = AesGcmCipher(parse_key(KEY)).decrypt(str(stored.id), stored.password_encrypted)
    assert json.loads(opened) == {"app_secret": APP_SECRET}


def _events(h: Harness, event_type: str) -> list[MowerRegistration]:
    return [
        m.model(EventEnvelope[MowerRegistration]).payload
        for m in h.bus.messages(f"event.mower.{event_type}")
    ]


async def test_fleet_linked_mower_gets_every_field_and_reauthentication_republishes() -> None:
    async with harness(**_settings()) as h:
        clouds, _ = await _gateway(h.bus)
        owner = await h.owner()
        linked = (await _post(h, owner, _zeta())).json()["id"]
        mower = await h.client.post(
            f"{API}/mowers",
            headers=owner.headers,
            json={"connection_kind": "cloud", "linked_account_id": linked, "device_name": "Zeta-1"},
        )
        mower_id = mower.json()["id"]
        reply = await h.bus.request(
            FLEET_MOWERS_CREDENTIALS,
            MowerCredentialsRequest(mower_id=mower_id, account_id=owner.account_id),
            timeout=1.0,
        )
        url = f"{API}/linked-accounts/{linked}/sync"
        not_secret = await h.client.post(
            url, headers=owner.headers, json={"credentials": {"app_key": "x"}}
        )
        clouds.secrets["zeta"] = "rotated-secret"
        stale = await h.client.post(url, headers=owner.headers, json={})
        fixed = await h.client.post(
            url, headers=owner.headers, json={"credentials": {"app_secret": "rotated-secret"}}
        )
        after = await h.bus.request(
            FLEET_MOWERS_CREDENTIALS,
            MowerCredentialsRequest(mower_id=mower_id, account_id=owner.account_id),
            timeout=1.0,
        )
        registered, updated = _events(h, "registered"), _events(h, "updated")
    assert mower.status_code == 201, mower.text
    assert reply.model(MowerCredentialsReply).secret_options == {
        "app_key": APP_KEY,
        "app_secret": APP_SECRET,
        "region": "eu",
    }
    options = registered[0].connection_options
    assert options["cloud_provider"] == "zeta" and options["credentials"] == "fleet"
    assert not {"app_key", "app_secret", "region"} & set(options)
    assert not_secret.status_code == 422 and _pointers(not_secret) == {"#/credentials/app_key"}
    assert stale.status_code == 422  # the vendor revoked the old secret
    assert fixed.status_code == 200, fixed.text
    assert clouds.requests[-1].credentials["app_key"] == APP_KEY  # kept from the store
    assert updated[-1].connection_options["credentials_rev"] != options["credentials_rev"]
    assert after.model(MowerCredentialsReply).secret_options["app_secret"] == "rotated-secret"  # noqa: S105
    assert "rotated-secret" not in fixed.text


async def test_fleet_reads_accounts_linked_before_adr_0065() -> None:
    """A row of ADR 0034: the e-mail and one sealed password (``secret_format`` 1)."""
    async with harness(**_settings()) as h:
        clouds, _ = await _gateway(h.bus)
        owner = await h.owner()
        row_id = uuid4()
        now = h.app.state.fleet.linked_accounts.clock()
        sealed = AesGcmCipher(parse_key(KEY)).encrypt(str(row_id), PASSWORD)
        row = LinkedAccount(
            id=row_id,
            account_id=owner.account_id,
            provider="acme",
            email=EMAIL,
            password_encrypted=sealed,
            created_at=now,
            updated_at=now,
        )
        async with h.store.transaction() as tx:
            await tx.linked_accounts.add(row)
        listed = await h.client.get(f"{API}/linked-accounts", headers=owner.headers)
        url = f"{API}/linked-accounts/{row_id}/sync"
        synced = await h.client.post(url, headers=owner.headers, json={})
        kept = next(iter(h.store.tables.linked_accounts.rows.values()))
        format_after_plain_sync = kept.secret_format
        clouds.secrets["acme"] = "new-password"
        changed = await h.client.post(url, headers=owner.headers, json={"password": "new-password"})
        moved = next(iter(h.store.tables.linked_accounts.rows.values()))
    item = listed.json()["items"][0]
    assert item["identity"] == EMAIL and item["public_credentials"] == {"email": EMAIL}
    assert synced.status_code == 200, synced.text
    assert clouds.requests[0].credentials == {"email": EMAIL, "password": PASSWORD}
    assert format_after_plain_sync == SECRET_FORMAT_PASSWORD  # nothing to re-seal
    assert changed.status_code == 200, changed.text  # the deprecated `password` still works
    assert moved.secret_format == SECRET_FORMAT_FIELDS
    assert moved.public_credentials == {"email": EMAIL}


async def test_fleet_sync_of_a_disabled_provider_is_unavailable() -> None:
    async with harness(**_settings()) as h:
        _, providers = await _gateway(h.bus)
        owner = await h.owner()
        linked = (await _post(h, owner, _zeta())).json()["id"]
        providers.descriptors = [ACME]  # the operator switched zeta off
        h.app.state.fleet.linked_accounts.catalog._cached = None
        synced = await h.client.post(
            f"{API}/linked-accounts/{linked}/sync", headers=owner.headers, json={}
        )
        listed = await h.client.get(f"{API}/linked-accounts", headers=owner.headers)
    assert synced.status_code == 503
    assert [i["provider"] for i in listed.json()["items"]] == ["zeta"]  # still listed
