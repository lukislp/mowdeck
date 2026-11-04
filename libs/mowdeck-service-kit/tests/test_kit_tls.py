# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""Internal TLS: settings, server and client options, URL upgrade, and the certificate job."""

import asyncio
import datetime as dt
import os
import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from mowdeck_service_kit import certs, db, tls
from mowdeck_service_kit.app import server_tls_options
from mowdeck_service_kit.bus.jetstream import JetStreamBus, nats_tls_context
from mowdeck_service_kit.db import create_engine, database_connect_args
from mowdeck_service_kit.settings import ServiceSettings
from mowdeck_service_kit.tls import client_context, upgrade_internal_url, upgrade_internal_urls
from pydantic import ValidationError
from sqlalchemy.ext import asyncio as sqlalchemy_asyncio

NOW = dt.datetime(2026, 11, 4, 12, 0, tzinfo=dt.UTC)
UID = os.getuid() if hasattr(os, "getuid") else 0
GID = os.getgid() if hasattr(os, "getgid") else 0


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ):
        if key.upper().startswith("MOWDECK_"):
            monkeypatch.delenv(key)
    yield


def spec(name: str, *extra: str) -> certs.CertSpec:
    return certs.CertSpec(name=name, uid=UID, gid=GID, extra_names=extra)


@pytest.fixture
def pki(tmp_path: Path) -> Path:
    """A CA and certificates for identity and postgres, as the tls-init job writes them."""
    certs.ensure(
        tmp_path / "ca",
        tmp_path / "tls",
        [spec("identity"), spec("postgres")],
        now=NOW,
        public_roots=b"",
    )
    return tmp_path / "tls"


# --- URL upgrade ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://openfga:8080", "https://openfga:8080"),
        ("http://camera-relay:8000/path?x=1", "https://camera-relay:8000/path?x=1"),
        ("http://host.docker.internal:8790", "https://host.docker.internal:8790"),
        ("http://{service}:8000/api/v1/{service}", "https://{service}:8000/api/v1/{service}"),
        ("https://openfga:8080", "https://openfga:8080"),
        ("http://ollama:11434", "http://ollama:11434"),
        ("http://fake-github:8000/x", "http://fake-github:8000/x"),
        ("nats://nats:4222", "nats://nats:4222"),
        ("", ""),
    ],
)
def test_upgrade_internal_url(url: str, expected: str) -> None:
    assert upgrade_internal_url(url) == expected


def test_upgrade_internal_urls_keeps_other_entries() -> None:
    value = "http://fleet:8000,http://example.org:8000,https://vision:8000,"
    assert upgrade_internal_urls(value) == (
        "https://fleet:8000,http://example.org:8000,https://vision:8000,"
    )


# --- settings ---------------------------------------------------------------------------------


class _UrlSettings(ServiceSettings):
    peer_url: str = "http://identity:8000"
    module_urls: str = "http://fleet:8000,http://example.org"
    other: str = "http://fleet:8000"


def test_tls_settings_default_to_off() -> None:
    settings = ServiceSettings(service_name="fleet")
    assert settings.tls_cert_file is None
    assert settings.tls_ca_file is None
    assert settings.tls_server_enabled is False
    assert server_tls_options(settings) == {}


def test_empty_tls_settings_are_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "fleet")
    monkeypatch.setenv("MOWDECK_TLS_CERT_FILE", "")
    monkeypatch.setenv("MOWDECK_TLS_KEY_FILE", " ")
    monkeypatch.setenv("MOWDECK_TLS_CA_FILE", "")
    settings = ServiceSettings.from_env()
    assert (settings.tls_cert_file, settings.tls_key_file, settings.tls_ca_file) == (
        None,
        None,
        None,
    )


def test_certificate_needs_its_key() -> None:
    with pytest.raises(ValidationError, match="MOWDECK_TLS_KEY_FILE"):
        ServiceSettings(service_name="fleet", tls_cert_file="/tls/fleet/tls.crt")


