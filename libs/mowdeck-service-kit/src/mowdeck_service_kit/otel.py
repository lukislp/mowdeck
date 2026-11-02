"""Optional OpenTelemetry tracing (install the ``otel`` extra).

Tracing is enabled when ``MOWDECK_OTEL_EXPORTER_OTLP_ENDPOINT`` is set. Spans are exported via
OTLP/HTTP in batches; FastAPI requests are instrumented automatically.
"""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI

from mowdeck_service_kit.logging import get_logger
from mowdeck_service_kit.settings import ServiceSettings

_log = get_logger(__name__)

ShutdownFunction = Callable[[], Awaitable[None]]


def traces_endpoint(base: str) -> str:
    """Return the OTLP/HTTP traces URL for a collector base URL."""
    stripped = base.rstrip("/")
    return stripped if stripped.endswith("/v1/traces") else f"{stripped}/v1/traces"


def instrument_app(app: FastAPI, settings: ServiceSettings) -> ShutdownFunction | None:
    """Instrument ``app`` if an OTLP endpoint is configured.

    Returns an async function that flushes and shuts the tracer provider down, or None when
    tracing is disabled or the ``otel`` extra is not installed (a warning is logged then).
    """
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:  # pragma: no cover - depends on installed extras
        _log.warning("tracing requested but the 'otel' extra is not installed")
        return None

    resource = Resource.create(
        {"service.name": settings.service_name, "deployment.environment": settings.environment}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=traces_endpoint(endpoint)))
    )
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls="healthz,startupz,readyz,metrics",
    )
    _log.info("tracing enabled", endpoint=traces_endpoint(endpoint))

    async def shutdown() -> None:
        provider.shutdown()

    return shutdown
