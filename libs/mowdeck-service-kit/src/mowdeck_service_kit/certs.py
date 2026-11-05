# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""One-shot job that keeps the private CA and the certificates of the internal hops current.

    python -m mowdeck_service_kit.certs --ca-dir /var/lib/mowdeck/ca --out-dir /etc/mowdeck/tls \\
        --cert identity:10001:10001 --cert postgres:70:70 \\
        --cert matter-server:1000:1000:host.docker.internal

Compose runs it as the service ``tls-init`` before every other container (docs/ops/internal-tls.md).
It needs the ``certs`` extra (``cryptography``) and runs as root, to hand every key to the user
of the container that reads it. Every run is idempotent:

* the CA (``ca.key`` and ``ca.crt`` in ``--ca-dir``, a volume only this job mounts) is created on
  the first run and replaced :data:`CA_RENEW_BEFORE` before it expires. The previous CA stays
  trusted until it expires, so a container that still serves a certificate of the old CA keeps
  working until it is recreated;
* ``<out-dir>/ca.crt`` holds the trusted CA certificates, ``<out-dir>/ca-bundle.pem`` those plus
  the public roots (``SSL_CERT_FILE`` of the containers, so calls to the internet keep working);
* ``<out-dir>/<name>/tls.crt`` and ``tls.key`` are issued for ``<name>``, ``localhost``,
  ``127.0.0.1``, ``::1`` and the extra names of the spec, and issued again when they are missing,
  do not match their key, the CA or the names, or expire within :data:`LEAF_RENEW_BEFORE`. The key
  belongs to ``uid:gid`` with mode 0400 (PostgreSQL refuses a key others can read).

Spec of ``--cert``: ``NAME:UID:GID[:EXTRA_NAME,...]``; an extra name may be an IP address.
Exit code 0 when everything is in place, 2 for invalid arguments, 1 for any other error.
"""

import argparse
import datetime as dt
import ipaddress
import os
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mowdeck_service_kit.tls import public_roots as tls_public_roots

CA_VALIDITY = dt.timedelta(days=20 * 365)
"""Lifetime of a new CA."""

CA_RENEW_BEFORE = dt.timedelta(days=5 * 365)
"""A CA that expires within this time is replaced (its certificates are issued again)."""

LEAF_VALIDITY = dt.timedelta(days=5 * 365)
"""Lifetime of a service certificate (never beyond the CA's own end)."""

LEAF_RENEW_BEFORE = dt.timedelta(days=365)
"""A service certificate that expires within this time is issued again. Containers load their
certificate at start, so a year leaves room for the next deploy to recreate them."""

DEFAULT_NAMES = ("localhost", "127.0.0.1", "::1")
"""Names every service certificate carries besides the service's own (health checks)."""

CA_COMMON_NAME = "MowDeck internal CA"

_CA_KEY = "ca.key"
_CA_CERT = "ca.crt"
_CA_PREVIOUS = "ca-previous.crt"
_BUNDLE = "ca-bundle.pem"
_LEAF_CERT = "tls.crt"
_LEAF_KEY = "tls.key"


@dataclass(frozen=True, slots=True)
class CertSpec:
    """One service certificate: the file owner and the names it is valid for."""

    name: str
    uid: int
    gid: int
    extra_names: tuple[str, ...] = field(default=())

    @property
    def names(self) -> tuple[str, ...]:
        """Every subject alternative name, the service name first, without duplicates."""
        seen: dict[str, None] = {}
        for item in (self.name, *DEFAULT_NAMES, *self.extra_names):
            seen.setdefault(item, None)
        return tuple(seen)


def parse_spec(text: str) -> CertSpec:
    """Parse ``NAME:UID:GID[:EXTRA,...]`` (raises :class:`ValueError`)."""
    parts = text.split(":", 3)
    if len(parts) < 3:
        msg = f"certificate spec {text!r}: expected NAME:UID:GID[:EXTRA_NAME,...]"
        raise ValueError(msg)
    name, uid, gid = parts[0].strip(), parts[1].strip(), parts[2].strip()
    if not name or not name.replace("-", "").replace(".", "").isalnum() or name != name.lower():
        msg = f"certificate spec {text!r}: the name must be a lower case host name"
        raise ValueError(msg)
    if not (uid.isdigit() and gid.isdigit()):
        msg = f"certificate spec {text!r}: UID and GID must be numbers"
        raise ValueError(msg)
    extras = (
        tuple(item.strip() for item in parts[3].split(",") if item.strip())
        if len(parts) > 3
        else ()
    )
    return CertSpec(name=name, uid=int(uid), gid=int(gid), extra_names=extras)


@dataclass(slots=True)
class Report:
    """What a run changed, for the job's log."""

    ca_created: bool = False
    issued: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


# --- files --------------------------------------------------------------------------------------


def _write_atomic(
    path: Path, data: bytes, *, mode: int, owner: tuple[int, int] | None = None
) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        if owner is not None and hasattr(os, "chown"):
            os.chown(tmp, *owner)
        if os.name == "nt" and path.exists():  # pragma: no cover - a read-only target on Windows
            os.chmod(path, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _set_owner(path: Path, *, mode: int, owner: tuple[int, int]) -> None:
    os.chmod(path, mode)
    if hasattr(os, "chown"):
        os.chown(path, *owner)


def _read_certs(path: Path) -> list[x509.Certificate]:
    try:
        return x509.load_pem_x509_certificates(path.read_bytes())
    except (OSError, ValueError):
        return []


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


# --- issuing ------------------------------------------------------------------------------------


def _new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _general_names(names: Iterable[str]) -> list[x509.GeneralName]:
    result: list[x509.GeneralName] = []
    for item in names:
        try:
            result.append(x509.IPAddress(ipaddress.ip_address(item)))
        except ValueError:
            result.append(x509.DNSName(item))
    return result


def issue_ca(now: dt.datetime) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """A new self-signed CA for the internal hops (may sign leaf certificates only)."""
    key = _new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_COMMON_NAME)])
    public = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .sign(key, hashes.SHA256())
    )
    return key, cert