def test_server_options_with_certificate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "fleet")
    monkeypatch.setenv("MOWDECK_TLS_CERT_FILE", "/tls/fleet/tls.crt")
    monkeypatch.setenv("MOWDECK_TLS_KEY_FILE", "/tls/fleet/tls.key")
    settings = ServiceSettings.from_env()
    assert settings.tls_server_enabled is True
    assert server_tls_options(settings) == {
        "ssl_certfile": "/tls/fleet/tls.crt",
        "ssl_keyfile": "/tls/fleet/tls.key",
    }


def test_internal_urls_become_https_with_the_ca() -> None:
    settings = _UrlSettings(
        service_name="fleet", tls_ca_file="/tls/ca.crt", module_urls="http://vision:8000"
    )
    assert settings.peer_url == "https://identity:8000"
    assert settings.module_urls == "https://vision:8000"
    assert settings.other == "http://fleet:8000"  # not a *_url setting
    assert settings.nats_url == "nats://localhost:4222"
    # Only a configured value is reported (and logged), not an upgraded default.
    assert settings.upgraded_internal_urls == ("module_urls",)


def test_internal_urls_from_the_environment_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOWDECK_SERVICE_NAME", "fleet")
    monkeypatch.setenv("MOWDECK_TLS_CA_FILE", "/tls/ca.crt")
    monkeypatch.setenv("MOWDECK_PEER_URL", "http://openfga:8080")
    settings = _UrlSettings.from_env()
    assert settings.peer_url == "https://openfga:8080"
    assert settings.module_urls == "https://fleet:8000,http://example.org"
    assert settings.upgraded_internal_urls == ("peer_url",)


def test_internal_urls_stay_without_the_ca() -> None:
    settings = _UrlSettings(service_name="fleet")
    assert settings.peer_url == "http://identity:8000"
    assert settings.upgraded_internal_urls == ()


# --- clients ----------------------------------------------------------------------------------


def test_client_context_verifies_against_the_ca_only(pki: Path) -> None:
    context = client_context(str(pki / "ca.crt"))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert len(context.get_ca_certs()) == 1


def test_database_uses_tls_with_the_ca(pki: Path) -> None:
    settings = ServiceSettings(service_name="fleet", tls_ca_file=str(pki / "ca.crt"))
    args = database_connect_args(settings, "postgresql+asyncpg://u:p@postgres/fleet")
    assert isinstance(args["ssl"], ssl.SSLContext)
    assert args["ssl"].check_hostname is True


def test_database_url_with_own_ssl_choice_is_kept(pki: Path) -> None:
    settings = ServiceSettings(service_name="fleet", tls_ca_file=str(pki / "ca.crt"))
    assert database_connect_args(settings, "postgresql://u:p@db.example/fleet?ssl=require") == {}
    assert database_connect_args(settings, "postgresql://u:p@db/fleet?sslmode=verify-full") == {}


def test_database_without_the_ca_is_unchanged() -> None:
    settings = ServiceSettings(service_name="fleet")
    assert database_connect_args(settings, "postgresql://u:p@postgres/fleet") == {}


