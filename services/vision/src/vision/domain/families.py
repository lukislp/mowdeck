"""Model families: how a LiteLLM model string is read, what each family needs, base URLs.

A model string is ``<family>/<model>`` as LiteLLM expects it, for example
``anthropic/claude-sonnet-5-5``, ``openai/gpt-5-mini``, ``gemini/gemini-2.5-flash``,
``mistral/pixtral-large-latest``, ``ollama/llava`` or, for any OpenAI-compatible endpoint
(vLLM, LM Studio, LocalAI, a proxy), ``openai/<model>`` with a base URL. ``fake/<mode>`` selects
the rule based fake provider (development and tests; for users only when the operator allows
it).
"""

import ipaddress
import re
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

from vision.domain.errors import InvalidInput

FAKE_FAMILY: Final = "fake"
MODEL_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]*/[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$")
DEFAULT_HOSTED_MODEL: Final = "anthropic/claude-sonnet-5-5"
"""Default model string of the installation provider: the current Sonnet-class Claude model."""


@dataclass(frozen=True, slots=True)
class Family:
    """A family of vision models as the settings page offers it."""

    key: str
    label: str
    prefix: str
    """Model string prefix (``<prefix>/<model>``)."""
    examples: tuple[str, ...]
    key_env: str | None
    """Environment variable LiteLLM reads when no key is given (installation default)."""
    needs_key: bool
    needs_api_base: bool
    default_api_base: str | None = None
    local: bool = False
    """Runs on the operator's or user's own hardware: images stay local."""


FAMILIES: Final[tuple[Family, ...]] = (
    Family(
        "anthropic",
        "Anthropic",
        "anthropic",
        ("anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5", "anthropic/claude-haiku-5-5"),
        "ANTHROPIC_API_KEY",
        needs_key=True,
        needs_api_base=False,
    ),
    Family(
        "openai",
        "OpenAI",
        "openai",
        ("openai/gpt-5-mini", "openai/gpt-4.1-mini"),
        "OPENAI_API_KEY",
        needs_key=True,
        needs_api_base=False,
    ),
    Family(
        "gemini",
        "Google Gemini",
        "gemini",
        ("gemini/gemini-2.5-flash", "gemini/gemini-2.5-pro"),
        "GEMINI_API_KEY",
        needs_key=True,
        needs_api_base=False,
    ),
    Family(
        "mistral",
        "Mistral",
        "mistral",
        ("mistral/pixtral-large-latest", "mistral/mistral-medium-latest"),
        "MISTRAL_API_KEY",
        needs_key=True,
        needs_api_base=False,
    ),
    Family(
        "ollama",
        "Ollama",
        "ollama",
        ("ollama/llava", "ollama/qwen2.5vl"),
        None,
        needs_key=False,
        needs_api_base=True,
        default_api_base="http://ollama:11434",
        local=True,
    ),
    Family(
        "openai_compatible",
        "OpenAI-compatible endpoint",
        "openai",
        ("openai/<model name of the endpoint>",),
        None,
        needs_key=False,
        needs_api_base=True,
        local=True,
    ),
)
"""The families of the settings page, in display order."""

FAKE: Final = Family(
    FAKE_FAMILY,
    "Fake (development)",
    FAKE_FAMILY,
    (
        "fake/recover",
        "fake/dock",
        "fake/resume",
        "fake/decline",
        "fake/people",
        "fake/low_confidence",
        "fake/fail",
    ),
    None,
    needs_key=False,
    needs_api_base=False,
    local=True,
)
"""The rule based fake provider; users see it only with
``MOWDECK_VISION_ALLOW_FAKE_PROVIDER``."""

ALLOWED_PREFIXES: Final = frozenset(
    {"anthropic", "openai", "gemini", "mistral", "ollama", "ollama_chat", "openrouter", "azure"}
)
"""LiteLLM providers a model string may name (besides ``fake``)."""

ApiBasePolicy = Literal["any", "public", "none"]


def family_of(model: str) -> str:
    """The family key of a model string (its prefix)."""
    return model.split("/", 1)[0]


def check_model(model: str, *, allow_fake: bool) -> str:
    """Validate a model string; returns its family. Raises :class:`InvalidInput`."""
    if not MODEL_PATTERN.match(model):
        msg = "the model must look like <family>/<model>, e.g. anthropic/claude-sonnet-5-5"
        raise InvalidInput(msg)
    family = family_of(model)
    if family == FAKE_FAMILY:
        if not allow_fake:
            raise InvalidInput("the fake provider is not available here")
        return family
    if family not in ALLOWED_PREFIXES:
        allowed = ", ".join(sorted(ALLOWED_PREFIXES))
        raise InvalidInput(f"unsupported model family {family!r}; use one of {allowed}")
    return family


def _private_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        lowered = host.lower().rstrip(".")
        return (
            "." not in lowered
            or lowered == "localhost"
            or lowered.endswith((".local", ".internal", ".lan", ".home.arpa", ".localhost"))
        )
    return not address.is_global


def check_api_base(api_base: str | None, policy: ApiBasePolicy) -> str | None:
    """Validate a user's base URL against the installation's policy; returns it normalised.

    ``any`` allows every http(s) URL (a home installation with Ollama on the LAN), ``public``
    refuses loopback, private, link-local and single-label hosts (a hosted installation must
    not let users probe its internal network; DNS names are not resolved, so a public name
    that points inward is not caught), ``none`` refuses every user base URL.
    """
    if api_base is None or not api_base.strip():
        return None
    value = api_base.strip().rstrip("/")
    if policy == "none":
        raise InvalidInput("custom base URLs are disabled on this installation")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise InvalidInput("the base URL must be an http or https URL")
    if parts.username or parts.password:
        raise InvalidInput("put credentials into the API key, not into the base URL")
    if len(value) > 300:
        raise InvalidInput("the base URL is too long")
    if policy == "public" and _private_host(parts.hostname):
        raise InvalidInput("the base URL must point to a public host on this installation")
    return value
