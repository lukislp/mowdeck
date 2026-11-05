# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Internal TLS of the Compose stack (docs/ops/internal-tls.md): every hop inside the stack is
encrypted and verified against the stack's private CA, and nothing falls back to plain text."""

import re
from pathlib import Path
from typing import Any

import yaml

COMPOSE = Path("infra/compose")
TLS_MOUNT = "tls:/etc/mowdeck/tls:ro"
CA = "/etc/mowdeck/tls/ca.crt"
BUNDLE = "/etc/mowdeck/tls/ca-bundle.pem"

# Hops that stay plain on purpose, each with its reason in docs/ops/internal-tls.md.
PLAIN_ALLOWED = {
    # camera-relay -> MediaMTX: ffmpeg's RTSP client cannot check an RTSPS certificate.
    "MOWDECK_RELAY_MEDIAMTX_RTSP_URL",
    # The Matter server calls Traefik on the host's own loopback.
    "MOWDECK_MATTER_API_URL",
    # The agora-bridge listens on camera-relay's loopback (shared network namespace).
    "MOWDECK_RELAY_AGORA_BRIDGE_URL",
}


def _load(repo_root: Path, name: str = "docker-compose.yml") -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((repo_root / COMPOSE / name).read_text("utf-8"))
    return data


def _specs(services: dict[str, Any]) -> dict[str, str]:
    """``--cert=NAME:UID:GID[:EXTRA]`` of tls-init as {name: spec}."""
    specs = {}
    for arg in services["tls-init"]["command"]:
        if arg.startswith("--cert="):
            spec = arg.removeprefix("--cert=")
            specs[spec.split(":")[0]] = spec
    return specs


def _env(svc: dict[str, Any]) -> dict[str, Any]:
    env = svc.get("environment") or {}
    assert isinstance(env, dict)
    return env


def test_tls_init_runs_first_and_keeps_the_ca_to_itself(repo_root: Path) -> None:
    services = _load(repo_root)["services"]
    init = services["tls-init"]
    assert init["command"][:3] == ["python", "-m", "mowdeck_service_kit.certs"]
    assert init["user"] == "0:0" and init["read_only"] is True
    assert init["network_mode"] == "none"
    assert init["restart"] == "no"
    assert "tls-ca:/var/lib/mowdeck/ca" in init["volumes"]
    for name, svc in services.items():
        if name != "tls-init":
            assert not any("tls-ca" in str(v) for v in svc.get("volumes", [])), name
    # Every stateful backend and every container without another dependency waits for it.
    for name in ("nats", "postgres", "web", "mediamtx", "matter-server"):
        assert services[name]["depends_on"]["tls-init"] == {
            "condition": "service_completed_successfully"
        }, name


def test_every_server_has_a_certificate_and_mounts_it(repo_root: Path) -> None:
    services = _load(repo_root)["services"]
    specs = _specs(services)
    for name, svc in services.items():
        env = _env(svc)
        cert_vars = [
            key for key in env if key.endswith("TLS_CERT_FILE") or key.endswith("_TLS_CERT")
        ]
        for key in cert_vars:
            match = re.fullmatch(r"/etc/mowdeck/tls/([a-z-]+)/tls\.crt", str(env[key]))
            assert match, f"{name}: {key}"
            assert match.group(1) in specs, f"{name}: no tls-init certificate {match.group(1)}"
        if cert_vars or any(str(v).startswith("/etc/mowdeck/tls/") for v in env.values()):
            assert TLS_MOUNT in svc.get("volumes", []), name
    for name in ("postgres", "nats", "openfga", "web", "mediamtx", "matter-server"):
        assert name in specs, name
    assert specs["matter-server"].endswith(":host.docker.internal")
    assert specs["postgres"].startswith("postgres:70:70")


def test_python_services_serve_https_and_trust_the_ca(repo_root: Path) -> None:
    services = _load(repo_root)["services"]
    kit = {
        name: svc
        for name, svc in services.items()
        if "urllib.request" in " ".join(svc.get("healthcheck", {}).get("test", []))
    }
    assert len(kit) >= 13
    for name, svc in kit.items():
        env = _env(svc)
        assert env["MOWDECK_TLS_CERT_FILE"] == f"/etc/mowdeck/tls/{name}/tls.crt", name
        assert env["MOWDECK_TLS_KEY_FILE"] == f"/etc/mowdeck/tls/{name}/tls.key", name
        assert env["MOWDECK_TLS_CA_FILE"] == CA, name
        assert env["SSL_CERT_FILE"] == BUNDLE, name
        assert "'https://127.0.0.1:8000/startupz'" in " ".join(svc["healthcheck"]["test"]), name
        if "MOWDECK_NATS_URL" in env:
            assert env["MOWDECK_NATS_URL"] == "tls://nats:4222", name


