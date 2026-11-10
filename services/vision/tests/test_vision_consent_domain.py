# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The consent rules (ADR 0081): classification, when consent is required, what it covers."""

from datetime import UTC, datetime

import pytest

from vision.domain.consent import (
    Consent,
    Destination,
    DestinationKind,
    classify_installation,
    classify_own,
    consent_missing,
    consent_required,
    covers,
    endpoint_of,
)

NOW = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
CLOUD, LOCAL, SIMULATED = DestinationKind.CLOUD, DestinationKind.LOCAL, DestinationKind.SIMULATED


@pytest.mark.parametrize(
    ("model", "api_base", "kind", "provider"),
    [
        ("anthropic/claude-sonnet-5-5", None, CLOUD, "Anthropic"),
        ("gemini/gemini-2.5-flash", None, CLOUD, "Google Gemini"),
        ("mistral/pixtral-large-latest", None, CLOUD, "Mistral"),
        ("openai/gpt-5-mini", None, CLOUD, "OpenAI"),
        # openai/ is shared: a private base URL makes it the local compatible endpoint
        ("openai/qwen", "http://vllm:8000/v1", LOCAL, "OpenAI-compatible endpoint"),
        ("openai/qwen", "http://192.168.1.20:1234/v1", LOCAL, "OpenAI-compatible endpoint"),
        ("openai/gpt-5-mini", "https://api.openai.com/v1", CLOUD, "OpenAI"),
        ("openai/gpt-5-mini", "https://proxy.example.org/v1", CLOUD, "OpenAI"),
        ("ollama/llava", None, LOCAL, "Ollama"),
        ("ollama/llava", "http://ollama:11434", LOCAL, "Ollama"),
        ("ollama_chat/llava", None, LOCAL, "Ollama"),
        ("ollama/llava", "https://ollama.example.com", CLOUD, "Ollama"),
        # unknown families and the other hosted routers are cloud
        ("openrouter/some/model", None, CLOUD, "Openrouter"),
        ("azure/gpt-4o", None, CLOUD, "Azure"),
        ("weird/model", None, CLOUD, "Weird"),
        ("fake/recover", None, SIMULATED, "Fake (development)"),
    ],
)
def test_classify_own(
    model: str, api_base: str | None, kind: DestinationKind, provider: str
) -> None:
    destination = classify_own(model, api_base=api_base)
    assert (destination.kind, destination.provider) == (kind, provider)
    assert destination.model == model
    assert destination.family == model.split("/", 1)[0]


@pytest.mark.parametrize(
    ("model", "api_base", "declared", "kind"),
    [
        # the heuristic is off: only the operator's declaration makes the installation local
        ("openai/gpt-5-mini", "http://litellm:4000", False, CLOUD),
        ("openai/qwen", "http://192.168.1.20:1234/v1", False, CLOUD),
        ("ollama/llava", None, False, CLOUD),
        ("ollama/llava", "http://ollama:11434", False, CLOUD),
        ("anthropic/claude-sonnet-5-5", None, False, CLOUD),
        ("openai/gpt-5-mini", None, True, LOCAL),
        ("ollama/llava", "http://ollama:11434", True, LOCAL),
        ("anthropic/claude-sonnet-5-5", "http://gateway:4000", True, LOCAL),
        ("fake/recover", None, False, SIMULATED),
        ("fake/recover", None, True, SIMULATED),
    ],
)
def test_classify_installation_never_guesses(
    model: str, api_base: str | None, declared: bool, kind: DestinationKind
) -> None:
    assert classify_installation(model, api_base=api_base, declared_local=declared).kind is kind


def test_classify_installation_defaults_to_cloud() -> None:
    assert classify_installation("ollama/llava").kind is CLOUD


def test_only_cloud_requires_consent() -> None:
    assert consent_required(classify_own("anthropic/claude-sonnet-5-5"))
    assert not consent_required(classify_own("ollama/llava"))
    assert not consent_required(classify_own("fake/recover"))


@pytest.mark.parametrize(
    ("api_base", "endpoint"),
    [
        (None, "default"),
        ("", "default"),
        ("not a url", "default"),
        ("https://API.OpenAI.com/v1", "api.openai.com"),
        ("https://api.openai.com:443/v1", "api.openai.com"),
        ("http://gateway:4000/v1/", "gateway:4000"),
        ("http://gateway:80", "gateway"),
        ("https://proxy.example.org./v1", "proxy.example.org"),
    ],
)
def test_endpoint_is_the_normalised_host(api_base: str | None, endpoint: str) -> None:
    assert endpoint_of(api_base) == endpoint


def _consent(model: str, api_base: str | None = None) -> Consent:
    return Consent(granted_at=NOW, destination=classify_own(model, api_base=api_base))


def test_consent_covers_the_same_family_model_and_endpoint_only() -> None:
    given = _consent("anthropic/claude-sonnet-5-5")
    assert covers(given, classify_own("anthropic/claude-sonnet-5-5"))
    assert not covers(given, classify_own("anthropic/claude-opus-5-5")), "another model"
    assert not covers(given, classify_own("openai/gpt-5-mini")), "another provider"
    assert not covers(None, classify_own("anthropic/claude-sonnet-5-5"))


def test_consent_is_bound_to_the_recipient() -> None:
    gpt = "openai/gpt-5-mini"
    default = _consent(gpt)
    assert covers(default, classify_own(gpt)), "default endpoint again"
    assert not covers(default, classify_own(gpt, api_base="https://llm.other.example/v1")), (
        "the same model string at another third party"
    )
    custom = _consent(gpt, "https://llm.other.example/v1")
    assert covers(custom, classify_own(gpt, api_base="https://LLM.other.example/v1/")), "same host"
    assert covers(custom, classify_own(gpt, api_base="https://llm.other.example/other/path"))
    assert not covers(custom, classify_own(gpt)), "custom host, then the default endpoint"
    assert not covers(custom, classify_own(gpt, api_base="https://llm.third.example/v1"))
    assert not covers(custom, classify_own(gpt, api_base="https://llm.other.example:8443/v1"))


def test_consent_binds_the_installations_base_host_too() -> None:
    first = Consent(NOW, classify_installation("openai/gpt-5-mini", api_base="http://gw-a:4000"))
    same = classify_installation("openai/gpt-5-mini", api_base="http://gw-a:4000/v1")
    other = classify_installation("openai/gpt-5-mini", api_base="http://gw-b:4000")
    assert covers(first, same)
    assert not covers(first, other)


def test_consent_is_missing_only_for_uncovered_cloud_destinations() -> None:
    cloud = classify_own("anthropic/claude-sonnet-5-5")
    assert consent_missing(None, cloud)
    assert consent_missing(_consent("openai/gpt-5-mini"), cloud)
    assert not consent_missing(_consent("anthropic/claude-sonnet-5-5"), cloud)
    for exempt in (classify_own("ollama/llava"), classify_own("fake/recover")):
        assert not consent_missing(None, exempt)


def test_a_model_change_needs_a_new_consent() -> None:
    old = Destination(CLOUD, "Anthropic", "anthropic", "anthropic/claude-sonnet-5-5")
    new = Destination(CLOUD, "Anthropic", "anthropic", "anthropic/claude-haiku-5-5")
    given = Consent(NOW, old)
    assert not consent_missing(given, old)
    assert consent_missing(given, new)


def test_custom_endpoint_flag() -> None:
    assert not classify_own("openai/gpt-5-mini").custom_endpoint
    assert classify_own("openai/gpt-5-mini", api_base="https://x.example/v1").custom_endpoint
