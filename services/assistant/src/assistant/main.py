"""Entry point of the assistant: wires the layers into one FastAPI app.

``python -m assistant.main`` (the container command) loads the settings from the environment,
builds the app and serves it. Tests call :func:`build_app` with their own settings, platform,
interpreter, clock and principal resolver.

Principals: with ``MOWDECK_AUTH_JWKS_URL`` set, bearer access tokens issued by Identity are
verified locally against its JWKS (``mowdeck-auth``); without it every request is 401. The
verified token is then forwarded unchanged to the public API (ADR 0075).
"""

from collections.abc import Mapping

import httpx
from fastapi import FastAPI
from mowdeck_auth import principal_resolver_from_env
from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import PrincipalResolver, create_app, get_logger, run

from assistant.adapters.api.deps import install_error_mapping
from assistant.adapters.api.routes import router
from assistant.adapters.interpreters import PhraseInterpreter, ProviderInterpreter
from assistant.adapters.upstream import HttpPlatform
from assistant.application.ports import Clock, Interpreter, Platform
from assistant.application.proposals import AssistantService, RateLimiter
from assistant.module import DESCRIPTOR
from assistant.settings import Settings

_log = get_logger("assistant.main")


def build_app(
    settings: Settings,
    *,
    platform: Platform | None = None,
    interpreter: Interpreter | None = None,
    clock: Clock | None = None,
    principal_resolver: PrincipalResolver | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the app. Without overrides it calls the public API over HTTP."""
    client = httpx.AsyncClient(
        timeout=settings.assistant_upstream_timeout_seconds,
        transport=transport,
        follow_redirects=False,
    )

    async def startup(app: FastAPI) -> None:
        chosen: Interpreter = interpreter or (
            PhraseInterpreter()
            if settings.interpreter == "phrases"
            else ProviderInterpreter(
                client,
                vision_url=settings.assistant_vision_url,
                timeout=settings.assistant_ai_timeout_seconds,
            )
        )
        app.state.assistant_service = AssistantService(
            platform=platform
            or HttpPlatform(
                client,
                identity_url=settings.assistant_identity_url,
                fleet_url=settings.assistant_fleet_url,
                telemetry_url=settings.assistant_telemetry_url,
            ),
            interpreter=chosen,
            clock=clock or utc_now,
            limiter=RateLimiter(settings.assistant_requests_per_minute),
        )
        _log.info("assistant ready", interpreter=chosen.kind, demo=settings.demo_mode)

    async def shutdown(app: FastAPI) -> None:
        await client.aclose()

    app = create_app(
        settings,
        DESCRIPTOR,
        routers=[router],
        on_startup=[startup],
        on_shutdown=[shutdown],
        principal_resolver=principal_resolver,
    )
    install_error_mapping(app)
    return app


def default_principal_resolver(
    environ: Mapping[str, str] | None = None,
) -> PrincipalResolver | None:
    """The JWT resolver when ``MOWDECK_AUTH_JWKS_URL`` is set, else None (the kit denies all)."""
    resolver = principal_resolver_from_env(environ)
    if resolver is None:
        _log.warning("MOWDECK_AUTH_JWKS_URL is not set: every API request is answered with 401")
    return resolver


def main() -> None:
    """Load the settings from the environment and serve the app."""
    settings = Settings.from_env()
    run(build_app(settings, principal_resolver=default_principal_resolver()), settings)


if __name__ == "__main__":
    main()
