"""bootstrap_model(): store by name, model written only when it changed; and the CLI."""

import json
from typing import Any

import pytest
from authz_fake_sdk import FakeFgaClient, store
from mowdeck_authz import __main__ as cli
from mowdeck_authz import bootstrap_model, find_store_id, load_model
from mowdeck_authz.settings import AuthzSettings


async def test_bootstrap_creates_store_and_model_then_is_idempotent() -> None:
    client = FakeFgaClient()
    first = await bootstrap_model(client)
    assert first.store_created and first.model_written
    assert client.names().count("create_store") == 1

    second = await bootstrap_model(client)
    assert not second.store_created and not second.model_written
    assert (second.store_id, second.model_id) == (first.store_id, first.model_id)
    assert client.names().count("write_authorization_model") == 1
    assert client.names().count("create_store") == 1


async def test_bootstrap_writes_a_changed_model_into_the_existing_store() -> None:
    client = FakeFgaClient()
    first = await bootstrap_model(client)
    changed = load_model()
    changed["type_definitions"] = [*changed["type_definitions"], {"type": "camera"}]
    second = await bootstrap_model(client, model=changed)
    assert second.store_id == first.store_id
    assert second.model_written
    assert second.model_id != first.model_id


async def test_find_store_prefers_the_oldest_and_ignores_others(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeFgaClient()
    client.stores = [
        store("new", created_at="t9"),
        store("x", "other"),
        store("old", created_at="t1"),
    ]
    assert await find_store_id(client) == "old"
    assert "several OpenFGA stores" in caplog.text
    assert await find_store_id(client, "missing") is None


def test_cli_bootstrap(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeFgaClient()
    seen: list[AuthzSettings] = []

    def fake_create_client(settings: AuthzSettings) -> Any:
        seen.append(settings)
        return client

    monkeypatch.setenv("MOWDECK_OPENFGA_API_URL", "http://fga:8080")
    monkeypatch.setattr(cli, "create_client", fake_create_client)
    assert cli.main(["bootstrap", "--store-name", "demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["store_created"] is True
    assert result["model_written"] is True
    assert client.stores[0].name == "demo"
    assert client.closed
    assert seen[0].api_url == "http://fga:8080"
