"""Client-credentials token, its cache, refusals and the request throttle."""

from collections.abc import Mapping

import pytest
from aioautomower.exceptions import (
    ApiBadRequestError,
    ApiError,
    ApiForbiddenError,
    ApiUnauthorizedError,
)
from husqvarna_support import APP_KEY, SECRET, ManualClock, ScriptedTransport
from mowdeck_control import CloudLoginRefused, CloudUnavailable
from mowdeck_control_husqvarna.auth import (
    TOKEN_MARGIN_S,
    ClientCredentialsAuth,
    RateLimitedError,
    RequestThrottle,
    TokenSource,
    urgent,
)
from mowdeck_control_husqvarna.transport import HttpReply


def _tokens(transport: ScriptedTransport, clock: ManualClock) -> TokenSource:
    return TokenSource(transport, APP_KEY, SECRET, clock=clock)


async def test_token_request_is_a_client_credentials_grant() -> None:
    transport = ScriptedTransport()
    token = await _tokens(transport, ManualClock()).token()
    assert token == "token-1"  # noqa: S105 - the scripted token
    assert transport.forms == [
        {"grant_type": "client_credentials", "client_id": APP_KEY, "client_secret": SECRET}
    ]


async def test_token_is_cached_until_shortly_before_it_expires() -> None:
    transport, clock = ScriptedTransport(), ManualClock()
    tokens = _tokens(transport, clock)
    assert await tokens.token() == "token-1"
    clock.now += 3600 - TOKEN_MARGIN_S - 1
    assert await tokens.token() == "token-1"
    assert tokens.requests == 1
    clock.now += 2
    assert await tokens.token() == "token-2"
    tokens.invalidate()
    assert await tokens.token() == "token-3"
    assert tokens.requests == 3


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_a_refused_token_request_is_a_refused_login(status: int) -> None:
    transport = ScriptedTransport(token_status=status)
    with pytest.raises(CloudLoginRefused) as caught:
        await _tokens(transport, ManualClock()).token()
    assert APP_KEY not in str(caught.value) and SECRET not in str(caught.value)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_rate_limits_and_server_errors_are_unavailable(status: int) -> None:
    with pytest.raises(CloudUnavailable) as caught:
        await _tokens(ScriptedTransport(token_status=status), ManualClock()).token()
    assert SECRET not in str(caught.value)


async def test_network_failures_and_odd_answers_are_unavailable() -> None:
    transport = ScriptedTransport()
    transport.fail_network = True
    with pytest.raises(CloudUnavailable, match="did not answer"):
        await _tokens(transport, ManualClock()).token()

    class NoToken(ScriptedTransport):
        async def post_form(self, url: str, form: Mapping[str, str]) -> HttpReply:
            return HttpReply(200, {"token_type": "Bearer"})

    with pytest.raises(CloudUnavailable, match="HTTP 200"):
        await _tokens(NoToken(), ManualClock()).token()


def test_reprs_hide_the_credentials() -> None:
    tokens = _tokens(ScriptedTransport(), ManualClock())
    assert SECRET not in repr(tokens) and APP_KEY not in repr(tokens)


async def test_throttle_spaces_requests_one_second_apart_except_urgent_ones() -> None:
    clock = ManualClock()
    throttle = RequestThrottle(1.0, clock=clock, sleep=clock.sleep)
    await throttle.wait()
    await throttle.wait()
    clock.now += 0.25
    await throttle.wait()
    assert clock.sleeps == [1.0, 0.75]
    with urgent():
        await throttle.wait()
    assert clock.sleeps == [1.0, 0.75]
    assert throttle.requests == 4


def _auth(transport: ScriptedTransport, clock: ManualClock) -> ClientCredentialsAuth:
    return ClientCredentialsAuth(
        transport,
        _tokens(transport, clock),
        throttle=RequestThrottle(1.0, clock=clock, sleep=clock.sleep),
    )


async def test_requests_carry_bearer_token_and_application_key() -> None:
    transport = ScriptedTransport()
    auth = _auth(transport, ManualClock())
    body = await auth.get_json("mowers/")
    assert len(body["data"]) == 4
    method, path, headers, _ = transport.calls[0]
    assert (method, path) == ("get", "mowers")
    assert headers["Authorization"] == "Bearer token-1"
    assert headers["X-Api-Key"] == APP_KEY
    assert headers["Authorization-Provider"] == "husqvarna"
    assert SECRET not in repr(auth)


async def test_a_401_renews_the_token_once() -> None:
    transport = ScriptedTransport(status={("get", "mowers"): 401})
    auth = _auth(transport, ManualClock())
    with pytest.raises(ApiUnauthorizedError):
        await auth.get_json("mowers/")
    assert len(transport.calls) == 2
    assert [c[2]["Authorization"] for c in transport.calls] == ["Bearer token-1", "Bearer token-2"]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, ApiBadRequestError),
        (403, ApiForbiddenError),
        (429, RateLimitedError),
        (500, ApiError),
    ],
)
async def test_vendor_statuses_become_aioautomower_errors(
    status: int, error: type[Exception]
) -> None:
    path = "mowers/x/actions"
    transport = ScriptedTransport(status={("post", path): status})
    with pytest.raises(error):
        await _auth(transport, ManualClock()).post_json(path, json={"data": {"type": "Pause"}})


async def test_network_failures_of_requests_are_api_errors() -> None:
    transport = ScriptedTransport()
    auth = _auth(transport, ManualClock())
    await auth.get_json("mowers/")  # token first
    transport.fail_network = True
    with pytest.raises(ApiError, match="ClientConnectorError"):
        await auth.get_json("mowers/")


async def test_raw_aiohttp_requests_are_never_made() -> None:
    auth = _auth(ScriptedTransport(), ManualClock())
    with pytest.raises(NotImplementedError):
        await auth.request("get", "mowers/")
