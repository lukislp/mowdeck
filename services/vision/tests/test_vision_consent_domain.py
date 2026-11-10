# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The consent rules (ADR 0081): classification, when consent is required, what it covers."""

from datetime import UTC, datetime

import pytest

from vision.domain.consent import (
    Consent,
    Destination,
    DestinationKind,
    classify,
    consent_missing,
    consent_required,
    covers,
)

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
CLOUD, LOCAL, SIMULATED = DestinationKind.CLOUD, DestinationKind.LOCAL, DestinationKind.SIMULATED


@pytest.mark.parametrize(
    ("model", "api_base", "assume_local", "kind", "provider"),
    [
        ("anthropic/claude-sonnet-5-5", None, False, CLOUD, "Anthropic"),
        ("gemini/gemini-2.5-flash", None, False, CLOUD, "Google Gemini"),
        ("mistral/pixtral-large-latest", None, False, CLOUD, "Mistral"),
        ("openai/gpt-5-mini", None, False, CLOUD, "OpenAI"),
        # openai/ is shared: a private base URL makes it the local compatible endpoint
        ("openai/qwen", "http://vllm:8000/v1", False, LOCAL, "OpenAI-compatible endpoint"),
        ("openai/qwen", "http://192.168.1.20:1234/v1", False, LOCAL, "OpenAI-compatible endpoint"),
        ("openai/gpt-5-mini", "https://api.openai.com/v1", False, CLOUD, "OpenAI"),
        ("openai/gpt-5-mini", "https://proxy.example.org/v1", False, CLOUD, "OpenAI"),
        ("ollama/llava", None, False, LOCAL, "Ollama"),
        ("ollama/llava", "http://ollama:11434", False, LOCAL, "Ollama"),
        ("ollama_chat/llava", None, False, LOCAL, "Ollama"),
        ("ollama/llava", "https://ollama.example.com", False, CLOUD, "Ollama"),
        # unknown families and the other hosted routers are cloud
        ("openrouter/some/model", None, False, CLOUD, "Openrouter"),
        ("azure/gpt-4o", None, False, CLOUD, "Azure"),
        ("weird/model", None, False, CLOUD, "Weird"),
        # the operator's statement turns the installation default local
        ("openai/gpt-5-mini", None, True, LOCAL, "OpenAI-compatible endpoint"),
        ("anthropic/claude-sonnet-5-5", None, True, LOCAL, "Anthropic"),
        ("fake/recover", None, False, SIMULATED, "Fake (development)"),
        ("fake/recover", None, True, SIMULATED, "Fake (development)"),
    ],
)
def test_classify(
    model: str, api_base: str | None, assume_local: bool, kind: DestinationKind, provider: str
) -> None:
    destination = classify(model, api_base=api_base, assume_local=assume_local)
    assert (destination.kind, destination.provider) == (kind, provider)
    assert destination.model == model
    assert destination.family == model.split("/", 1)[0]


def test_only_cloud_requires_consent() -> None:
    assert consent_required(classify("anthropic/claude-sonnet-5-5"))
    assert not consent_required(classify("ollama/llava"))
    assert not consent_required(classify("fake/recover"))


def _consent(model: str) -> Consent:
    return Consent(granted_at=NOW, destination=classify(model))


def test_consent_covers_the_same_family_and_model_only() -> None:
    given = _consent("anthropic/claude-sonnet-5-5")
    assert covers(given, classify("anthropic/claude-sonnet-5-5"))
    assert not covers(given, classify("anthropic/claude-opus-5-5")), "another model"
    assert not covers(given, classify("openai/gpt-5-mini")), "another provider"
    assert not covers(None, classify("anthropic/claude-sonnet-5-5"))


def test_consent_is_missing_only_for_uncovered_cloud_destinations() -> None:
    cloud = classify("anthropic/claude-sonnet-5-5")
    assert consent_missing(None, cloud)
    assert consent_missing(_consent("openai/gpt-5-mini"), cloud)
    assert not consent_missing(_consent("anthropic/claude-sonnet-5-5"), cloud)
    for exempt in (classify("ollama/llava"), classify("fake/recover")):
        assert not consent_missing(None, exempt)


def test_a_model_change_needs_a_new_consent() -> None:
    old = Destination(CLOUD, "Anthropic", "anthropic", "anthropic/claude-sonnet-5-5")
    new = Destination(CLOUD, "Anthropic", "anthropic", "anthropic/claude-haiku-5-5")
    given = Consent(NOW, old)
    assert not consent_missing(given, old)
    assert consent_missing(given, new)
