"""Push texts explain the error code with the vendor's lexicon (ADR 0067).

A responder on the in-memory bus stands in for the Device-Gateway (``gateway.errors.explain``)
and answers from a small lexicon; without it the text keeps the vendor's message."""

from typing import Any

from mowdeck_contracts.v1 import (
    GATEWAY_ERRORS_EXPLAIN,
    ConnectionKind,
    ErrorExplainReply,
    ErrorExplainRequest,
    ErrorLexicon,
    ErrorLexiconEntry,
    ErrorText,
    MowerActivity,
    MowerErrorCategory,
    explain_code,
    lexicon_for_connection,
)
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from notifications_support import Harness, Person, harness, make_state

from notifications.adapters.bus.errors import BusErrorExplainer

LEXICON = ErrorLexicon(
    vendor="simulated",
    vendor_name="Simulator",
    connection_kinds=[ConnectionKind.SIMULATED],
    entries=[
        ErrorLexiconEntry(
            code="stuck",
            category=MowerErrorCategory.TRAPPED,
            texts={
                "en": ErrorText(title="Stuck on an obstacle", steps=["Drive it free."]),
                "de": ErrorText(
                    title="Steckt an einem Hindernis fest", steps=["Fahr ihn frei.", "Weiter."]
                ),
            },
        )
    ],
)


async def serve_lexicon(bus: InMemoryBus) -> list[ErrorExplainRequest]:
    """Answer like the Device-Gateway from :data:`LEXICON`; returns the requests it saw."""
    seen: list[ErrorExplainRequest] = []

    async def explain(message: BusMessage) -> None:
        request = message.model(ErrorExplainRequest)
        seen.append(request)
        lexicon = lexicon_for_connection([LEXICON], request.connection)
        explanations = [explain_code(lexicon, code, request.locale) for code in request.codes]
        await message.respond(ErrorExplainReply(explanations=explanations))

    await bus.subscribe(GATEWAY_ERRORS_EXPLAIN, explain, queue="device-gateway")
    return seen


async def get_stuck(h: Harness, owner: Person, mower_id: Any, code: str, **state: object) -> None:
    h.clock.advance(10)
    mowing = make_state(mower_id, owner.account_id, at=h.clock.now, activity=MowerActivity.MOWING)
    await h.publish_state(mowing.model_copy(update=state))
    h.clock.advance(30)
    error = make_state(
        mower_id,
        owner.account_id,
        at=h.clock.now,
        activity=MowerActivity.ERROR,
        error_code=code,
        error_message="the mower is stuck on an obstacle",
    )
    await h.publish_state(error.model_copy(update=state))


async def test_notifications_error_lexicon_explains_the_push_text() -> None:
    async with harness() as h:
        seen = await serve_lexicon(h.bus)
        owner = await h.owner()
        await h.subscribe_all(owner, language="de")
        mower_id = await h.add_mower(owner)
        await get_stuck(h, owner, mower_id, "stuck")
        items = await h.inbox(owner)
    stuck = next(i for i in items if i["kind"] == "mower_stuck")
    assert stuck["body"] == ("Fehler stuck. Steckt an einem Hindernis fest. Fahr ihn frei.")
    # the account-wide notifications.raised in English, the user's push in German
    assert {(r.connection, r.locale) for r in seen} == {("simulated", "en"), ("simulated", "de")}


async def test_notifications_error_lexicon_keeps_the_vendor_message_for_unknown_codes() -> None:
    async with harness() as h:
        await serve_lexicon(h.bus)
        owner = await h.owner()
        await h.subscribe_all(owner, language="en")
        mower_id = await h.add_mower(owner)
        await get_stuck(h, owner, mower_id, "E42")
        items = await h.inbox(owner)
    stuck = next(i for i in items if i["kind"] == "mower_stuck")
    assert stuck["body"] == "Error E42. the mower is stuck on an obstacle"


