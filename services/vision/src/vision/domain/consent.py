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

A consent is recorded for one destination (provider family and model). It covers a request
only for exactly that family and model: another provider or model needs a new consent.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from vision.domain.families import FAKE, FAKE_FAMILY, FAMILIES, family_of, private_api_base


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


def classify(model: str, *, api_base: str | None = None, assume_local: bool = False) -> Destination:
    """The destination of ``model``.

    Fake models are ``simulated``. A model whose family has a local variant (Ollama, and the
    ``openai`` prefix that OpenAI and every OpenAI-compatible endpoint share) is ``local`` when
    its base URL points into a private network, or, for Ollama, when no base URL is set.
    Everything else, including every model string nobody knows, is ``cloud``: unknown means
    cloud.

    ``assume_local`` is the operator's statement about the installation default
    (``MOWDECK_VISION_DEFAULT_LOCAL``): the endpoint runs on the operator's own hardware, which
    a model string alone cannot tell.
    """
    prefix = family_of(model)
    if prefix == FAKE_FAMILY:
        return Destination(DestinationKind.SIMULATED, _label(model, local=True), prefix, model)
    local = assume_local
    if not local and prefix in _LOCAL_PREFIXES:
        local = private_api_base(api_base) if api_base else prefix in _LOCAL_BY_DEFAULT
    kind = DestinationKind.LOCAL if local else DestinationKind.CLOUD
    return Destination(kind, _label(model, local=local), prefix, model)


def consent_required(destination: Destination) -> bool:
    """Only a request to a cloud model needs consent."""
    return destination.kind is DestinationKind.CLOUD


def covers(consent: Consent | None, destination: Destination) -> bool:
    """Does ``consent`` cover ``destination``? Provider family and model must both match."""
    if consent is None:
        return False
    given = consent.destination
    return given.family == destination.family and given.model == destination.model


def consent_missing(consent: Consent | None, destination: Destination) -> bool:
    """True when the request must be refused: consent is required and not covered."""
    return consent_required(destination) and not covers(consent, destination)
