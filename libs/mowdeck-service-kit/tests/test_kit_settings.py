"""ServiceSettings: environment, secret files, validation."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from mowdeck_service_kit.settings import ServiceSettings
from pydantic import ValidationError


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Remove MOWDECK_* variables of the developer shell so the tests are reproducible."""
    for key in list(os.environ):
        if key.upper().startswith("MOWDECK_"):
            monkeypatch.delenv(key)
    yield


def test_defaults_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "fleet")
    settings = ServiceSettings.from_env()
    assert settings.service_name == "fleet"
    assert settings.environment == "prod"
    assert settings.log_level == "INFO"
    assert settings.resolved_log_format == "json"
    assert settings.http_port == 8000
    assert settings.nats_enabled is True
    assert settings.database_url is None
    assert settings.metrics_enabled is True
    assert settings.shutdown_grace_seconds == 10.0


def test_env_values_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "fleet")
    monkeypatch.setenv("mowdeck_environment", "dev")
    monkeypatch.setenv("MOWDECK_LOG_LEVEL", "debug")
    monkeypatch.setenv("MOWDECK_HTTP_PORT", "9000")
    monkeypatch.setenv("MOWDECK_NATS_ENABLED", "false")
    monkeypatch.setenv("MOWDECK_NATS_URL", "nats://a:4222, nats://b:4222")
    settings = ServiceSettings.from_env()
    assert settings.environment == "dev"
    assert settings.log_level == "DEBUG"
    assert settings.resolved_log_format == "console"
    assert settings.http_port == 9000
    assert settings.nats_enabled is False
    assert settings.nats_servers == ["nats://a:4222", "nats://b:4222"]


def test_explicit_log_format_wins() -> None:
    settings = ServiceSettings(service_name="x", environment="dev", log_format="json")
    assert settings.resolved_log_format == "json"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MOWDECK_HTTP_PORT", "0"),
        ("MOWDECK_ENVIRONMENT", "staging"),
        ("MOWDECK_LOG_LEVEL", "chatty"),
        ("MOWDECK_SHUTDOWN_GRACE_SECONDS", "-1"),
    ],
)
def test_invalid_values_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "fleet")
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        ServiceSettings.from_env()


def test_service_name_is_required_and_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="service_name"):
        ServiceSettings.from_env()
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "Fleet Service")
    with pytest.raises(ValidationError, match="service_name"):
        ServiceSettings.from_env()


def test_secret_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "db_url"
    secret.write_text("postgresql://fleet:s3cret@db/fleet\n", encoding="utf-8")
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "fleet")
    monkeypatch.setenv("MOWDECK_DATABASE_URL_FILE", str(secret))
    settings = ServiceSettings.from_env()
    assert settings.database_url is not None
    assert settings.database_url.get_secret_value() == "postgresql://fleet:s3cret@db/fleet"
    assert "s3cret" not in repr(settings)


def test_file_source_works_for_any_field(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    name_file = tmp_path / "name"
    name_file.write_text("telemetry", encoding="utf-8")
    monkeypatch.setenv("MOWDECK_SERVICE_NAME_FILE", str(name_file))
    assert ServiceSettings.from_env().service_name == "telemetry"


def test_env_and_file_together_is_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "url"
    secret.write_text("nats://secret:4222", encoding="utf-8")
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "fleet")
    monkeypatch.setenv("MOWDECK_NATS_URL", "nats://plain:4222")
    monkeypatch.setenv("MOWDECK_NATS_URL_FILE", str(secret))
    with pytest.raises(ValueError, match="both MOWDECK_NATS_URL and MOWDECK_NATS_URL_FILE"):
        ServiceSettings.from_env()


def test_missing_secret_file_is_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "fleet")
    monkeypatch.setenv("MOWDECK_DATABASE_URL_FILE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="cannot read secret file"):
        ServiceSettings.from_env()


def test_explicit_arguments_override_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "fleet")
    monkeypatch.setenv("MOWDECK_HTTP_PORT", "9000")
    assert ServiceSettings(service_name="other", http_port=7000).http_port == 7000


class FleetSettings(ServiceSettings):
    service_name: str = "fleet"
    max_mowers: int = 50
    """Maximum number of mowers per account."""


def test_subclass_adds_settings_with_same_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    limit = tmp_path / "limit"
    limit.write_text("7", encoding="utf-8")
    monkeypatch.setenv("MOWDECK_MAX_MOWERS_FILE", str(limit))
    settings = FleetSettings.from_env()
    assert settings.service_name == "fleet"
    assert settings.max_mowers == 7
    assert FleetSettings.model_fields["max_mowers"].description == (
        "Maximum number of mowers per account."
    )


def test_field_docstrings_become_descriptions() -> None:
    description = ServiceSettings.model_fields["shutdown_grace_seconds"].description
    assert description is not None
    assert "grace" in description.lower() or "budget" in description.lower()
