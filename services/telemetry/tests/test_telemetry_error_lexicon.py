"""Error codes in plain words (ADR 0067): telemetry asks the Device-Gateway over the bus, caches
the answers and falls back to generic advice when nobody answers.

A small responder on the in-memory bus stands in for the gateway: it answers from the
simulator's lexicon through ``mowdeck-control``'s registry, the same code the gateway runs."""

from datetime import UTC, datetime

from mowdeck_contracts.v1 import (
    GATEWAY_ERRORS_EXPLAIN,
    GATEWAY_ERRORS_LEXICONS,
    ErrorExplainReply,
    ErrorExplainRequest,
    ErrorLexiconListReply,
    MowerActivity,
    MowerErrorCategory,
)
from mowdeck_control import ErrorLexiconRegistry
from mowdeck_control.simulator_lexicon import SIMULATED_LEXICON
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from telemetry_support import API, Harness, harness, make_state

from telemetry.application.errors import ErrorExplanations


async def gateway(bus: InMemoryBus) -> list[ErrorExplainRequest]:
    """Answer lookups like the Device-Gateway; returns the requests it saw."""
    registry = ErrorLexiconRegistry(lambda: [])
    registry.register(SIMULATED_LEXICON)
    seen: list[ErrorExplainRequest] = []

    async def explain(message: BusMessage) -> None:
        request = message.model(ErrorExplainRequest)
        seen.append(request)
        explanations = registry.explain(
            request.codes,
            request.locale,
            vendor=request.vendor,
            connection=request.connection,
            category=request.category,
        )
        await message.respond(ErrorExplainReply(explanations=explanations))

    async def lexicons(message: BusMessage) -> None:
        await message.respond(ErrorLexiconListReply(lexicons=registry.infos()))

    await bus.subscribe(GATEWAY_ERRORS_EXPLAIN, explain, queue="device-gateway")
    await bus.subscribe(GATEWAY_ERRORS_LEXICONS, lexicons, queue="device-gateway")
    return seen


async def _with_gateway(h: Harness) -> list[ErrorExplainRequest]:
    return await gateway(h.bus)


async def test_telemetry_error_lexicon_explains_the_current_error_in_german() -> None:
    async with harness() as h:
        seen = await _with_gateway(h)
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        url = f"{API}/mowers/{mower_id}/error"
        before = await h.client.get(url, headers=owner.headers)
        await h.publish_state(
            make_state(
                mower_id, owner.account_id, at=datetime.now(UTC), activity=MowerActivity.MOWING
            )
        )
        none = (await h.client.get(url, headers=owner.headers)).json()
        await h.publish_state(
            make_state(
                mower_id,
                owner.account_id,
                at=datetime.now(UTC),
                activity=MowerActivity.ERROR,
                error_code="stuck",
                error_category=MowerErrorCategory.TRAPPED,
            )
        )
        german = await h.client.get(url, params={"locale": "de"}, headers=owner.headers)
        again = await h.client.get(
            url, headers={**owner.headers, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}
        )
        foreign = await h.client.get(url, headers=(await h.owner()).headers)
    assert before.status_code == 404
    assert none["error"] is None and none["activity"] == "mowing"
    body = german.json()
    assert german.headers["cache-control"] == "private, max-age=15"
    assert body["error"]["title"] == "Steckt an einem Hindernis fest"
    assert body["error"]["vendor_name"] == "Simulator" and body["error"]["known"] is True
    assert body["error"]["category"] == "trapped" and len(body["error"]["steps"]) == 2
    assert again.json()["error"] == body["error"]
    assert len(seen) == 1  # the second answer came from the cache
    assert seen[0].connection == "simulated" and seen[0].locale == "de"
    assert foreign.status_code == 404


async def test_telemetry_error_lexicon_vendor_codes_and_list() -> None:
    async with harness() as h:
        await _with_gateway(h)
        owner = await h.owner()
        lifted = await h.client.get(
            f"{API}/error-codes/simulated/lifted", params={"locale": "en"}, headers=owner.headers
        )
        unknown = await h.client.get(
            f"{API}/error-codes/simulated/E42", params={"locale": "de"}, headers=owner.headers
        )
        bad = await h.client.get(f"{API}/error-codes/Not-A-Vendor/1", headers=owner.headers)
        listed = await h.client.get(f"{API}/error-lexicons", headers=owner.headers)
        anonymous = await h.client.get(f"{API}/error-lexicons")
    assert lifted.json()["title"] == "Lifted" and lifted.json()["severity"] == "critical"
    assert lifted.headers["cache-control"] == "private, max-age=3600"
    assert unknown.json()["known"] is False
    assert unknown.json()["title"] == "Unbekannter Fehler E42 von Simulator"
    assert bad.status_code == 422
    assert [x["vendor"] for x in listed.json()["lexicons"]] == ["simulated"]
    assert anonymous.status_code == 401


async def test_telemetry_error_lexicon_explains_codes_of_a_mower() -> None:
    async with harness() as h:
        await _with_gateway(h)
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(
            make_state(
                mower_id, owner.account_id, at=datetime.now(UTC), activity=MowerActivity.DOCKED
            )
        )
        reply = await h.client.get(
            f"{API}/mowers/{mower_id}/error-codes",
            params=[("code", "stuck"), ("code", "lifted"), ("code", "nope"), ("locale", "de")],
            headers=owner.headers,
        )
        empty = await h.client.get(f"{API}/mowers/{mower_id}/error-codes", headers=owner.headers)
    titles = [e["title"] for e in reply.json()["explanations"]]
    assert titles == [
        "Steckt an einem Hindernis fest",
        "Angehoben",
        "Unbekannter Fehler nope von Simulator",
    ]
    assert empty.status_code == 422


