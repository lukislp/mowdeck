"""MediaMTX's auth callback: who may read, publish and use the API (ADR 0045)."""

from uuid import uuid4

import httpx
import pytest
from mowdeck_contracts.v1.relay import RelayDetachRequest
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client
from relay_support import FakeClock, Harness, attach_request, key

from camera_relay.application.media_auth import MediaAuthorizer, MediaAuthRequest, WhepBindings
from camera_relay.domain.tickets import camera_path
from camera_relay.main import build_app
from camera_relay.settings import Settings

SECRET = "internal-secret"  # noqa: S105 - test value


async def _live(h: Harness, cameras: list[str] | None = None) -> tuple[str, str, object]:
    """A live viewer lease: its ticket, its first path and the lease id."""
    relay_key = key()
    reply = await h.service.attach(attach_request(relay_key, cameras))
    assert reply.session is not None and reply.session.relay is not None
    session = h.repository.get(relay_key)
    assert session is not None and session.relay_id is not None
    path = camera_path(session.relay_id, (cameras or ["front_left"])[0])
    return reply.session.relay.ticket, path, reply.session.relay.lease_id


def _authorizer(h: Harness, bindings: WhepBindings | None = None) -> MediaAuthorizer:
    return MediaAuthorizer(
        repository=h.repository,
        signer=h.signer,
        clock=h.clock,
        bindings=bindings or WhepBindings(),
        publisher_user="relay",
        publisher_secret=lambda: SECRET,
    )


def _read(path: str, token: str, session_id: str | None = "s-1") -> MediaAuthRequest:
    return MediaAuthRequest(
        action="read", path=path, protocol="webrtc", token=token, session_id=session_id
    )


async def test_a_valid_ticket_reads_its_path_and_binds_the_reader() -> None:
    h = Harness()
    ticket, path, lease_id = await _live(h)
    bindings = WhepBindings()
    decision = _authorizer(h, bindings).decide(_read(path, ticket, "whep-1"))
    assert decision.allowed, decision.reason
    assert decision.lease_id == lease_id
    assert bindings.lease_of("whep-1") == lease_id


async def test_a_ticket_opens_only_its_own_paths() -> None:
    h = Harness()
    ticket, path, _ = await _live(h, ["front_left"])
    authorizer = _authorizer(h)
    other_camera = path.rsplit("/", 1)[0] + "/front_right"
    assert not authorizer.decide(_read(other_camera, ticket)).allowed
    assert not authorizer.decide(_read(f"{path}/src", ticket)).allowed
    assert not authorizer.decide(_read("r/" + "0" * 32 + "/front_left", ticket)).allowed
    assert not authorizer.decide(_read("somewhere/else", ticket)).allowed


async def test_a_ticket_of_another_account_opens_nothing_of_this_one() -> None:
    h = Harness()
    ticket_a, path_a, _ = await _live(h)
    ticket_b, path_b, _ = await _live(h)
    authorizer = _authorizer(h)
    assert not authorizer.decide(_read(path_a, ticket_b)).allowed
    assert not authorizer.decide(_read(path_b, ticket_a)).allowed
    assert authorizer.decide(_read(path_a, ticket_a)).allowed


async def test_no_expired_tampered_or_foreign_tickets() -> None:
    h = Harness()
    ticket, path, _ = await _live(h)
    authorizer = _authorizer(h)
    assert not authorizer.decide(_read(path, "")).allowed
    assert not authorizer.decide(_read(path, ticket[:-3] + "AAA")).allowed
    assert not authorizer.decide(_read(path, "v1.e30.AAAA")).allowed
    foreign = Harness()
    foreign_ticket, _, _ = await _live(foreign)
    assert not authorizer.decide(_read(path, foreign_ticket)).allowed
    h.clock.advance(61)  # tickets live 60 s
    expired = authorizer.decide(_read(path, ticket))
    assert not expired.allowed
    assert "expired" in expired.reason


async def test_a_closed_lease_or_a_stopped_session_reads_nothing() -> None:
    h = Harness()
    relay_key = key()
    reply = await h.service.attach(attach_request(relay_key))
    assert reply.session is not None and reply.session.relay is not None
    session = h.repository.get(relay_key)
    assert session is not None and session.relay_id is not None
    path = camera_path(session.relay_id, "front_left")
    ticket = reply.session.relay.ticket
    second = await h.service.attach(attach_request(relay_key))
    assert second.session is not None and second.session.relay is not None
    await h.service.detach(
        RelayDetachRequest(
            account_id=relay_key.account_id,
            mower_id=relay_key.mower_id,
            lease_id=reply.session.relay.lease_id,
            requested_by=attach_request(relay_key).requested_by,
        )
    )
    authorizer = _authorizer(h)
    closed = authorizer.decide(_read(path, ticket))
    assert not closed.allowed and "lease" in closed.reason
    assert authorizer.decide(_read(path, second.session.relay.ticket)).allowed


async def test_read_needs_webrtc_and_other_protocols_are_refused() -> None:
    h = Harness()
    ticket, path, _ = await _live(h)
    authorizer = _authorizer(h)
    for protocol in ("hls", "rtmp", "srt", ""):
        request = MediaAuthRequest(action="read", path=path, protocol=protocol, token=ticket)
        assert not authorizer.decide(request).allowed, protocol


