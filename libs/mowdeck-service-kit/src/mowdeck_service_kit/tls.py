# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""TLS for the hops inside an installation (docs/ops/internal-tls.md).

Every service serves HTTPS with a certificate of the installation's private CA
(``MOWDECK_TLS_CERT_FILE``, ``MOWDECK_TLS_KEY_FILE``) and verifies the other side of every
internal connection against that CA (``MOWDECK_TLS_CA_FILE``): NATS and PostgreSQL through the
contexts built here, HTTP clients through ``SSL_CERT_FILE`` (the CA plus the public roots, so
calls to the internet keep working). Compose issues the certificates with the one-shot job
``tls-init`` (:mod:`mowdeck_service_kit.certs`), Helm with cert-manager or its own CA.

An installation that is upgraded keeps working without editing its ``.env``: with
``MOWDECK_TLS_CA_FILE`` set, an ``http://`` URL that names a MowDeck service or OpenFGA (the
values an older ``.env`` carries, e.g. ``OPENFGA_API_URL=http://openfga:8080``) is used as
``https://``, because those services no longer speak plain HTTP.

:func:`write_bundle` writes that bundle (the public roots the image ships plus the CA);
:mod:`mowdeck_service_kit.trust_bundle` is its command, the Helm chart's init container.
"""

import ssl
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

INTERNAL_HOSTS = frozenset(
    {
        "assistant",
        "billing",
        "camera-relay",
        "device-gateway",
        "entitlements",
        "fleet",
        "identity",
        "mediamtx",
        "notifications",
        "openfga",
        "planner",
        "realtime",
        "scheduler",
        "telemetry",
        "vision",
        "web",
        # The Matter server on the host network (profile matter), as Compose's containers reach it.
        "host.docker.internal",
        # The placeholder of URL templates such as Identity's demo service URL.
        "{service}",
    }
)
"""Host names of the Compose services that serve HTTPS only once internal TLS is on."""


def client_context(ca_file: str) -> ssl.SSLContext:
    """A client context that trusts only ``ca_file`` and checks the host name (TLS 1.2+)."""
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def upgrade_internal_url(url: str) -> str:
    """``https://`` instead of ``http://`` for a URL whose host is in :data:`INTERNAL_HOSTS`.

    Anything else (another scheme, another host, an unparsable value) comes back unchanged.
    """
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return url
    if parts.scheme.lower() != "http" or host is None or host not in INTERNAL_HOSTS:
        return url
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))


def public_roots() -> bytes:
    """The public roots of the image: certifi (what httpx uses), else the system bundle."""
    try:
        import certifi  # present in every service image (httpx)

        return Path(certifi.where()).read_bytes()
    except (ImportError, OSError):
        pass
    try:
        return Path("/etc/ssl/certs/ca-certificates.crt").read_bytes()
    except OSError:
        return b""


def write_bundle(ca_file: Path, out: Path, *, roots: bytes | None = None) -> None:
    """Write ``out``: the public roots followed by the CA certificates of ``ca_file``."""
    ca = ca_file.read_bytes()
    if b"-----BEGIN CERTIFICATE-----" not in ca:
        msg = f"{ca_file} contains no PEM certificate"
        raise ValueError(msg)
    roots = public_roots() if roots is None else roots
    if roots and not roots.endswith(b"\n"):
        roots += b"\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(roots + ca)


def upgrade_internal_urls(value: str) -> str:
    """:func:`upgrade_internal_url` for every entry of a comma separated list."""
    if "," not in value:
        return upgrade_internal_url(value)
    return ",".join(
        upgrade_internal_url(item) if item.strip() else item for item in value.split(",")
    )
