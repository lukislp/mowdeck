"""Ed25519 tickets: scope, expiry, signature and key loading."""

import base64
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from relay_support import T0, at

from camera_relay.adapters.tickets import (
    Ed25519TicketSigner,
    load_private_key,
    verify_ticket,
)
from camera_relay.domain.tickets import (
    InvalidTicketError,
    TicketClaims,
    camera_path,
    camera_segment,
)


def _claims(**overrides: object) -> TicketClaims:
    relay_id = "0123456789abcdef0123456789abcdef"
    values: dict[str, object] = {
        "account_id": uuid4(),
        "mower_id": uuid4(),
        "relay_id": relay_id,
        "lease_id": uuid4(),
        "paths": (camera_path(relay_id, "front_left"), camera_path(relay_id, "front_right")),
        "issued_at": T0,
        "expires_at": T0 + timedelta(seconds=60),
    }
    values.update(overrides)
    return TicketClaims(**values)  # type: ignore[arg-type]


def test_round_trip_keeps_the_scope() -> None:
    signer = Ed25519TicketSigner.generate()
    claims = _claims()
    ticket = signer.sign(claims)
    assert ticket.startswith("v1.") and len(ticket) < 2048
    assert signer.verify(ticket, at(59)) == claims


@pytest.mark.parametrize("seconds", [60, 61, 3600])
def test_expired_tickets_are_refused(seconds: float) -> None:
    signer = Ed25519TicketSigner.generate()
    ticket = signer.sign(_claims())
    with pytest.raises(InvalidTicketError, match="expired"):
        signer.verify(ticket, at(seconds))


def test_another_key_cannot_verify() -> None:
    ticket = Ed25519TicketSigner.generate().sign(_claims())
    with pytest.raises(InvalidTicketError, match="signature"):
        Ed25519TicketSigner.generate().verify(ticket, T0)


def _tamper(ticket: str, field: str, value: str) -> str:
    version, payload, signature = ticket.split(".")
    data = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    claims = json.loads(data)
    claims[field] = value
    raw = json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()
    forged = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{version}.{forged}.{signature}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("acc", str(uuid4())),
        ("mow", str(uuid4())),
        ("lse", str(uuid4())),
        ("rid", "ffffffffffffffffffffffffffffffff"),
    ],
)
def test_tampered_tickets_are_refused(field: str, value: str) -> None:
    signer = Ed25519TicketSigner.generate()
    with pytest.raises(InvalidTicketError, match="signature"):
        signer.verify(_tamper(signer.sign(_claims()), field, value), T0)


@pytest.mark.parametrize(
    "ticket",
    ["", "v1", "v1.a", "v2.abc.def", "v1.!!!.@@@", "v1." + "a" * 3000 + ".b", "x.y.z"],
)
def test_garbage_is_refused(ticket: str) -> None:
    with pytest.raises(InvalidTicketError):
        Ed25519TicketSigner.generate().verify(ticket, T0)


def test_a_signed_malformed_payload_is_refused() -> None:
    key = Ed25519PrivateKey.generate()
    payload = base64.urlsafe_b64encode(b'{"acc":"x"}').rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(key.sign(f"v1.{payload}".encode())).rstrip(b"=")
    with pytest.raises(InvalidTicketError, match="malformed"):
        verify_ticket(f"v1.{payload}.{signature.decode()}", key.public_key(), T0)


def test_claims_bind_account_path_and_time() -> None:
    claims = _claims()
    path = claims.paths[0]
    assert claims.allows(account_id=claims.account_id, path=path, now=at(59))
    assert not claims.allows(account_id=uuid4(), path=path, now=at(1))
    assert not claims.allows(account_id=claims.account_id, path="r/other/front_left", now=at(1))
    assert not claims.allows(account_id=claims.account_id, path=path, now=at(60))


@pytest.mark.parametrize(
    ("camera", "segment"),
    [("front_left", "front_left"), ("cam-2", "cam-2"), ("a/b", None), ("../x", None), ("ä", None)],
)
def test_camera_segments_are_path_safe(camera: str, segment: str | None) -> None:
    result = camera_segment(camera)
    if segment is not None:
        assert result == segment
    else:
        assert result.startswith("x") and result.isalnum() and result == camera_segment(camera)


def test_keys_load_from_pem_and_seed() -> None:
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    seed = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    for value in (pem, base64.b64encode(seed).decode(), base64.urlsafe_b64encode(seed).decode()):
        loaded = load_private_key(value)
        assert (
            loaded.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            == public
        )
    ticket = Ed25519TicketSigner.from_setting(pem).sign(_claims())
    assert verify_ticket(ticket, key.public_key(), T0).relay_id == _claims().relay_id


@pytest.mark.parametrize("value", ["", "not base64 at all!", base64.b64encode(b"short").decode()])
def test_bad_keys_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="key"):
        load_private_key(value)
