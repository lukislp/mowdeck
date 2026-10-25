"""A software WebAuthn authenticator for the passkey tests (a plain module, no conftest.py).

:class:`SoftAuthenticator` answers ``navigator.credentials.create()`` and ``.get()`` options the
way a platform authenticator with a synced passkey does: ES256 (P-256) keys, ``none``
attestation, user presence and verification set, the backup flags of a multi-device credential.
Its output is the JSON of ``PublicKeyCredential.toJSON()``, verified by the real py_webauthn in
the service. Flags, origin, counter and user handle can be bent for the negative tests.
"""

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import encode_cbor

UP, UV, BE, BS, AT = 0x01, 0x04, 0x08, 0x10, 0x40
ICLOUD_AAGUID = "fbfc3007-154e-4ecc-8c0b-6e020557d7bd"


def b64url(data: bytes) -> str:
    """base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    """Inverse of :func:`b64url`."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class SoftCredential:
    """One discoverable credential held by the soft authenticator."""

    credential_id: bytes
    key: ec.EllipticCurvePrivateKey
    rp_id: str
    user_handle: bytes
    counter: int = 0


@dataclass
class SoftAuthenticator:
    """Creates and uses passkeys like a platform authenticator."""

    origin: str
    aaguid: str = ICLOUD_AAGUID
    counting: bool = False
    """Increase the signature counter on every use (hardware keys); synced passkeys keep 0."""
    credentials: list[SoftCredential] = field(default_factory=list)

    def create(
        self,
        options: dict[str, Any],
        *,
        flags: int = UP | UV | BE | BS,
        credential_id: bytes | None = None,
    ) -> dict[str, Any]:
        """Answer ``PublicKeyCredentialCreationOptions`` (as JSON)."""
        rp_id = options["rp"]["id"]
        key = ec.generate_private_key(ec.SECP256R1())
        numbers = key.public_key().public_numbers()
        cose = encode_cbor(
            {
                1: 2,
                3: -7,
                -1: 1,
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )
        credential_id = credential_id or secrets.token_bytes(16)
        cred = SoftCredential(credential_id, key, rp_id, unb64url(options["user"]["id"]))
        attested = (
            bytes.fromhex(self.aaguid.replace("-", ""))
            + len(credential_id).to_bytes(2, "big")
            + credential_id
            + cose
        )
        auth_data = self._auth_data(rp_id, flags | AT, cred.counter) + attested
        client_data = self._client_data("webauthn.create", options["challenge"])
        attestation = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        self.credentials.append(cred)
        return {
            "id": b64url(credential_id),
            "rawId": b64url(credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "attestationObject": b64url(attestation),
                "transports": ["internal", "hybrid"],
            },
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
        }

    def get(
        self,
        options: dict[str, Any],
        *,
        credential: SoftCredential | None = None,
        flags: int = UP | UV | BE | BS,
        origin: str | None = None,
        user_handle: bytes | None = None,
        counter: int | None = None,
    ) -> dict[str, Any]:
        """Answer ``PublicKeyCredentialRequestOptions`` with a discoverable credential."""
        cred = credential or self.credentials[-1]
        if counter is not None:
            cred.counter = counter
        elif self.counting:
            cred.counter += 1
        auth_data = self._auth_data(options["rpId"], flags, cred.counter)
        client_data = self._client_data("webauthn.get", options["challenge"], origin)
        signed = auth_data + hashlib.sha256(client_data).digest()
        signature = cred.key.sign(signed, ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64url(cred.credential_id),
            "rawId": b64url(cred.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": b64url(user_handle or cred.user_handle),
            },
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
        }

    @staticmethod
    def _auth_data(rp_id: str, flags: int, counter: int) -> bytes:
        return hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + counter.to_bytes(4, "big")

    def _client_data(self, kind: str, challenge: str, origin: str | None = None) -> bytes:
        return json.dumps(
            {
                "type": kind,
                "challenge": challenge,
                "origin": origin or self.origin,
                "crossOrigin": False,
            }
        ).encode()
