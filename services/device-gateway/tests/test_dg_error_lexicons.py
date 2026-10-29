"""Error lexicon lookups over the bus (ADR 0067): the gateway answers from the lexicons the
installed adapter packages register (simulator, Mammotion and Husqvarna in this image)."""

from mowdeck_contracts.v1 import (
    GATEWAY_ERRORS_ENTRIES,
    GATEWAY_ERRORS_EXPLAIN,
    GATEWAY_ERRORS_LEXICONS,
    ConnectionKind,
    ErrorExplainReply,
    ErrorExplainRequest,
    ErrorLexicon,
    ErrorLexiconEntry,
    ErrorLexiconListReply,
    ErrorLexiconListRequest,
    ErrorLexiconSearchReply,
    ErrorLexiconSearchRequest,
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
    assert [info.vendor for info in listed.lexicons] == ["husqvarna", "mammotion", "simulated"]
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


def _cloud_lexicon(vendor: str, title: str) -> ErrorLexicon:
    return ErrorLexicon(
        vendor=vendor,
        vendor_name=vendor.title(),
        connection_kinds=[ConnectionKind.CLOUD],
        entries=[ErrorLexiconEntry(code="7", texts={"en": ErrorText(title=title)})],
    )


async def test_dg_error_lexicons_each_cloud_provider_gets_its_own_lexicon() -> None:
    # Two providers share the kind `cloud` (ADR 0066); "alpha" sorts first, "zulu" claims the
    # cloud registrations that name no provider (the mowers from before ADR 0066).
    registry = ErrorLexiconRegistry(lambda: [])
    registry.register(_cloud_lexicon("alpha", "Alpha jammed"))
    registry.register(_cloud_lexicon("zulu", "Zulu jammed"))
    bus = InMemoryBus()
    await subscribe_error_lexicons(bus, registry, unqualified_cloud="zulu")

    async def title(**values: object) -> tuple[str | None, str]:
        [explanation] = (await _explain(bus, codes=["7"], **values)).explanations
        return explanation.vendor, explanation.title

    assert await title(connection="cloud", cloud_provider="alpha") == ("alpha", "Alpha jammed")
    assert await title(connection="cloud", cloud_provider="zulu") == ("zulu", "Zulu jammed")
    # a stored state without the provider: the provider that claims unqualified cloud mowers
    assert await title(connection="cloud") == ("zulu", "Zulu jammed")
    assert await title(connection="cloud", cloud_provider="nobody") == (
        None,
        "Unknown error 7 from the mower",
    )

    async def lexicon(**values: object) -> str | None:
        request = ErrorLexiconSearchRequest.model_validate(values)
        reply = (await bus.request(GATEWAY_ERRORS_ENTRIES, request)).model(ErrorLexiconSearchReply)
        return reply.lexicon.vendor if reply.lexicon is not None else None

    assert await lexicon(connection="cloud", cloud_provider="alpha") == "alpha"
    assert await lexicon(connection="cloud") == "zulu"
    # without a claiming provider the connection kind decides, as before
    plain = InMemoryBus()
    await subscribe_error_lexicons(plain, registry)
    reply = await _explain(plain, codes=["7"], connection="cloud")
    assert reply.explanations[0].vendor == "alpha"


async def test_dg_error_lexicons_search_a_page_of_the_mowers_lexicon() -> None:
    bus = InMemoryBus()
    await subscribe_error_lexicons(bus)

    async def search(**values: object) -> ErrorLexiconSearchReply:
        request = ErrorLexiconSearchRequest.model_validate(values)
        return (await bus.request(GATEWAY_ERRORS_ENTRIES, request)).model(ErrorLexiconSearchReply)

    mammotion = await search(connection="cloud", locale="de", codes=["-1201", "-2801"])
    assert mammotion.lexicon is not None and mammotion.lexicon.vendor == "mammotion"
    assert [e.code for e in mammotion.entries] == ["1201", "2801"]  # the mower's spelling works
    assert mammotion.entries[0].title == "Steckt fest"
    assert sum(mammotion.categories.values()) == mammotion.lexicon.entries
    lifts = await search(connection="cloud", category="lift_tilt", limit=100)
    assert {"1004", "1030", "2502", "2801", "11131", "11133"} <= {e.code for e in lifts.entries}
    simulated = await search(connection="simulated", query="lifted")
    assert [e.code for e in simulated.entries] == ["lifted"]
    nobody = await search(vendor="nobody")
    assert nobody.lexicon is None and nobody.entries == [] and nobody.total == 0