async def test_telemetry_error_lexicon_without_gateway_falls_back() -> None:
    async with harness() as h:  # nobody answers on the bus
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(
            make_state(
                mower_id,
                owner.account_id,
                at=datetime.now(UTC),
                activity=MowerActivity.ERROR,
                error_code="lifted",
                error_category=MowerErrorCategory.LIFT_TILT,
            )
        )
        body = (
            await h.client.get(
                f"{API}/mowers/{mower_id}/error", params={"locale": "de"}, headers=owner.headers
            )
        ).json()
        listed = (await h.client.get(f"{API}/error-lexicons", headers=owner.headers)).json()
    assert body["error"]["known"] is False
    assert body["error"]["category"] == "lift_tilt" and body["error"]["severity"] == "critical"
    assert body["error"]["description"] == "Der Mäher wurde angehoben oder gekippt"
    assert listed == {"lexicons": []}


async def test_telemetry_error_lexicon_widget_carries_the_title() -> None:
    """The widgets read one compact state (ADR 0059) and store it for offline use: it carries
    the error's title, category and severity in the requested locale."""
    async with harness() as h:
        await _with_gateway(h)
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        url = f"{API}/mowers/{mower_id}/widget"

        async def publish(activity: MowerActivity, code: str | None, **kw: object) -> None:
            await h.publish_state(
                make_state(
                    mower_id,
                    owner.account_id,
                    at=datetime.now(UTC),
                    activity=activity,
                    error_code=code,
                    **kw,  # type: ignore[arg-type]
                )
            )

        await publish(MowerActivity.MOWING, None)
        mowing = (await h.client.get(url, params={"locale": "de"}, headers=owner.headers)).json()
        await publish(
            MowerActivity.ERROR,
            "lifted",
            error_message="the mower was lifted",
            error_category=MowerErrorCategory.LIFT_TILT,
        )
        german = await h.client.get(url, params={"locale": "de"}, headers=owner.headers)
        english = await h.client.get(url, headers={**owner.headers, "Accept-Language": "en-GB"})
        await publish(MowerActivity.ERROR, "E42", error_category=MowerErrorCategory.TRAPPED)
        generic = (await h.client.get(url, params={"locale": "de"}, headers=owner.headers)).json()
        await publish(MowerActivity.ERROR, "E43")
        unknown = (await h.client.get(url, params={"locale": "de"}, headers=owner.headers)).json()
    assert (mowing["error_title"], mowing["error_category"], mowing["error_severity"]) == (
        None,
        None,
        None,
    )
    body = german.json()
    assert body["error_code"] == "lifted" and body["error_message"] == "the mower was lifted"
    assert body["error_title"] == "Angehoben"
    assert (body["error_category"], body["error_severity"]) == ("lift_tilt", "critical")
    assert german.headers["vary"] == "Accept-Language"
    assert english.json()["error_title"] == "Lifted"
    # a code no lexicon knows: the generic words of its category, else none (vendor message)
    assert generic["error_title"] == "Der Mäher steckt fest"
    assert generic["error_category"] == "trapped"
    assert unknown["error_title"] is None and unknown["error_category"] == "other"


async def test_telemetry_error_lexicon_widget_without_gateway_uses_the_category() -> None:
    async with harness() as h:  # nobody answers on the bus
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(
            make_state(
                mower_id,
                owner.account_id,
                at=datetime.now(UTC),
                activity=MowerActivity.ERROR,
                error_code="lifted",
                error_category=MowerErrorCategory.LIFT_TILT,
            )
        )
        body = (
            await h.client.get(
                f"{API}/mowers/{mower_id}/widget", params={"locale": "en"}, headers=owner.headers
            )
        ).json()
    assert body["error_title"] == "The mower was lifted or tilted"
    assert (body["error_category"], body["error_severity"]) == ("lift_tilt", "critical")


class _CountingGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def explain(self, request: ErrorExplainRequest) -> ErrorExplainReply | None:
        self.calls += 1
        registry = ErrorLexiconRegistry(lambda: [])
        registry.register(SIMULATED_LEXICON)
        return ErrorExplainReply(
            explanations=registry.explain(request.codes, request.locale, vendor=request.vendor)
        )

    async def lexicons(self) -> None:
        return None


async def test_telemetry_error_lexicon_cache_expires_and_is_bounded() -> None:
    now = [0.0]
    fake = _CountingGateway()
    explanations = ErrorExplanations(
        fake,
        queries=None,  # type: ignore[arg-type]
        cache_seconds=10,
        cache_size=1,
        clock=lambda: now[0],
    )
    await explanations.explain(["stuck"], "de", vendor="simulated")
    await explanations.explain(["stuck"], "de", vendor="simulated")
    assert fake.calls == 1
    now[0] = 11.0
    await explanations.explain(["stuck"], "de", vendor="simulated")
    assert fake.calls == 2
    await explanations.explain(["lifted"], "de", vendor="simulated")  # evicts "stuck"
    await explanations.explain(["stuck"], "de", vendor="simulated")
    assert fake.calls == 4
    assert await explanations.lexicons() == []
