"""Startup and shutdown of the assistant and its settings."""

from assistant_support import DEMO, settings
from mowdeck_service_kit.testing import lifespan_client

from assistant.adapters.interpreters import PhraseInterpreter, ProviderInterpreter
from assistant.main import build_app, default_principal_resolver


def test_assistant_settings_defaults_need_no_bus() -> None:
    defaults = settings()
    assert defaults.service_name == "assistant"
    assert defaults.nats_enabled is False
    assert defaults.interpreter == "provider"
    assert settings(assistant_interpreter="phrases").interpreter == "phrases"
    # Demo mode never calls an external AI (ADR 0068).
    assert settings(**DEMO).interpreter == "phrases"


async def test_assistant_starts_without_nats_and_picks_its_interpreter() -> None:
    app = build_app(settings())
    async with lifespan_client(app) as client:
        assert (await client.get("/readyz")).status_code == 200
        assert app.state.bus is None
        assert isinstance(app.state.assistant_service.interpreter, ProviderInterpreter)
    assert app.state.draining is True
    phrases = build_app(settings(assistant_interpreter="phrases"))
    async with lifespan_client(phrases):
        assert isinstance(phrases.state.assistant_service.interpreter, PhraseInterpreter)


def test_assistant_principal_resolver_needs_the_jwks_url() -> None:
    assert default_principal_resolver({}) is None
    assert default_principal_resolver({"MOWDECK_AUTH_JWKS_URL": "http://identity/jwks"})