def test_no_internal_hop_uses_plain_http(repo_root: Path) -> None:
    services = _load(repo_root)["services"]
    for name, svc in services.items():
        for key, value in _env(svc).items():
            text = str(value)
            if key in PLAIN_ALLOWED:
                continue
            assert not re.search(r"\bhttp://(?!127\.0\.0\.1|localhost)", text), f"{name}: {key}"
            assert "nats://" not in text, f"{name}: {key}"
            assert "sslmode=disable" not in text, f"{name}: {key}"
    for name in ("openfga", "openfga-migrate"):
        uri = _env(services[name])["OPENFGA_DATASTORE_URI"]
        assert uri.endswith(f"?sslmode=verify-full&sslrootcert={CA}"), name
    for name in ("MOWDECK_IDENTITY_MODULE_URLS",):
        urls = _env(services["identity"])[name].split(",")
        assert all(u.startswith("https://") or u.startswith("${") for u in urls)


def test_traefik_checks_every_backend_certificate(repo_root: Path) -> None:
    data = _load(repo_root)
    transports = yaml.safe_load(
        (repo_root / COMPOSE / "traefik" / "internal-tls.yml").read_text("utf-8")
    )["http"]["serversTransports"]
    for name, transport in transports.items():
        assert transport == {"serverName": name, "rootCAs": [CA]}, name
        assert "insecureSkipVerify" not in transport
    traefik = data["services"]["traefik"]
    assert "--providers.file.filename=/etc/traefik/mowdeck/internal-tls.yml" in traefik["command"]
    assert "./traefik:/etc/traefik/mowdeck:ro" in traefik["volumes"]
    routed = 0
    for name, svc in data["services"].items():
        labels = svc.get("labels") or []
        for label in labels:
            match = re.fullmatch(
                r"traefik\.http\.services\.([a-z-]+)\.loadbalancer\.server\.port=.+", label
            )
            if not match or name == "whoami":
                continue
            service = match.group(1)
            assert f"traefik.http.services.{service}.loadbalancer.server.scheme=https" in labels, (
                name
            )
            transport = next(
                label.split("=", 1)[1]
                for label in labels
                if label.startswith(
                    f"traefik.http.services.{service}.loadbalancer.serverstransport="
                )
            )
            assert transport.endswith("@file") and transport.removesuffix("@file") in transports
            routed += 1
    assert routed >= 15
    assert not any("insecureskipverify" in str(v).lower() for v in traefik["command"])


def test_backends_require_tls(repo_root: Path) -> None:
    services = _load(repo_root)["services"]
    # NATS: TLS on the client port, monitoring on the loopback only, no monitoring host port.
    nats = services["nats"]
    assert nats["command"][:2] == ["-c", "/etc/nats/mowdeck/server.conf"]
    assert not any("8222" in port for port in nats["ports"])
    server = (repo_root / COMPOSE / "nats" / "server.conf").read_text("utf-8")
    assert "include auth.conf" in server
    assert 'cert_file: "/etc/mowdeck/tls/nats/tls.crt"' in server
    assert 'http: "127.0.0.1:8222"' in server
    # PostgreSQL: TLS on, and the network may only connect with it.
    command = services["postgres"]["command"]
    assert "ssl=on" in command
    assert "hba_file=/etc/mowdeck/postgres/pg_hba.conf" in command
    hba = [
        line.split()
        for line in (repo_root / COMPOSE / "postgres" / "pg_hba.conf")
        .read_text("utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    ]
    network = [
        rule
        for rule in hba
        if rule[0].startswith("host") and rule[3] not in {"127.0.0.1/32", "::1/128"}
    ]
    assert network == [
        ["hostssl", "all", "all", "all", "scram-sha-256"],
        ["hostnossl", "all", "all", "all", "reject"],
    ]
    # OpenFGA: both APIs with TLS, the health check verifies.
    env = _env(services["openfga"])
    assert env["OPENFGA_HTTP_TLS_ENABLED"] == env["OPENFGA_GRPC_TLS_ENABLED"] == "true"
    assert env["OPENFGA_METRICS_ADDR"].startswith("127.0.0.1:")
    assert f"-tls-ca-cert={CA}" in services["openfga"]["healthcheck"]["test"]
    # MediaMTX: API and WHEP with TLS.
    env = _env(services["mediamtx"])
    assert env["MTX_APIENCRYPTION"] == env["MTX_WEBRTCENCRYPTION"] == "yes"
    # The web app: HTTPS for Traefik, plain HTTP on the loopback for the health check.
    assert _env(services["web"])["MOWDECK_WEB_TLS_CERT_FILE"] == "/etc/mowdeck/tls/web/tls.crt"


def test_demo_keeps_the_tls_wiring(repo_root: Path) -> None:
    demo = yaml.safe_load(
        (repo_root / COMPOSE / "docker-compose.demo.yml")
        .read_text("utf-8")
        .replace("!override", "")
        .replace("!reset", "")
    )["services"]
    assert (
        "--providers.file.filename=/etc/traefik/mowdeck/internal-tls.yml"
        in demo["traefik"]["command"]
    )
    assert demo["demo-seed"]["environment"]["SSL_CERT_FILE"] == BUNDLE
    assert TLS_MOUNT in demo["demo-seed"]["volumes"]
