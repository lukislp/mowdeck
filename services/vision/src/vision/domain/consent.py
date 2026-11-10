# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Explicit consent before user data goes to a cloud AI provider (ADR 0081).

Pure rules, no I/O. A request sends camera pictures, a map sketch with area names and mower
status (AI assist, automatic analysis) or typed text with mower and area names (chat
assistant) to the model of the *effective provider*. Where that model runs decides whether the
user must have agreed first:

* ``cloud``: a third party outside this installation, so consent is required;
* ``local``: the user's or the operator's own hardware, no consent;
* ``simulated``: the rule based fake model (demo, development), nothing leaves the process.

A consent is recorded for one destination: provider family, model and endpoint (the host, and
the port when it is not the scheme's default, of the effective base URL, or ``default`` for the
provider's own endpoint). It covers a request only for exactly that destination: another
provider, model or endpoint, that is another recipient, needs a new consent.

Who decides where a request really goes differs. A user's *own* provider is classified by a
heuristic (:func:`classify_own`): the person whose data it is chose the endpoint, so a private
network address or Ollama means local. The *installation's* model is not the user's choice and
the user cannot see where the operator's gateway forwards to, so :func:`classify_installation`
never guesses: it is local only when the operator declared so
(``MOWDECK_VISION_DEFAULT_LOCAL=true``), cloud otherwise, whatever its prefix or base URL.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit

from vision.domain.families import FAKE, FAKE_FAMILY, FAMILIES, family_of, private_api_base

DEFAULT_ENDPOINT: Final = "default"
"""The endpoint of a provider without a base URL."""


class DestinationKind(StrEnum):
    """Where a request goes."""

    CLOUD = "cloud"
    LOCAL = "local"
    SIMULATED = "simulated"


@dataclass(frozen=True, slots=True)
class Destination:
    """Where the data of one request goes: the kind, the provider's label and the model."""

    kind: DestinationKind
    provider: str
    """Human readable provider name (``Anthropic``), for the consent text."""
    family: str
    """Family key, the model string's prefix (``anthropic``)."""
    model: str
    """The full model string (``anthropic/claude-sonnet-5-5``)."""
    endpoint: str = "default"
    """The recipient's address: ``default`` (the provider's own endpoint) or the normalised
    host of the base URL (``host`` or ``host:port``)."""

    @property
    def custom_endpoint(self) -> bool:
        """The data goes to a configured address, not to the provider's default endpoint."""
        return self.endpoint != DEFAULT_ENDPOINT


@dataclass(frozen=True, slots=True)
class Consent:
    """A user's agreement that data may go to one destination."""

    granted_at: datetime
    destination: Destination


_LOCAL_PREFIXES: Final = frozenset({f.prefix for f in FAMILIES if f.local} | {"ollama_chat"})
"""Model string prefixes that have a local variant (``openai`` for any compatible endpoint)."""
_LOCAL_BY_DEFAULT: Final = frozenset({"ollama", "ollama_chat"})
"""Prefixes that run locally even without a base URL (Ollama's default base is the LAN)."""


def _label(model: str, *, local: bool) -> str:
    prefix = family_of(model)
    if prefix == "ollama_chat":
        prefix = "ollama"
    if prefix == FAKE_FAMILY:
        return FAKE.label
    for family in FAMILIES:
        if family.prefix == prefix and family.local == local:
            return family.label
    for family in FAMILIES:
        if family.prefix == prefix:
            return family.label
    return prefix.capitalize()


def endpoint_of(api_base: str | None) -> str:
    """The recipient of a base URL: lower-case host, plus the port unless it is the scheme's
    default; ``default`` without a (usable) base URL."""
    if not api_base or not api_base.strip():
        return DEFAULT_ENDPOINT
    parts = urlsplit(api_base.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return DEFAULT_ENDPOINT
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and port != {"http": 80, "https": 443}.get(parts.scheme):
        return f"{host}:{port}"
    return host


def classify_own(model: str, *, api_base: str | None = None) -> Destination:
    """The destination of a user's *own* provider.

    Fake models are ``simulated``. A model whose family has a local variant (Ollama, and the
    ``openai`` prefix that OpenAI and every OpenAI-compatible endpoint share) is ``local`` when
    its base URL points into a private network, or, for Ollama, when no base URL is set. The
    person whose data it is chose the endpoint, so this guess is theirs to make. Everything
    else, including every model string nobody knows, is ``cloud``: unknown means cloud.
    """
    prefix = family_of(model)
    if prefix == FAKE_FAMILY:
        return Destination(DestinationKind.SIMULATED, _label(model, local=True), prefix, model)
    local = False
    if prefix in _LOCAL_PREFIXES:
        local = private_api_base(api_base) if api_base else prefix in _LOCAL_BY_DEFAULT
    kind = DestinationKind.LOCAL if local else DestinationKind.CLOUD
    return Destination(kind, _label(model, local=local), prefix, model, endpoint_of(api_base))


def classify_installation(
    model: str, *, api_base: str | None = None, declared_local: bool = False
) -> Destination:
    """The destination of the *installation's* model.

    ``fake/...`` is ``simulated``. Otherwise it is ``local`` only when the operator declared it
    (``MOWDECK_VISION_DEFAULT_LOCAL=true``) and ``cloud`` in every other case: neither the model
    prefix nor a private base URL says where an operator's gateway forwards to, and the user
    cannot see it.
    """
    prefix = family_of(model)
    if prefix == FAKE_FAMILY:
        return Destination(DestinationKind.SIMULATED, _label(model, local=True), prefix, model)
    kind = DestinationKind.LOCAL if declared_local else DestinationKind.CLOUD
    label = _label(model, local=declared_local)
    return Destination(kind, label, prefix, model, endpoint_of(api_base))


def consent_required(destination: Destination) -> bool:
    """Only a request to a cloud model needs consent."""
    return destination.kind is DestinationKind.CLOUD


def covers(consent: Consent | None, destination: Destination) -> bool:
    """Does ``consent`` cover ``destination``? Provider family, model and endpoint must all match:
    the same model string at another address is another recipient."""
    if consent is None:
        return False
    given = consent.destination
    return (
        given.family == destination.family
        and given.model == destination.model
        and given.endpoint == destination.endpoint
    )


def consent_missing(consent: Consent | None, destination: Destination) -> bool:
    """True when the request must be refused: consent is required and not covered."""
    return consent_required(destination) and not covers(consent, destination)
