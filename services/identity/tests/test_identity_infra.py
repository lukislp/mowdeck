"""OpenFGA outages, healing, signing keys, crypto, module descriptors, events, CLI, tasks."""

import asyncio
import base64
import io
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from identity_support import (
    API,
    PASSWORD,
    TEST_KEK,
    FlakyBackend,
    FlakyStore,
    MutableClock,
    bearer,
    harness,
    make_settings,
)
from mowdeck_auth import SigningKey, StaticKeySource, TokenIssuer
from mowdeck_auth.verifier import JwtVerifier
from mowdeck_authz.testing import InMemoryAuthorizer
from mowdeck_contracts.v1 import ModuleDescriptor, Principal, PrincipalKind
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client
from pydantic import BaseModel

from identity import cli
from identity.adapters.bus.publisher import BusEventPublisher
from identity.adapters.crypto import AesGcmKeyCipher, Argon2PasswordHasher, parse_kek
from identity.adapters.memory import MemoryStore
from identity.adapters.modules import HttpModuleSource
from identity.application.authenticators import AuthenticatorRegistry, GitHubAuthenticator
from identity.application.context import IdentityConfig, RequestContext
from identity.application.keys import KeyManager
from identity.application.users import NewSubUser
from identity.domain.errors import NotFoundError
from identity.domain.models import KeyStatus, Membership, Status
from identity.main import build_app, every, start_background_tasks
from identity.runtime import Runtime


async def test_openfga_outage_answers_503_and_changes_nothing() -> None:
    authz = InMemoryAuthorizer()
    backend = FlakyBackend(authz)
    async with harness(authz=authz, backend=backend) as h:
        lukas = await h.register("lukas")
        backend.fail_writes = True
        create = await h.client.post(
            lukas.url("/users"),
            headers=lukas.headers,
            json={"username": "anna", "password": PASSWORD},
        )
        backend.fail_writes = False
        users = (await h.client.get(lukas.url("/users"), headers=lukas.headers)).json()
        anna_id, _ = await h.subuser(lukas, "anna")
        backend.fail_reads = True
        disable = await h.client.post(lukas.url(f"/users/{anna_id}/disable"), headers=lukas.headers)
        backend.fail_reads = False
        anna = (await h.client.get(lukas.url(f"/users/{anna_id}"), headers=lukas.headers)).json()
        backend.fail_reads = True
        level_check = await h.client.post(
            lukas.url("/role-assignments"),
            headers=lukas.headers,
            json={
                "subject_id": str(anna_id),
                "role_id": str(await h.role_id(lukas, "viewer")),
                "level": {"type": "group", "id": str(uuid4())},
            },
        )
        backend.fail_reads = False
        registered = await h.client.post(
            f"{API}/register",
            json={"username": "x-user", "password": PASSWORD, "account_name": "x"},
        )
    assert create.status_code == 503
    assert create.json()["type"] == "urn:mowdeck:problem:unavailable"
    assert [u["username"] for u in users] == ["lukas"]
    assert disable.status_code == 503
    assert anna["status"] == "active"
    assert level_check.status_code == 503
    assert registered.status_code == 201


async def test_failed_commit_after_the_openfga_write_is_healed() -> None:
    store = FlakyStore()
    async with harness(store=store) as h:
        lukas = await h.register("lukas")
        store.fail_next_commit = True
        create = await h.client.post(
            lukas.url("/users"),
            headers=lukas.headers,
            json={"username": "anna", "password": PASSWORD},
        )
        members = {str(t.subject) for t in h.authz.tuples if t.relation == "member"}
    assert create.status_code == 500
    assert members == {f"user:{lukas.user_id}"}  # the orphan tuple was removed again


async def test_signing_key_rotation_lifecycle() -> None:
    clock = MutableClock()
    store = MemoryStore()
    config = IdentityConfig(key_rotation=timedelta(days=30))
    keys = KeyManager(store, AesGcmKeyCipher(parse_kek(TEST_KEK)), clock, config)
    with pytest.raises(RuntimeError, match="no active signing key"):
        keys.active_key()
    await keys.ensure()
    await keys.ensure()  # idempotent
    first = keys.active_key()
    assert await keys.rotate_step() == []
    clock.advance(timedelta(days=31))
    assert (await keys.rotate_step())[0].startswith("created next")
    assert len(keys.jwks()["keys"]) == 2
    assert keys.active_key().kid == first.kid  # announced, not used yet
    clock.advance(timedelta(minutes=61))
    assert (await keys.rotate_step())[0].startswith("promoted")
    second = keys.active_key()
    assert second.kid != first.kid
    assert await keys.key_for(first.kid) is not None  # old tokens still verify
    clock.advance(timedelta(minutes=61))
    assert (await keys.rotate_step())[0] == f"retired {first.kid}"
    assert [k["kid"] for k in keys.jwks()["keys"]] == [second.kid]
    immediate = await keys.rotate_now(immediate=True)
    assert keys.active_key().kid == immediate.kid
    announced = await keys.rotate_now(immediate=False)
    assert announced.status is KeyStatus.NEXT
    assert (await keys.rotate_now(immediate=False)).kid == announced.kid
    await keys.retire(immediate.kid)
    statuses = {r.kid: r.status for r in await keys.list_keys()}
    assert statuses[immediate.kid] is KeyStatus.RETIRED
    assert list(statuses.values()).count(KeyStatus.ACTIVE) == 1
    with pytest.raises(NotFoundError):
        await keys.retire("nope")
    assert await keys.key_for("unknown-kid") is None


