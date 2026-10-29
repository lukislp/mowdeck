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


async def get_stuck(h: Harness, owner: Person, mower_id: Any, code: str) -> None:
    h.clock.advance(10)
    await h.publish_state(
        make_state(mower_id, owner.account_id, at=h.clock.now, activity=MowerActivity.MOWING)
    )
    h.clock.advance(30)
    await h.publish_state(
        make_state(
            mower_id,
            owner.account_id,
            at=h.clock.now,
            activity=MowerActivity.ERROR,
            error_code=code,
            error_message="the mower is stuck on an obstacle",
        )
    )


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