def issue_leaf(
    spec: CertSpec,
    ca_key: ec.EllipticCurvePrivateKey,
    ca_cert: x509.Certificate,
    now: dt.datetime,
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    """A server and client certificate of ``spec`` signed by the CA."""
    key = _new_key()
    public = key.public_key()
    not_after = min(now + LEAF_VALIDITY, ca_cert.not_valid_after_utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, spec.name)]))
        .issuer_name(ca_cert.subject)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName(_general_names(spec.names)), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()),  # type: ignore[arg-type]
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return key, cert


def _leaf_is_current(
    cert_path: Path,
    key_path: Path,
    spec: CertSpec,
    ca_cert: x509.Certificate,
    now: dt.datetime,
) -> bool:
    certs = _read_certs(cert_path)
    if not certs:
        return False
    cert = certs[0]
    try:
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        cert.verify_directly_issued_by(ca_cert)
    except (OSError, ValueError, TypeError, InvalidSignature):
        return False
    if key.public_key() != cert.public_key():
        return False
    if (
        cert.not_valid_after_utc - now < LEAF_RENEW_BEFORE
        and cert.not_valid_after_utc < ca_cert.not_valid_after_utc
    ):
        return False
    if cert.not_valid_after_utc - now < dt.timedelta(days=1):
        return False
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return False
    return set(_general_names(spec.names)) == set(san)


def _ca_is_current(
    cert: x509.Certificate, key: ec.EllipticCurvePrivateKey, now: dt.datetime
) -> bool:
    return (
        cert.public_key() == key.public_key() and cert.not_valid_after_utc - now >= CA_RENEW_BEFORE
    )