async def test_rotate_step_recreates_a_missing_active_key() -> None:
    store = MemoryStore()
    keys = KeyManager(store, AesGcmKeyCipher(parse_kek(TEST_KEK)), MutableClock(), IdentityConfig())
    assert (await keys.rotate_step())[0].startswith("created active")
    assert keys.active_key()


async def test_tokens_signed_after_rotation_verify_through_the_jwks() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        await h.app.state.runtime.keys.rotate_now(immediate=True)
        fresh = await h.login("lukas")
        jwks = (await h.client.get("/.well-known/jwks.json")).json()
        old_ok = await h.client.get(f"{API}/me", headers=lukas.headers)
        new_ok = await h.client.get(f"{API}/me", headers=bearer(fresh["access_token"]))
    verifier = JwtVerifier(StaticKeySource(jwks))
    assert (await verifier.verify(fresh["access_token"])).subject == lukas.user_id
    assert len(jwks["keys"]) == 2
    assert old_ok.status_code == new_ok.status_code == 200


async def test_token_signed_by_a_foreign_key_is_rejected() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        rogue = SigningKey.generate()
        forged = TokenIssuer(lambda: rogue).issue(
            Principal(subject=lukas.user_id, account_id=lukas.account_id, kind=PrincipalKind.USER)
        )
        response = await h.client.get(f"{API}/me", headers=bearer(forged.token))
    assert response.status_code == 401


def test_key_encryption_key_parsing_and_cipher() -> None:
    raw = b"\x01" * 32
    assert parse_kek(raw.hex()) == raw
    assert parse_kek(base64.b64encode(raw).decode()) == raw
    assert parse_kek(base64.urlsafe_b64encode(raw).decode().rstrip("=")) == raw
    with pytest.raises(ValueError, match="32 bytes"):
        parse_kek(base64.b64encode(b"short").decode())
    with pytest.raises(ValueError, match="base64 or hex"):
        parse_kek("not base64 at all !!!")
    cipher = AesGcmKeyCipher(raw)
    blob = cipher.encrypt("kid-1", b"secret")
    assert cipher.decrypt("kid-1", blob) == b"secret"
    with pytest.raises(ValueError, match="cannot decrypt"):
        cipher.decrypt("kid-2", blob)
    with pytest.raises(ValueError, match="cannot decrypt"):
        AesGcmKeyCipher(b"\x02" * 32).decrypt("kid-1", blob)


async def test_argon2_hasher() -> None:
    hasher = Argon2PasswordHasher("insecure_test", concurrency=1)
    hashed = await hasher.hash("a long password")
    assert hashed.startswith("$argon2id$")
    assert await hasher.verify(hashed, "a long password")
    assert not await hasher.verify(hashed, "another password")
    assert not await hasher.verify("not-a-hash", "x")
    assert not hasher.needs_rehash(hashed)
    assert Argon2PasswordHasher("owasp_min").needs_rehash(hashed)
    assert hasher.needs_rehash("not-a-hash")
    await hasher.dummy_verify("x")


async def test_login_upgrades_an_outdated_hash() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        weak = await Argon2PasswordHasher("insecure_test").hash(PASSWORD)
        stronger = Argon2PasswordHasher("owasp_min")
        h.app.state.identity.deps.hasher = stronger
        h.app.state.identity.authenticators.credential["password"].hasher = stronger
        async with h.store.transaction() as tx:
            user = await tx.users.get(id=lukas.user_id)
            assert user is not None
            user.password_hash = weak
            await tx.users.update(user)
        await h.login("lukas")
        async with h.store.transaction() as tx:
            upgraded = await tx.users.get(id=lukas.user_id)
    assert upgraded is not None and upgraded.password_hash != weak
    assert not stronger.needs_rehash(upgraded.password_hash or "")


