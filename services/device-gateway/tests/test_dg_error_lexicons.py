"""Error lexicon lookups over the bus (ADR 0067): the gateway answers from the lexicons the
installed adapter packages register (simulator and Mammotion in this image)."""

from mowdeck_contracts.v1 import (
    GATEWAY_ERRORS_EXPLAIN,
    GATEWAY_ERRORS_LEXICONS,
    ConnectionKind,
    ErrorExplainReply,
    ErrorExplainRequest,
    ErrorLexicon,
    ErrorLexiconEntry,
    ErrorLexiconListReply,
    ErrorLexiconListRequest,
    ErrorText,
    MowerErrorCategory,
)
from mowdeck_control import ErrorLexiconRegistry
from mowdeck_service_kit.bus import InMemoryBus

from device_gateway.adapters.bus.errors import subscribe_error_lexicons


async def _explain(bus: InMemoryBus, **values: object) -> ErrorExplainReply:
    reply = await bus.request(GATEWAY_ERRORS_EXPLAIN, ErrorExplainRequest.model_validate(values))
    return reply.model(ErrorExplainReply)


async def test_dg_error_lexicons_explain_with_the_installed_lexicons() -> None:
    bus = InMemoryBus()
    subscriptions = await subscribe_error_lexicons(bus)
    reply = await _explain(bus, codes=["-1201", "stuck"], connection="cloud", locale="de")
    first, second = reply.explanations
    assert (first.vendor, first.code, first.title, first.known) == (
        "mammotion",
        "1201",
        "Steckt fest",
        True,
    )
    assert second.known is False and second.title == "Unbekannter Fehler stuck von Mammotion"
    simulated = await _explain(bus, codes=["lifted"], connection=ConnectionKind.SIMULATED)
    assert simulated.explanations[0].category is MowerErrorCategory.LIFT_TILT
    listed = (await bus.request(GATEWAY_ERRORS_LEXICONS, ErrorLexiconListRequest())).model(
        ErrorLexiconListReply
    )
    assert [info.vendor for info in listed.lexicons] == ["mammotion", "simulated"]
    for subscription in subscriptions:
        await subscription.unsubscribe()


async def test_dg_error_lexicons_unknown_vendor_gets_the_generic_advice() -> None:
    registry = ErrorLexiconRegistry(lambda: [])
    registry.register(
        ErrorLexicon(
            vendor="acme",
            vendor_name="Acme",
            entries=[ErrorLexiconEntry(code="1", texts={"en": ErrorText(title="One")})],
        )
    )
    bus = InMemoryBus()
    await subscribe_error_lexicons(bus, registry)
    reply = await _explain(
        bus, codes=["1"], vendor="nobody", category=MowerErrorCategory.RAIN, locale="en"
    )
    [explanation] = reply.explanations
    assert explanation.known is False and explanation.vendor is None
    assert explanation.description == "Rain detected" and explanation.user_action is False
    assert (await _explain(bus, codes=["1"], vendor="acme")).explanations[0].title == "One"