@pytest.mark.parametrize(
    ("user", "password", "allowed"),
    [("relay", SECRET, True), ("relay", "wrong", False), ("", "", False), ("other", SECRET, False)],
)
async def test_publish_needs_the_internal_credential(
    user: str, password: str, allowed: bool
) -> None:
    authorizer = _authorizer(Harness())
    path = "r/" + "a" * 32 + "/front_left"
    for target in (path, f"{path}/src"):
        request = MediaAuthRequest(
            action="publish", path=target, protocol="rtsp", user=user, password=password
        )
        assert authorizer.decide(request).allowed is allowed
    # The transcoder reads .../src over RTSP with the same credential.
    rtsp_read = MediaAuthRequest(
        action="read", path=f"{path}/src", protocol="rtsp", user=user, password=password
    )
    assert authorizer.decide(rtsp_read).allowed is allowed


async def test_publish_only_on_relay_paths() -> None:
    authorizer = _authorizer(Harness())
    for path in ("live", "r/abc/front_left", "r/" + "a" * 32, "../r/" + "a" * 32 + "/x"):
        request = MediaAuthRequest(
            action="publish", path=path, protocol="rtsp", user="relay", password=SECRET
        )
        assert not authorizer.decide(request).allowed, path


async def test_api_only_for_the_relay_and_metrics_playback_pprof_never() -> None:
    authorizer = _authorizer(Harness())
    api = MediaAuthRequest(action="api", user="relay", password=SECRET)
    assert authorizer.decide(api).allowed
    assert not authorizer.decide(MediaAuthRequest(action="api")).allowed
    wrong = MediaAuthRequest(action="api", user="x", password="y")  # noqa: S106
    assert not authorizer.decide(wrong).allowed
    for action in ("metrics", "playback", "pprof", "unknown", ""):
        with_credential = MediaAuthRequest(action=action, user="relay", password=SECRET)
        assert not authorizer.decide(with_credential).allowed, action


def test_requests_never_show_secrets_in_repr() -> None:
    request = MediaAuthRequest(action="read", password="pw-secret", token="tk-secret")  # noqa: S106
    assert "pw-secret" not in repr(request)
    assert "tk-secret" not in repr(request)


def _settings(**overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "environment": "test",
            "log_format": "console",
            "relay_mediamtx_api_url": "http://mediamtx:9997",
            "relay_publisher_secret": SECRET,
            **overrides,
        }
    )


async def test_the_callback_route_answers_200_or_401_and_logs_no_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = build_app(_settings(), bus=InMemoryBus(), clock=FakeClock(), background_tasks=False)
    async with lifespan_client(app) as client:
        allowed = await client.post(
            "/internal/mediamtx/auth",
            json={
                "user": "relay",
                "password": SECRET,
                "token": SECRET,
                "action": "publish",
                "path": "r/" + "b" * 32 + "/front_left",
                "protocol": "rtsp",
                "id": str(uuid4()),
                "query": "",
                "userAgent": "Lavf61",
            },
        )
        assert allowed.status_code == 200
        denied = await client.post(
            "/internal/mediamtx/auth",
            json={
                "token": "v1.secret-token-value.x",
                "action": "read",
                "path": "r/" + "b" * 32 + "/front_left",
                "protocol": "webrtc",
                "id": str(uuid4()),
            },
        )
        assert denied.status_code == 401
        metrics = await client.post("/internal/mediamtx/auth", json={"action": "metrics"})
        assert metrics.status_code == 401
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert "secret-token-value" not in output.out + output.err


async def test_the_freeze_hook_exists_only_with_test_hooks() -> None:
    without = build_app(_settings(), bus=InMemoryBus(), background_tasks=False)
    async with lifespan_client(without) as client:
        response = await client.post("/internal/v1/test/freeze", json={"frozen": True})
        assert response.status_code == 404
    with_hooks = build_app(
        _settings(relay_test_hooks=True), bus=InMemoryBus(), background_tasks=False
    )
    async with lifespan_client(with_hooks) as client:
        response = await client.post("/internal/v1/test/freeze", json={"frozen": True})
        assert response.status_code == 200
        assert response.json() == {"frozen": True, "changed": 0}
        bad = await client.post("/internal/v1/test/freeze", json={"relay_id": "../x"})
        assert bad.status_code == 422


def test_test_hooks_are_refused_in_production() -> None:
    with pytest.raises(ValueError, match="TEST_HOOKS"):
        _settings(environment="prod", relay_test_hooks=True)


def test_without_a_media_server_no_media_stack_is_built() -> None:
    app = build_app(
        Settings.model_validate({"environment": "test", "log_format": "console"}),
        bus=InMemoryBus(),
    )
    assert app.state.media is None


async def test_the_api_client_authenticates_with_the_internal_credential() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"itemCount": 0, "pageCount": 0, "items": []})

    app = build_app(
        _settings(),
        bus=InMemoryBus(),
        background_tasks=False,
        media_transport=httpx.MockTransport(handler),
    )
    assert await app.state.media.api.webrtc_sessions() == []
    assert seen[0].headers["authorization"] == httpx.BasicAuth("relay", SECRET)._auth_header
    assert seen[0].url.path == "/v3/webrtcsessions/list"
    await app.state.media.api.close()
