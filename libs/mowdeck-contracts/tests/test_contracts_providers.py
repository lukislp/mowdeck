"""Cloud provider descriptors and their credential checks (ADR 0066)."""

from typing import Any

import pytest
from mowdeck_contracts.v1 import (
    RESERVED_OPTION_KEYS,
    CloudProvidersReply,
    CredentialField,
    CredentialFieldKind,
    ProviderDescriptor,
)
from mowdeck_contracts.v1.subjects import GATEWAY_CLOUD_PROVIDERS, stream_for_subject
from pydantic import ValidationError


def _descriptor(**changes: Any) -> ProviderDescriptor:
    data: dict[str, Any] = {
        "provider": "acme",
        "name": "Acme Cloud",
        "fields": [
            {"key": "email", "kind": "email", "label": "E-mail", "identity": True},
            {"key": "password", "kind": "secret", "label": "Password"},
            {
                "key": "brand",
                "kind": "choice",
                "label": "Brand",
                "choices": [{"value": "acme", "label": "Acme"}, {"value": "zeta", "label": "Zeta"}],
            },
            {
                "key": "region",
                "kind": "text",
                "label": "Region",
                "required": False,
                "pattern": "[a-z]{2}",
            },
        ],
        "links": [{"label": "Portal", "url": "https://example.com/portal"}],
    }
    data.update(changes)
    return ProviderDescriptor.model_validate(data)


def test_descriptor_defaults_to_untested_without_developer_account() -> None:
    found = _descriptor()
    assert not found.tested_on_hardware
    assert not found.needs_developer_account
    assert found.identity_field.key == "email"
    assert found.secret_keys == frozenset({"password"})


@pytest.mark.parametrize(
    "fields",
    [
        # no identity
        [{"key": "password", "kind": "secret", "label": "P"}],
        # two identities
        [
            {"key": "a", "kind": "text", "label": "A", "identity": True},
            {"key": "b", "kind": "text", "label": "B", "identity": True},
            {"key": "p", "kind": "secret", "label": "P"},
        ],
        # no secret
        [{"key": "a", "kind": "text", "label": "A", "identity": True}],
        # duplicate keys
        [
            {"key": "a", "kind": "text", "label": "A", "identity": True},
            {"key": "a", "kind": "secret", "label": "P"},
        ],
        # a secret identity
        [{"key": "a", "kind": "secret", "label": "A", "identity": True}],
        # a choice without choices
        [
            {"key": "a", "kind": "text", "label": "A", "identity": True},
            {"key": "p", "kind": "secret", "label": "P"},
            {"key": "c", "kind": "choice", "label": "C"},
        ],
    ],
)
def test_descriptor_rejects_inconsistent_fields(fields: list[dict[str, Any]]) -> None:
    with pytest.raises(ValidationError):
        _descriptor(fields=fields)


def test_descriptor_rejects_bad_ids_links_and_patterns() -> None:
    with pytest.raises(ValidationError):
        _descriptor(provider="Acme Cloud")
    with pytest.raises(ValidationError):
        _descriptor(links=[{"label": "Portal", "url": "http://example.com"}])
    with pytest.raises(ValidationError):
        CredentialField(key="a", kind=CredentialFieldKind.TEXT, label="A", pattern="(")


def test_normalized_strips_shown_values_but_keeps_secrets() -> None:
    found = _descriptor().normalized(
        {"email": " A@x.test ", "password": " pass ", "brand": "acme ", "region": "  "}
    )
    assert found == {"email": "A@x.test", "password": " pass ", "brand": "acme"}


def test_problems_on_create() -> None:
    descriptor = _descriptor()
    ok = {"email": "a@x.test", "password": "p", "brand": "acme"}
    assert descriptor.problems(ok) == []
    assert descriptor.problems({**ok, "region": "de"}) == []
    codes = {
        p.key: p.code
        for p in descriptor.problems(
            {"email": "nope", "brand": "omega", "region": "germany", "other": "x"}
        )
    }
    assert codes == {
        "email": "format",
        "password": "required",
        "brand": "choice",
        "region": "format",
        "other": "unknown",
    }
    too_long = descriptor.problems({**ok, "password": "x" * 300})
    assert [(p.key, p.code) for p in too_long] == [("password", "too_long")]


def test_problems_on_reauthentication_take_only_secrets() -> None:
    descriptor = _descriptor()
    assert descriptor.problems({}, only_secrets=True) == []
    assert descriptor.problems({"password": "new"}, only_secrets=True) == []
    found = descriptor.problems({"email": "b@x.test"}, only_secrets=True)
    assert [(p.key, p.code) for p in found] == [("email", "unknown")]


def test_reply_and_subject() -> None:
    reply = CloudProvidersReply(providers=[_descriptor()])
    assert CloudProvidersReply.model_validate(reply.model_dump(mode="json")) == reply
    assert CloudProvidersReply().providers == []
    assert GATEWAY_CLOUD_PROVIDERS == "gateway.cloud.providers"
    assert stream_for_subject(GATEWAY_CLOUD_PROVIDERS) is None


def test_descriptor_declares_mower_options() -> None:
    options = [
        {"key": "refresh_seconds", "type": "number", "title": "Refresh", "minimum": 10},
        {"key": "night_mode", "type": "boolean", "title": "Night mode", "default": False},
    ]
    found = _descriptor(mower_options=options)
    assert [o.key for o in found.mower_options] == ["refresh_seconds", "night_mode"]
    assert _descriptor().mower_options == []  # a provider without settings


@pytest.mark.parametrize(
    "changes",
    [
        # a credential field with a key MowDeck manages on every cloud mower
        {
            "fields": [
                {"key": "device_name", "kind": "text", "label": "A", "identity": True},
                {"key": "p", "kind": "secret", "label": "P"},
            ]
        },
        # a mower option with a reserved key
        {"mower_options": [{"key": "cloud_provider", "type": "string", "title": "X"}]},
        # a mower option with the key of a credential field
        {"mower_options": [{"key": "password", "type": "string", "title": "X"}]},
        # duplicate options
        {
            "mower_options": [
                {"key": "a", "type": "string", "title": "A"},
                {"key": "a", "type": "number", "title": "B"},
            ]
        },
    ],
)
def test_descriptor_guards_option_keys(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _descriptor(**changes)
    assert "iot_id" in RESERVED_OPTION_KEYS