def _load_ca(ca_dir: Path) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate] | None:
    certs = _read_certs(ca_dir / _CA_CERT)
    try:
        key = serialization.load_pem_private_key((ca_dir / _CA_KEY).read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None
    if not certs or not isinstance(key, ec.EllipticCurvePrivateKey):
        return None
    return key, certs[0]


def ensure(
    ca_dir: Path,
    out_dir: Path,
    specs: Sequence[CertSpec],
    *,
    now: dt.datetime | None = None,
    public_roots: bytes | None = None,
) -> Report:
    """Create or renew the CA and every certificate of ``specs``; see the module docstring."""
    now = now or dt.datetime.now(dt.UTC)
    report = Report()
    ca_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(ca_dir, 0o700)
    os.chmod(out_dir, 0o755)  # noqa: S103 - certificates are public; every key is 0400 below

    loaded = _load_ca(ca_dir)
    if loaded is None or not _ca_is_current(loaded[1], loaded[0], now):
        if loaded is not None and loaded[1].not_valid_after_utc > now:
            # Keep the old CA trusted while it is valid: containers not yet recreated still
            # serve certificates it signed.
            _write_atomic(ca_dir / _CA_PREVIOUS, _pem(loaded[1]), mode=0o644)
        ca_key, ca_cert = issue_ca(now)
        _write_atomic(ca_dir / _CA_KEY, _key_pem(ca_key), mode=0o600)
        _write_atomic(ca_dir / _CA_CERT, _pem(ca_cert), mode=0o644)
        report.ca_created = True
    else:
        ca_key, ca_cert = loaded

    trusted = [ca_cert] + [
        cert for cert in _read_certs(ca_dir / _CA_PREVIOUS) if cert.not_valid_after_utc > now
    ]
    trust_pem = b"".join(_pem(cert) for cert in trusted)
    roots = tls_public_roots() if public_roots is None else public_roots
    if roots and not roots.endswith(b"\n"):
        roots += b"\n"
    _write_atomic(out_dir / _CA_CERT, trust_pem, mode=0o644)
    _write_atomic(out_dir / _BUNDLE, roots + trust_pem, mode=0o644)

    for spec in specs:
        directory = out_dir / spec.name
        directory.mkdir(exist_ok=True)
        os.chmod(directory, 0o755)  # noqa: S103 - the key in it is 0400 and owned by its reader
        cert_path, key_path = directory / _LEAF_CERT, directory / _LEAF_KEY
        owner = (spec.uid, spec.gid)
        if _leaf_is_current(cert_path, key_path, spec, ca_cert, now):
            _set_owner(key_path, mode=0o400, owner=owner)
            _set_owner(cert_path, mode=0o644, owner=owner)
            report.kept.append(spec.name)
            continue
        key, cert = issue_leaf(spec, ca_key, ca_cert, now)
        _write_atomic(key_path, _key_pem(key), mode=0o400, owner=owner)
        _write_atomic(cert_path, _pem(cert), mode=0o644, owner=owner)
        report.issued.append(spec.name)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of ``python -m mowdeck_service_kit.certs``."""
    parser = argparse.ArgumentParser(
        prog="mowdeck_service_kit.certs", description=__doc__.split("\n")[0]
    )
    parser.add_argument(
        "--ca-dir", required=True, type=Path, help="directory of the CA key (private)"
    )
    parser.add_argument(
        "--out-dir", required=True, type=Path, help="directory the containers mount"
    )
    parser.add_argument(
        "--cert",
        action="append",
        default=[],
        metavar="NAME:UID:GID[:EXTRA,...]",
        help="a certificate",
    )
    args = parser.parse_args(argv)
    try:
        specs = [parse_spec(item) for item in args.cert]
    except ValueError as exc:
        print(f"tls-init: {exc}", file=sys.stderr)
        return 2
    try:
        report = ensure(args.ca_dir, args.out_dir, specs)
    except (OSError, ValueError) as exc:
        print(f"tls-init: failed: {exc}", file=sys.stderr)
        return 1
    if report.ca_created:
        print("tls-init: created a new internal CA")
    if report.issued:
        print(f"tls-init: issued certificates for {', '.join(report.issued)}")
    if report.kept:
        print(f"tls-init: certificates still valid for {', '.join(report.kept)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