async def test_http_module_source() -> None:
    good = ModuleDescriptor(name="fleet", version="1.0.0")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fleet":
            return httpx.Response(200, json=good.model_dump(mode="json"))
        if request.url.host == "broken":
            return httpx.Response(200, json={"name": "INVALID"})
        return httpx.Response(503)

    source = HttpModuleSource(
        ["http://fleet:8000/", "http://broken:8000", "http://down:8000", " "],
        attempts=2,
        delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    fetched = await source.fetch()
    assert [f.url for f in fetched] == [
        "http://fleet:8000",
        "http://broken:8000",
        "http://down:8000",
    ]
    assert fetched[0].descriptor == good
    assert fetched[1].descriptor is None and fetched[2].descriptor is None


class Payload(BaseModel):
    value: int


async def test_event_publisher_without_bus_and_with_a_failing_bus() -> None:
    await BusEventPublisher(None).publish("identity.user.created", None, Payload(value=1))

    class Broken(InMemoryBus):
        async def publish(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("nats down")

    await BusEventPublisher(Broken()).publish("identity.user.created", uuid4(), Payload(value=1))
    bus = InMemoryBus()
    await BusEventPublisher(bus).publish("identity.user.created", uuid4(), Payload(value=2))
    assert bus.published[0].subject == "event.identity.user.created"


def test_authenticator_registry_and_github_stub() -> None:
    registry = AuthenticatorRegistry()
    github = GitHubAuthenticator()
    registry.register(github)
    assert registry.names == ["github"]
    assert registry.get("github") is None  # not a credential authenticator
    with pytest.raises(TypeError):
        registry.register(object())
    with pytest.raises(NotImplementedError):
        asyncio.run(github.start("/"))
    with pytest.raises(NotImplementedError):
        asyncio.run(github.callback({}))


async def test_disabled_password_login_and_account_choice() -> None:
    async with harness(identity_github_login=True) as h:
        lukas = await h.register("lukas")
        bob = await h.register("bob")
        async with h.store.transaction() as tx:
            await tx.memberships.add(
                Membership(uuid4(), bob.account_id, lukas.user_id, Status.ACTIVE, h.clock())
            )
        default = await h.login("lukas")
        chosen = await h.login("lukas", account_id=str(bob.account_id))
        foreign = await h.client.post(
            f"{API}/session",
            json={"username": "bob", "password": PASSWORD, "account_id": str(uuid4())},
        )
        assert "github" in h.app.state.identity.authenticators.names
        h.app.state.identity.authenticators.credential.pop("password")
        h.app.state.identity.authenticators.credential.pop("api_token")
        disabled = await h.client.post(
            f"{API}/session", json={"username": "lukas", "password": PASSWORD}
        )
        no_tokens = await h.client.post(f"{API}/token", json={"token": "mdk_x"})
    assert default["account_id"] == str(lukas.account_id)
    assert chosen["account_id"] == str(bob.account_id)
    assert foreign.status_code == 401
    assert disabled.status_code == 403
    assert no_tokens.status_code == 403


async def test_api_token_validation_errors() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        level = {"type": "account", "id": str(lukas.account_id)}
        grant = [{"level": level, "permissions": ["mower.view"]}]
        past = await h.client.post(
            lukas.url("/api-tokens"),
            headers=lukas.headers,
            json={"name": "x", "grants": grant, "expires_at": "2000-01-01T00:00:00Z"},
        )
        blank = await h.client.post(
            lukas.url("/api-tokens"), headers=lukas.headers, json={"name": "  ", "grants": grant}
        )
        future = await h.client.post(
            lukas.url("/api-tokens"),
            headers=lukas.headers,
            json={"name": "ha", "grants": grant, "expires_at": "2999-01-01T00:00:00Z"},
        )
        token = (
            await h.client.post(f"{API}/token", json={"token": future.json()["token"]})
        ).json()["access_token"]
        by_token = await h.client.post(
            lukas.url("/api-tokens"), headers=bearer(token), json={"name": "x", "grants": grant}
        )
        logout_all = await h.client.post(f"{API}/session/logout-all", headers=bearer(token))
        missing = await h.client.delete(lukas.url(f"/api-tokens/{uuid4()}"), headers=lukas.headers)
        twice = [
            await h.client.delete(
                lukas.url(f"/api-tokens/{future.json()['id']}"), headers=lukas.headers
            )
            for _ in range(2)
        ]
    assert past.status_code == 422
    assert blank.status_code == 422
    assert future.status_code == 201
    assert by_token.status_code == 403  # tokens have no users.manage
    assert logout_all.status_code == 403
    assert missing.status_code == 404
    assert [r.status_code for r in twice] == [204, 204]


async def test_misc_validation_paths() -> None:
    async with harness() as h:
        lukas = await h.register("lukas")
        anna_id, anna_token = await h.subuser(lukas, "anna")
        bad_display = await h.client.patch(
            lukas.url(f"/users/{anna_id}"), headers=lukas.headers, json={"display_name": " "}
        )
        unknown_user = await h.client.post(
            lukas.url(f"/users/{uuid4()}/disable"), headers=lukas.headers
        )
        unknown_role = await h.client.post(
            lukas.url("/role-assignments"),
            headers=lukas.headers,
            json={
                "subject_id": str(anna_id),
                "role_id": str(uuid4()),
                "level": {"type": "account", "id": str(lukas.account_id)},
            },
        )
        unknown_subject = await h.client.post(
            lukas.url("/role-assignments"),
            headers=lukas.headers,
            json={
                "subject_id": str(uuid4()),
                "role_id": str(await h.role_id(lukas, "viewer")),
                "level": {"type": "account", "id": str(lukas.account_id)},
            },
        )
        admin_on_group = await h.client.post(
            lukas.url("/role-assignments"),
            headers=lukas.headers,
            json={
                "subject_id": str(anna_id),
                "role_id": str(await h.role_id(lukas, "admin")),
                "level": {"type": "group", "id": str(uuid4())},
            },
        )
        viewer = str(await h.role_id(lukas, "viewer"))
        level = {"type": "account", "id": str(lukas.account_id)}
        first = await h.client.post(
            lukas.url("/role-assignments"),
            headers=lukas.headers,
            json={"subject_id": str(anna_id), "role_id": viewer, "level": level},
        )
        again = await h.client.post(
            lukas.url("/role-assignments"),
            headers=lukas.headers,
            json={"subject_id": str(anna_id), "role_id": viewer, "level": level},
        )
        missing_assignment = await h.client.delete(
            lukas.url(f"/role-assignments/{uuid4()}"), headers=lukas.headers
        )
        missing_role = await h.client.patch(
            lukas.url(f"/roles/{uuid4()}"), headers=lukas.headers, json={"name": "x"}
        )
        bad_role_name = await h.client.patch(
            lukas.url(f"/roles/{viewer}"), headers=lukas.headers, json={"name": " "}
        )
        long_name = await h.client.post(
            lukas.url("/roles"), headers=lukas.headers, json={"name": " ", "permissions": []}
        )
        description = await h.client.patch(
            lukas.url(f"/roles/{viewer}"), headers=lukas.headers, json={"description": "Look only"}
        )
        bad_account_name = await h.client.patch(
            lukas.url(), headers=lukas.headers, json={"name": " "}
        )
        set_pw_unknown = await h.client.post(
            lukas.url(f"/users/{uuid4()}/password"),
            headers=lukas.headers,
            json={"password": "temporary password"},
        )
        anna_me = await h.client.get(lukas.url(f"/users/{anna_id}"), headers=bearer(anna_token))
    assert bad_display.status_code == 422
    assert unknown_user.status_code == 404
    assert unknown_role.status_code == 404
    assert unknown_subject.status_code == 404
    assert admin_on_group.status_code == 404  # the level is checked first
    assert first.status_code == 201 and again.json()["id"] == first.json()["id"]
    assert missing_assignment.status_code == 404
    assert missing_role.status_code == 404
    assert bad_role_name.status_code == 422
    assert long_name.status_code == 422
    assert description.json()["description"] == "Look only"
    assert bad_account_name.status_code == 422
    assert set_pw_unknown.status_code == 404
    assert anna_me.status_code == 200


def runtime_for(store: MemoryStore, authz: InMemoryAuthorizer) -> Runtime:
    return Runtime(
        make_settings(),
        store=store,
        authorizer=authz,
        tuple_backend=authz,
        hasher=Argon2PasswordHasher("insecure_test"),
    )


async def test_cli_commands(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, authz = MemoryStore(), InMemoryAuthorizer()
    monkeypatch.setenv(cli.PASSWORD_ENV, PASSWORD)

    async def run(*argv: str) -> tuple[int, str, str]:
        code = await cli.amain(list(argv), runtime_for(store, authz))
        out = capsys.readouterr()
        return code, out.out, out.err

    code, out, _ = await run("create-user", "lukas", "--account-name", "Home")
    assert code == 0 and out.startswith("created account")
    account_id = out.split()[2]
    code, out, _ = await run("list-keys")
    assert code == 0 and "active" in out
    code, out, _ = await run("rotate-keys")
    assert out.startswith("next key")
    code, out, _ = await run("rotate-keys", "--now")
    assert out.startswith("active key")
    kid = out.split()[-1]
    # A kid is base64url and may start with "-", which argparse would read as an option.
    code, out, _ = await run("retire-key", "--", kid)
    assert out == f"retired {kid}\n"
    code, out, _ = await run("reconcile")
    assert code == 0 and out.startswith("reconciled")
    code, out, _ = await run("reconcile", "--account", account_id)
    assert code == 0
    code, _, err = await run("promote-owner", "ghost", "--account", account_id)
    assert code == 1 and "no such member" in err
    code, _, err = await run("create-user", "lukas", "--account-name", "Again")
    assert code == 1 and "taken" in err
    services = await runtime_for(store, authz).start()
    async with store.transaction() as tx:
        owner = await tx.users.get(username_norm="lukas")
    assert owner is not None
    ctx = RequestContext(principal=Principal(subject=owner.id, kind=PrincipalKind.USER))
    await services.users.create_subuser(UUID(account_id), NewSubUser("anna", PASSWORD), ctx)
    code, out, _ = await run("promote-owner", "anna", "--account", account_id)
    assert code == 0
    relations = {(str(t.subject), t.relation) for t in authz.tuples if t.object.id == account_id}
    assert (f"user:{owner.id}", "admin") in relations
    assert (f"user:{owner.id}", "owner") not in relations
    assert any(rel == "owner" and subject != f"user:{owner.id}" for subject, rel in relations)
    assert cli.parser().parse_args(["list-keys"]).command == "list-keys"


async def test_cli_reads_the_password_from_a_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cli.PASSWORD_ENV, raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("from a pipe\n"))
    assert cli.read_password() == "from a pipe"


async def test_background_tasks_run_and_stop() -> None:
    calls: list[str] = []

    async def flaky() -> None:
        calls.append("x")
        if len(calls) == 1:
            raise RuntimeError("first run fails")

    task = asyncio.create_task(every(0.01, "test", flaky))
    await asyncio.sleep(0.08)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 2

    store, authz = MemoryStore(), InMemoryAuthorizer()
    settings = make_settings().model_copy(
        update={
            "identity_maintenance_seconds": 0.01,
            "identity_reconcile_seconds": 0.01,
            "identity_catalogue_refresh_seconds": 0.01,
        }
    )
    runtime = runtime_for(store, authz)
    runtime.settings = settings
    await runtime.start()
    tasks = start_background_tasks(settings, runtime)
    await asyncio.sleep(0.1)
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert [t.get_name() for t in tasks] == ["catalogue", "maintenance", "reconcile"]


async def test_app_with_background_tasks_starts_and_stops() -> None:
    app = build_app(
        make_settings(),
        bus=InMemoryBus(),
        store=MemoryStore(),
        authorizer=InMemoryAuthorizer(),
        hasher=Argon2PasswordHasher("insecure_test"),
        tuple_backend=InMemoryAuthorizer(),
    )
    async with lifespan_client(app) as client:
        ready = await client.get("/readyz")
    assert ready.status_code == 200
    assert set(ready.json()["checks"]) >= {"signing_key", "authz", "nats"}


def test_settings() -> None:
    settings = make_settings(identity_module_urls=" http://a:8000 , ,http://b:8000")
    assert settings.module_urls == ["http://a:8000", "http://b:8000"]
    config = settings.to_config()
    assert config.access_token_ttl == timedelta(minutes=10)
    assert config.refresh_absolute == timedelta(days=30)
    with pytest.raises(ValueError, match="KEY_ENCRYPTION_KEY"):
        build_app(make_settings(identity_key_encryption_key=None), store=MemoryStore())


async def test_concurrent_first_key_creation_loads_the_winner() -> None:
    store = MemoryStore()
    cipher = AesGcmKeyCipher(parse_kek(TEST_KEK))
    first = KeyManager(store, cipher, MutableClock(), IdentityConfig())
    await first.ensure()

    second = KeyManager(store, cipher, MutableClock(), IdentityConfig())
    original = store.tables.signing_keys.find

    async def blind_find(**where: Any) -> Any:  # sees no active key, then loses the insert
        if where.get("status") == KeyStatus.ACTIVE.value:
            return []
        return await original(**where)

    store.tables.signing_keys.find = blind_find  # type: ignore[method-assign]
    store.tables.signing_keys.unique = [("status",)]
    await second.ensure()
    store.tables.signing_keys.find = original  # type: ignore[method-assign]
    assert second.active_key().kid == first.active_key().kid