async def test_notifications_error_lexicon_explainer_caches_and_survives_no_gateway() -> None:
    bus = InMemoryBus()
    explainer = BusErrorExplainer(bus, timeout_seconds=0.2)
    assert await explainer.explain("stuck", "simulated", "en") is None  # nobody answers
    seen = await serve_lexicon(bus)
    first = await explainer.explain("stuck", "simulated", "en", "trapped")
    second = await explainer.explain("stuck", "simulated", "en")
    assert first is not None and first.title == "Stuck on an obstacle"
    assert second == first and len(seen) == 1
    assert await explainer.explain("stuck", "not-a-kind", "en") is None


def _cloud_lexicon(vendor: str, title: str) -> ErrorLexicon:
    """A lexicon on the shared ``cloud`` kind whose code ``7`` reads ``title``."""
    return ErrorLexicon(
        vendor=vendor,
        vendor_name=vendor.title(),
        connection_kinds=[ConnectionKind.CLOUD],
        entries=[ErrorLexiconEntry(code="7", texts={"en": ErrorText(title=title)})],
    )


CLOUD_LEXICONS = {
    "alpha": _cloud_lexicon("alpha", "Alpha jammed"),
    "bravo": _cloud_lexicon("bravo", "Bravo jammed"),
}


async def serve_cloud_lexicons(bus: InMemoryBus) -> list[ErrorExplainRequest]:
    """Answer like the Device-Gateway: by the mower's provider, else by the connection kind."""
    seen: list[ErrorExplainRequest] = []

    async def explain(message: BusMessage) -> None:
        request = message.model(ErrorExplainRequest)
        seen.append(request)
        lexicon = (
            CLOUD_LEXICONS.get(request.cloud_provider)
            if request.cloud_provider
            else lexicon_for_connection(CLOUD_LEXICONS.values(), request.connection)
        )
        explanations = [explain_code(lexicon, code, request.locale) for code in request.codes]
        await message.respond(ErrorExplainReply(explanations=explanations))

    await bus.subscribe(GATEWAY_ERRORS_EXPLAIN, explain, queue="device-gateway")
    return seen


async def test_notifications_error_lexicon_push_uses_the_mowers_provider() -> None:
    """Two providers share the kind ``cloud`` (ADR 0066): each mower's push carries the words
    of its own vendor's lexicon."""
    async with harness() as h:
        seen = await serve_cloud_lexicons(h.bus)
        owner = await h.owner()
        await h.subscribe_all(owner, language="en")
        alpha, bravo = await h.add_mower(owner), await h.add_mower(owner)
        cloud = {"connection": ConnectionKind.CLOUD}
        await get_stuck(h, owner, alpha, "7", **cloud, cloud_provider="alpha")
        await get_stuck(h, owner, bravo, "7", **cloud, cloud_provider="bravo")
        items = await h.inbox(owner)
    bodies = {i["mower_id"]: i["body"] for i in items if i["kind"] == "mower_stuck"}
    assert bodies == {str(alpha): "Error 7. Alpha jammed", str(bravo): "Error 7. Bravo jammed"}
    assert {r.cloud_provider for r in seen} == {"alpha", "bravo"}


async def test_notifications_error_lexicon_explainer_caches_per_provider() -> None:
    bus = InMemoryBus()
    seen = await serve_cloud_lexicons(bus)
    explainer = BusErrorExplainer(bus, timeout_seconds=0.2)
    alpha = await explainer.explain("7", "cloud", "en", cloud_provider="alpha")
    bravo = await explainer.explain("7", "cloud", "en", cloud_provider="bravo")
    again = await explainer.explain("7", "cloud", "en", cloud_provider="bravo")
    legacy = await explainer.explain("7", "cloud", "en")
    assert alpha is not None and alpha.title == "Alpha jammed"
    assert bravo is not None and bravo.title == "Bravo jammed" and again == bravo
    assert legacy is not None and legacy.vendor == "alpha"  # no provider: by connection kind
    assert [r.cloud_provider for r in seen] == ["alpha", "bravo", None]
