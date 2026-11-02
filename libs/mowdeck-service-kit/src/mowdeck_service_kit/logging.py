"""Structured logging with structlog.

All logs, including those of the standard library (uvicorn, nats), go to stdout through one
handler: JSON lines in production, a coloured console renderer in development. Every line
carries ``service``, ``level``, ``timestamp`` (UTC, ISO 8601) and ``correlation_id`` when one
is set. Standard library records keep their ``extra={...}`` fields. Secret values (passwords,
tokens, pre-shared keys, ...) are masked by ``redact_secrets`` before rendering.
"""

import logging
import sys
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from mowdeck_service_kit.correlation import get_correlation_id
from mowdeck_service_kit.settings import ServiceSettings

REDACTED = "***"

_SECRET_KEYS = frozenset(
    {
        "password",
        "psk",
        "noise_psk",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "private_key",
        "client_secret",
        "refresh_token",
        "access_token",
    }
)
_SECRET_SUFFIXES = ("_password", "_secret", "_token")
_KEY_SUFFIX_HINTS = ("secret", "api", "private", "encryption")


def _is_secret_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    name = key.lower()
    if name in _SECRET_KEYS or name.endswith(_SECRET_SUFFIXES):
        return True
    return name.endswith("_key") and any(hint in name for hint in _KEY_SUFFIX_HINTS)


def _redact(value: Any) -> Any:
    """Return ``value`` with secrets masked; the very same object when nothing changed."""
    if isinstance(value, dict):
        changed: dict[Any, Any] | None = None
        for key, item in value.items():
            new = (REDACTED if item is not None else None) if _is_secret_key(key) else _redact(item)
            if new is not item:
                if changed is None:
                    changed = dict(value)
                changed[key] = new
        return value if changed is None else changed
    if isinstance(value, list | tuple):
        items = [_redact(item) for item in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return items if isinstance(value, list) else tuple(items)
    return value


def redact_secrets(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    """Structlog processor masking secret values with ``"***"``.

    Keys are matched case-insensitively at any depth of nested dicts, lists and tuples
    (so also inside ``connection_options``): ``password``, ``psk``, ``noise_psk``, ``secret``,
    ``token``, ``api_key``, ``apikey``, ``authorization``, ``credential(s)``, ``private_key``,
    ``client_secret``, ``refresh_token``, ``access_token``, any ``*_password``, ``*_secret``,
    ``*_token`` and ``*_key`` names containing ``secret``, ``api``, ``private`` or
    ``encryption`` (ids such as ``product_key`` stay readable). ``None`` stays ``None``.

    Containers are copied on write, so caller-owned objects are never mutated and events
    without secrets cost one traversal and no allocation. Only plain dicts, lists and tuples
    are inspected: pydantic models, dataclasses and other objects are left untouched, so
    callers must never log raw registrations or credential models; log selected fields.
    """
    result: EventDict = _redact(event_dict)
    return result


def _add_correlation_id(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    correlation_id = get_correlation_id()
    if correlation_id is not None:
        event_dict.setdefault("correlation_id", correlation_id)
    return event_dict


def _add_service(service: str) -> Processor:
    def processor(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", service)
        return event_dict

    return processor


_DROPPED_EXTRAS = frozenset({"color_message"})
"""``extra`` fields of standard library records that only repeat the message (uvicorn's)."""


def _drop_noise_extras(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    for key in _DROPPED_EXTRAS:
        event_dict.pop(key, None)
    return event_dict


def configure_logging(settings: ServiceSettings, *, stream: TextIO | None = None) -> None:
    """Configure structlog and the standard library root logger for ``settings``.

    Logs go to ``stream`` (default stdout, as the Twelve-Factor rules require for services).
    Command line tools pass ``sys.stderr`` so their own output on stdout stays parseable.
    Safe to call more than once; the last call wins.
    """
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_service(settings.service_name),
        _add_correlation_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: Processor
    if settings.resolved_log_format == "console":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        shared.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    shared.append(redact_secrets)

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    # Standard library records (libraries such as mowdeck-authz log with ``extra={...}``) keep
    # their extra fields, e.g. the operation and error type of a failed authorization check;
    # they pass the same secret masking as every other field.
    foreign: list[Processor] = [
        structlog.stdlib.ExtraAdder(),
        _drop_noise_extras,
        *shared,
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=foreign,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def get_logger(name: str | None = None, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name, **initial)
    return logger