def test_engine_and_readiness_probe_use_the_tls_context(
    pki: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []
    real = sqlalchemy_asyncio.create_async_engine

    def recording(url: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(url, **kwargs)

    monkeypatch.setattr(db, "create_async_engine", recording)
    settings = ServiceSettings(
        service_name="fleet",
        tls_ca_file=str(pki / "ca.crt"),
        database_url="postgresql://u:p@postgres/fleet",  # type: ignore[arg-type]
    )
    engine = create_engine(settings, pool_size=1, connect_args={"timeout": 5})
    db.readiness_check_db(engine)
    try:
        engine_args, probe_args = calls[0]["connect_args"], calls[1]["connect_args"]
        assert isinstance(engine_args["ssl"], ssl.SSLContext)
        assert engine_args["timeout"] == 5
        assert probe_args is engine_args
    finally:
        asyncio.run(engine.dispose())


def test_nats_tls_context(pki: Path) -> None:
    plain = ServiceSettings(service_name="fleet", nats_url="nats://nats:4222")
    assert nats_tls_context(plain) is None
    public = ServiceSettings(service_name="fleet", nats_url="tls://nats.example:4222")
    assert isinstance(nats_tls_context(public), ssl.SSLContext)
    private = ServiceSettings(
        service_name="fleet", nats_url="tls://nats:4222", tls_ca_file=str(pki / "ca.crt")
    )
    context = nats_tls_context(private)
    assert context is not None
    assert len(context.get_ca_certs()) == 1
    assert JetStreamBus.from_settings(private).tls is not None


# --- certificate job --------------------------------------------------------------------------


def _leaf(pki: Path, name: str) -> x509.Certificate:
    return x509.load_pem_x509_certificate((pki / name / "tls.crt").read_bytes())


def test_job_writes_ca_bundle_and_certificates(tmp_path: Path) -> None:
    report = certs.ensure(
        tmp_path / "ca",
        tmp_path / "tls",
        [spec("identity"), spec("matter-server", "host.docker.internal")],
        now=NOW,
        public_roots=b"PUBLIC ROOTS",
    )
    assert report.ca_created is True
    assert report.issued == ["identity", "matter-server"]
    out = tmp_path / "tls"
    assert (out / "ca-bundle.pem").read_bytes().startswith(b"PUBLIC ROOTS\n-----BEGIN CERTIFICATE")
    assert not (out / "ca.key").exists()
    leaf = _leaf(out, "matter-server")
    names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(names.get_values_for_type(x509.DNSName)) == {
        "matter-server",
        "localhost",
        "host.docker.internal",
    }
    assert {str(ip) for ip in names.get_values_for_type(x509.IPAddress)} == {"127.0.0.1", "::1"}
    if os.name != "nt":
        assert (out / "identity" / "tls.key").stat().st_mode & 0o777 == 0o400
        assert (tmp_path / "ca" / "ca.key").stat().st_mode & 0o777 == 0o600


def test_job_is_idempotent(tmp_path: Path, pki: Path) -> None:
    before = (pki / "identity" / "tls.crt").read_bytes()
    report = certs.ensure(
        tmp_path / "ca", pki, [spec("identity"), spec("postgres")], now=NOW, public_roots=b""
    )
    assert report.ca_created is False
    assert report.issued == []
    assert report.kept == ["identity", "postgres"]
    assert (pki / "identity" / "tls.crt").read_bytes() == before


def test_job_issues_again_for_new_names(tmp_path: Path, pki: Path) -> None:
    report = certs.ensure(
        tmp_path / "ca", pki, [spec("identity", "identity.internal")], now=NOW, public_roots=b""
    )
    assert report.issued == ["identity"]


def test_job_renews_a_certificate_before_it_expires(tmp_path: Path, pki: Path) -> None:
    later = NOW + certs.LEAF_VALIDITY - certs.LEAF_RENEW_BEFORE + dt.timedelta(days=1)
    report = certs.ensure(tmp_path / "ca", pki, [spec("identity")], now=later, public_roots=b"")
    assert report.issued == ["identity"]
    assert _leaf(pki, "identity").not_valid_after_utc > later + certs.LEAF_RENEW_BEFORE


def test_job_replaces_a_broken_key(tmp_path: Path, pki: Path) -> None:
    key = pki / "identity" / "tls.key"
    os.chmod(key, 0o600)
    key.write_text("not a key")
    report = certs.ensure(tmp_path / "ca", pki, [spec("identity")], now=NOW, public_roots=b"")
    assert report.issued == ["identity"]


def test_job_renews_the_ca_and_keeps_trusting_the_old_one(tmp_path: Path, pki: Path) -> None:
    old_ca = x509.load_pem_x509_certificate((tmp_path / "ca" / "ca.crt").read_bytes())
    later = NOW + certs.CA_VALIDITY - certs.CA_RENEW_BEFORE + dt.timedelta(days=1)
    report = certs.ensure(tmp_path / "ca", pki, [spec("identity")], now=later, public_roots=b"")
    assert report.ca_created is True
    assert report.issued == ["identity"]
    trusted = x509.load_pem_x509_certificates((pki / "ca.crt").read_bytes())
    assert len(trusted) == 2
    assert old_ca in trusted
    _leaf(pki, "identity").verify_directly_issued_by(trusted[0])


@pytest.mark.parametrize(
    "text",
    ["identity", "identity:x:1", "Identity:1:1", "a b:1:1", ":1:1"],
)
def test_invalid_specs_are_refused(text: str) -> None:
    with pytest.raises(ValueError, match="certificate spec"):
        certs.parse_spec(text)


def test_spec_with_extra_names() -> None:
    parsed = certs.parse_spec("matter-server:1000:1000:host.docker.internal, 10.0.0.1")
    assert parsed == certs.CertSpec(
        "matter-server", 1000, 1000, ("host.docker.internal", "10.0.0.1")
    )


def test_main_reports_invalid_arguments(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = certs.main(["--ca-dir", str(tmp_path / "ca"), "--out-dir", str(tmp_path), "--cert", "x"])
    assert code == 2
    assert "certificate spec" in capsys.readouterr().err


def test_main_writes_the_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["--ca-dir", str(tmp_path / "ca"), "--out-dir", str(tmp_path / "tls")]
    assert certs.main([*args, "--cert", f"web:{UID}:{GID}"]) == 0
    assert "issued certificates for web" in capsys.readouterr().out
    assert certs.main([*args, "--cert", f"web:{UID}:{GID}"]) == 0
    assert "still valid for web" in capsys.readouterr().out


def test_trust_bundle_puts_the_ca_after_the_public_roots(pki: Path, tmp_path: Path) -> None:
    out = tmp_path / "trust" / "ca-bundle.pem"
    tls.write_bundle(pki / "ca.crt", out, roots=b"PUBLIC")
    assert out.read_bytes() == b"PUBLIC\n" + (pki / "ca.crt").read_bytes()
    # The default roots are certifi's, the ones httpx used before.
    assert tls.main(["--ca", str(pki / "ca.crt"), "--out", str(out)]) == 0
    context = ssl.create_default_context(cafile=str(out))
    assert len(context.get_ca_certs()) > 100


def test_trust_bundle_refuses_a_file_without_certificates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "ca.crt"
    empty.write_text("nothing")
    assert tls.main(["--ca", str(empty), "--out", str(tmp_path / "b.pem")]) == 1
    assert "no PEM certificate" in capsys.readouterr().err
    assert tls.main(["--ca", str(tmp_path / "missing"), "--out", str(tmp_path / "b.pem")]) == 1


# --- a real handshake -------------------------------------------------------------------------


def _serve_once(context: ssl.SSLContext) -> tuple[int, threading.Thread]:
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def accept() -> None:
        with listener:
            connection, _ = listener.accept()
            try:
                with context.wrap_socket(connection, server_side=True) as tls:
                    tls.sendall(b"ok")
            except (ssl.SSLError, OSError):
                connection.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    return port, thread


def _server_context(pki: Path, name: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pki / name / "tls.crt", pki / name / "tls.key")
    return context


def test_issued_certificate_passes_a_strict_handshake(pki: Path) -> None:
    port, thread = _serve_once(_server_context(pki, "identity"))
    client = client_context(str(pki / "ca.crt"))
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        client.wrap_socket(raw, server_hostname="identity") as tls,
    ):
        assert tls.recv(2) == b"ok"
    thread.join(5)


def test_handshake_fails_for_another_service_name(pki: Path) -> None:
    port, thread = _serve_once(_server_context(pki, "postgres"))
    client = client_context(str(pki / "ca.crt"))
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        pytest.raises(ssl.SSLCertVerificationError),
    ):
        client.wrap_socket(raw, server_hostname="identity")
    thread.join(5)


def test_handshake_fails_without_the_ca(pki: Path, tmp_path: Path) -> None:
    certs.ensure(tmp_path / "other-ca", tmp_path / "other", [], now=NOW, public_roots=b"")
    port, thread = _serve_once(_server_context(pki, "identity"))
    client = client_context(str(tmp_path / "other" / "ca.crt"))
    with (
        socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
        pytest.raises(ssl.SSLCertVerificationError),
    ):
        client.wrap_socket(raw, server_hostname="identity")
    thread.join(5)
